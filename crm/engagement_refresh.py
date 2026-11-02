"""Bounded daily rating refresh. No permission or messaging writes are allowed."""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
from datetime import timedelta
from time import monotonic
from uuid import uuid4
from zoneinfo import ZoneInfo

from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.db.models import Q
from django.db.models.functions import Lower
from django.utils import timezone

from core.models import AccountBan

from .engagement import engagement_rating
from .engagement_scoring import VERSION, score_engagement
from .engagement_sources import collect_activity
from .models import Contact, CrmAuditEvent

ZONE = ZoneInfo("Australia/Melbourne")
TARGET_TYPE = "crm_engagement_refresh"
LOCK_KEY = "crm:engagement-refresh:lock"
BATCH_SIZE = 500
PASS_SECONDS = 35
RETRY_MINUTES = 15


def pending_filter(day: str, now) -> Q:
    """Select unattempted records and due retries, explicitly including missing JSON keys."""

    field = "attributes__engagement_refresh"
    return (
        Q(**{f"{field}__day__isnull": True})
        | ~Q(**{f"{field}__day": day})
        | Q(**{f"{field}__version__isnull": True})
        | ~Q(**{f"{field}__version": VERSION})
        | (Q(**{f"{field}__status": "deferred"}) & Q(**{f"{field}__retry_at__lte": now.isoformat()}))
    )


def apply_observation(attributes: dict, *, activities, coverage: dict, now) -> tuple[dict, str]:
    """Change only the rating and refresh receipt; preserve all other attributes."""

    result = deepcopy(attributes) if isinstance(attributes, dict) else {"engagement_legacy_attributes": deepcopy(attributes), "marketing_review_hold": True}
    day = now.astimezone(ZONE).date().isoformat()
    previous = engagement_rating(result)
    rating = score_engagement(activities, as_of=now, coverage=coverage)
    status = "updated"
    if previous and previous["as_of"][:10] > day:
        status = "preserved_newer"
    elif rating is None or not rating["coverage"]["refresh_safe"]:
        status = "deferred"
        if previous:
            previous["coverage"]["latest_refresh"] = {
                "status": status, "attempted_at": now.isoformat(),
                "detail": "Previous score retained because an expected source cannot be assessed.",
                "sources": deepcopy(coverage),
            }
            result["engagement_rating"] = previous
    else:
        result["engagement_rating"] = rating
    result["engagement_refresh"] = {
        "day": day, "version": VERSION, "status": status,
        "attempted_at": now.isoformat(),
        "sources": deepcopy(coverage),
        **({"retry_at": (now + timedelta(minutes=RETRY_MINUTES)).isoformat()} if status == "deferred" else {}),
    }
    return result, status


def refresh_ratings(*, now=None, dry_run=False, max_seconds=PASS_SECONDS) -> dict:
    """Run a resumable pass, writing only Contact.attributes and audit metadata."""

    now = now or timezone.now()
    day = now.astimezone(ZONE).date().isoformat()
    target = f"{VERSION}:{day}"
    if not dry_run and CrmAuditEvent.objects.filter(
        action=CrmAuditEvent.Action.SYNC, target_type=TARGET_TYPE,
        target_id=target, detail__status="completed",
    ).exists():
        return {"status": "already_completed", "day": day}
    token = str(uuid4())
    if not cache.add(LOCK_KEY, token, timeout=300):
        return {"status": "locked", "day": day}
    counts = Counter()
    started = monotonic()
    after = None
    try:
        while monotonic() - started < max_seconds:
            candidates = Contact.objects.filter(pending_filter(day, now)).order_by("pk")
            if after is not None:
                candidates = candidates.filter(pk__gt=after)
            batch = list(candidates[:BATCH_SIZE])
            if not batch:
                break
            after = batch[-1].pk
            banned = set(AccountBan.objects.annotate(email_key=Lower("email")).filter(
                revoked_at__isnull=True, email_key__in=[c.email for c in batch],
            ).values_list("email_key", flat=True))
            # Adapters are read-only. Provider/query failure aborts before this
            # batch changes any rating, and a later tick can retry.
            observed = collect_activity([c for c in batch if c.email not in banned], as_of=now)
            with transaction.atomic():
                locked = list(Contact.objects.select_for_update().filter(pk__in=[c.pk for c in batch]))
                for contact in locked:
                    if contact.email in banned:
                        attributes = deepcopy(contact.attributes) if isinstance(contact.attributes, dict) else {"engagement_legacy_attributes": deepcopy(contact.attributes), "marketing_review_hold": True}
                        attributes["engagement_refresh"] = {
                            "day": day, "version": VERSION, "status": "banned_skipped",
                            "attempted_at": now.isoformat(),
                        }
                        # Do not add activity/ratings or restore a banned profile.
                        contact.attributes = attributes
                        counts["banned_skipped"] += 1
                    else:
                        contact.attributes, status = apply_observation(
                            contact.attributes,
                            activities=observed["activities"].get(contact.email, []),
                            coverage=observed["coverage"], now=now,
                        )
                        counts[status] += 1
                    contact.updated_at = now
                Contact.objects.bulk_update(locked, ["attributes", "updated_at"], batch_size=250)
                if dry_run:
                    transaction.set_rollback(True)
            counts["processed"] += len(batch)
        remaining = Contact.objects.filter(pending_filter(day, now)).exists()
        deferred = Contact.objects.filter(
            attributes__engagement_refresh__day=day,
            attributes__engagement_refresh__version=VERSION,
            attributes__engagement_refresh__status="deferred",
        ).exists()
        status = "dry_run" if dry_run else "partial" if remaining else "deferred" if deferred else "completed"
        result = {"status": status, "day": day, "version": VERSION, "counts": dict(counts)}
        if not dry_run:
            CrmAuditEvent.objects.create(
                action=CrmAuditEvent.Action.SYNC, target_type=TARGET_TYPE,
                target_id=target, detail=result,
            )
        return result
    finally:
        if cache.get(LOCK_KEY) == token:
            cache.delete(LOCK_KEY)


def run_engagement_refresh_scheduler() -> dict:
    """Tick daily after the configured Melbourne hour; never invoke a sender."""

    if not getattr(settings, "CRM_ENGAGEMENT_REFRESH_ENABLED", False):
        return {"status": "disabled"}
    now = timezone.now()
    hour = max(0, min(23, int(getattr(settings, "CRM_ENGAGEMENT_REFRESH_HOUR", 6))))
    if now.astimezone(ZONE).hour < hour:
        return {"status": "not_due"}
    try:
        return refresh_ratings(now=now)
    except Exception as exc:
        # No partial source failure is interpreted as inactivity. Log only the
        # exception type upstream; provider exceptions may contain identifiers.
        return {"status": "deferred", "reason": "activity_source_unavailable", "error_type": type(exc).__name__}
