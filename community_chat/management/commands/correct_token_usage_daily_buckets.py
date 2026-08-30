from __future__ import annotations

import json
from datetime import date

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Count, Sum

from community_chat.models import TokenUsageAccount, TokenUsageDailyBucket
from community_chat.token_usage import local_usage_date


TOKEN_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_creation_tokens",
    "reasoning_tokens",
)


class Command(BaseCommand):
    help = (
        "Remove daily token-usage buckets for one opted-in member and one "
        "Melbourne reporting date. Dry-run by default; cumulative sessions "
        "and all-time totals are never changed."
    )

    def add_arguments(self, parser):
        parser.add_argument("--email", required=True)
        parser.add_argument("--usage-date", required=True)
        parser.add_argument("--apply", action="store_true")
        parser.add_argument(
            "--confirm-email",
            help="Must exactly match --email when --apply is used.",
        )

    def handle(self, *args, **options):
        email = str(options["email"]).strip().lower()
        if not email:
            raise CommandError("--email must not be blank.")
        try:
            usage_date = date.fromisoformat(str(options["usage_date"]))
        except ValueError as exc:
            raise CommandError("--usage-date must be an ISO date (YYYY-MM-DD).") from exc
        if usage_date > local_usage_date():
            raise CommandError("--usage-date cannot be in the future.")

        account = (
            TokenUsageAccount.objects.select_related("user")
            .filter(user__email__iexact=email)
            .first()
        )
        if account is None:
            raise CommandError("No token-usage account exists for that email.")

        buckets = TokenUsageDailyBucket.objects.filter(
            account=account,
            usage_date=usage_date,
        )
        aggregates = buckets.aggregate(
            rows=Count("pk"),
            **{field: Sum(field) for field in TOKEN_FIELDS},
        )
        token_totals = {
            field: int(aggregates.get(field) or 0)
            for field in TOKEN_FIELDS
        }
        preview = {
            "account_id": str(account.pk),
            "apply": bool(options["apply"]),
            "daily_bucket_rows": int(aggregates.get("rows") or 0),
            "email": email,
            "session_rows_preserved": account.sessions.count(),
            "token_totals_removed": token_totals,
            "usage_date": usage_date.isoformat(),
        }

        if not options["apply"]:
            self.stdout.write(json.dumps(preview, sort_keys=True))
            return

        confirm_email = str(options.get("confirm_email") or "").strip().lower()
        if confirm_email != email:
            raise CommandError("--confirm-email must exactly match --email with --apply.")

        with transaction.atomic():
            deleted_rows, _ = buckets.delete()
        preview["deleted_rows"] = deleted_rows
        preview["remaining_daily_bucket_rows"] = TokenUsageDailyBucket.objects.filter(
            account=account,
            usage_date=usage_date,
        ).count()
        self.stdout.write(json.dumps(preview, sort_keys=True))
