"""Read-only founder funnel for a signup window (P0.1 staff command).

Reproduces the production funnel table from the 29 Oct 2026 founder-journey
audit: of the non-staff users who joined in the window, how many submitted the
member questions, created a startup, and reached a delivered article draft.
Platform-wide island_refresh failed and denied counts for the same window are
reported separately, because they cover every organisation, not new founders.

Postgres runs the queries inside a READ ONLY transaction that is rolled back.
"""
from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import DEFAULT_DB_ALIAS, connections, transaction
from django.utils import timezone

from community_chat.onboarding_models import CommunityMemberProfile
from content_factory.founder_journey_alerts import organizations_with_delivered_drafts
from founder_tools.models import VibeRaisingCompany
from workflow_runs.models import ContentFactoryRun

FUNNEL_STEPS = ("users", "member_submitted", "startups", "articles")


def parse_window_bound(value: str, *, end: bool) -> datetime:
    """An ISO date (whole day, inclusive at the end) or an ISO datetime, as an aware datetime."""
    try:
        if len(value) == 10:
            day = date.fromisoformat(value)
            moment = datetime.combine(day + timedelta(days=1) if end else day, time.min)
        else:
            moment = datetime.fromisoformat(value)
    except ValueError as exc:
        raise CommandError(f"{value!r} is not an ISO date or datetime.") from exc
    return moment if timezone.is_aware(moment) else timezone.make_aware(moment, timezone.get_default_timezone())


def founder_funnel(*, since: datetime, until: datetime, using: str = DEFAULT_DB_ALIAS) -> dict:
    """Count each funnel step for non-staff users who joined in [since, until)."""
    users = get_user_model().objects.using(using).filter(date_joined__gte=since, date_joined__lt=until, is_staff=False)
    user_ids = set(users.values_list("pk", flat=True))
    submitted = set(CommunityMemberProfile.objects.using(using)
                    .filter(user_id__in=user_ids, submitted_at__isnull=False).values_list("user_id", flat=True))
    companies = list(VibeRaisingCompany.objects.using(using).filter(profile__user_id__in=user_ids)
                     .values_list("profile__user_id", "organization_id"))
    founders = {user_id for user_id, _ in companies}
    delivered_orgs = organizations_with_delivered_drafts({org_id for _, org_id in companies}, using=using)
    with_article = {user_id for user_id, org_id in companies if org_id in delivered_orgs}
    island_refresh = ContentFactoryRun.objects.using(using).filter(
        workflow="island_refresh", created_at__gte=since, created_at__lt=until)
    failed = island_refresh.filter(status="failed").count()
    denied = island_refresh.filter(status="denied").count()
    return {
        "since": since.isoformat(), "until": until.isoformat(),
        "funnel": {"users": len(user_ids), "member_submitted": len(submitted), "startups": len(founders),
                   "articles": len(with_article)},
        "startup_companies_created": len(companies),
        "island_refresh_platform_wide": {"failed": failed, "denied": denied, "failed_or_denied": failed + denied},
    }


class Command(BaseCommand):
    help = ("Read-only founder funnel for users who joined in a window: users, member questions submitted, "
            "startups created and articles (delivered drafts), plus platform-wide island_refresh failures.")

    def add_arguments(self, parser):
        parser.add_argument("--since", required=True, help="Window start: ISO date (00:00) or datetime.")
        parser.add_argument("--until", default="", help="Window end: ISO date (inclusive) or datetime. Defaults to now.")
        parser.add_argument("--database", default=DEFAULT_DB_ALIAS, help="Database alias to read.")
        parser.add_argument("--json", action="store_true", help="Print one JSON object instead of a table.")

    def handle(self, *args, **options):
        since = parse_window_bound(options["since"], end=False)
        until = parse_window_bound(options["until"], end=True) if options["until"] else timezone.now()
        if until <= since:
            raise CommandError("--until must be after --since.")
        using = options["database"]
        with transaction.atomic(using=using):
            if connections[using].vendor == "postgresql":
                with connections[using].cursor() as cursor:
                    cursor.execute("SET TRANSACTION READ ONLY")
            report = founder_funnel(since=since, until=until, using=using)
            transaction.set_rollback(True, using=using)
        if options["json"]:
            self.stdout.write(json.dumps(report, sort_keys=True))
            return
        self.stdout.write(f"Founder funnel, joined {report['since']} to {report['until']} (staff excluded)")
        labels = {"users": "Signed up", "member_submitted": "Member questions submitted",
                  "startups": "Created a startup", "articles": "Reached an article (delivered draft)"}
        for step in FUNNEL_STEPS:
            self.stdout.write(f"  {labels[step]:<38} {report['funnel'][step]}")
        self.stdout.write(f"  {'Startups (companies) created':<38} {report['startup_companies_created']}")
        island = report["island_refresh_platform_wide"]
        self.stdout.write("Platform-wide island_refresh in the same window (all organisations, reported separately)")
        self.stdout.write(f"  failed {island['failed']}, denied {island['denied']}, total {island['failed_or_denied']}")
