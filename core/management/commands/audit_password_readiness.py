import json

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db.models import Count, Q
from django.db.models.functions import Lower


class Command(BaseCommand):
    help = "Audit existing MLAI accounts before enabling password-only sign-in."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Accepted for deployment runbook consistency; this command never mutates data.",
        )
        parser.add_argument(
            "--json",
            action="store_true",
            dest="as_json",
            help="Write the aggregate report as JSON.",
        )
        parser.add_argument(
            "--fail-on-blockers",
            action="store_true",
            help="Exit non-zero when duplicate emails or active placeholder accounts exist.",
        )

    def handle(self, *args, **options):
        User = get_user_model()
        users = User.objects.all()
        duplicate_groups = list(
            users.annotate(canonical_email=Lower("email"))
            .values("canonical_email")
            .annotate(account_count=Count("id"))
            .filter(account_count__gt=1)
            .values_list("account_count", flat=True)
        )
        placeholder_filter = Q(email__iendswith="@slack.placeholder.com")
        usable_passwords = sum(
            1
            for password in users.values_list("password", flat=True).iterator()
            if password and not password.startswith("!")
        )
        total = users.count()
        report = {
            "total_accounts": total,
            "active_accounts": users.filter(is_active=True).count(),
            "inactive_accounts": users.filter(is_active=False).count(),
            "usable_password_accounts": usable_passwords,
            "unusable_password_accounts": total - usable_passwords,
            "verified_email_accounts": users.filter(email_verified_at__isnull=False).count(),
            "slack_linked_accounts": users.filter(slack_id__isnull=False).count(),
            "slack_placeholder_accounts": users.filter(placeholder_filter).count(),
            "active_slack_placeholder_accounts": users.filter(
                placeholder_filter,
                is_active=True,
            ).count(),
            "incomplete_profile_accounts": users.filter(
                Q(first_name="") | Q(first_name__isnull=True)
            ).count(),
            "case_insensitive_duplicate_groups": len(duplicate_groups),
            "accounts_in_duplicate_groups": sum(duplicate_groups),
            "eligible_password_setup_accounts": users.filter(
                is_active=True,
            ).exclude(placeholder_filter).filter(password__startswith="!").count(),
        }

        if options["as_json"]:
            self.stdout.write(json.dumps(report, sort_keys=True))
        else:
            self.stdout.write("MLAI password readiness audit (read-only)")
            for key, value in report.items():
                self.stdout.write(f"{key}: {value}")

        blockers = (
            report["case_insensitive_duplicate_groups"]
            + report["active_slack_placeholder_accounts"]
        )
        if options["fail_on_blockers"] and blockers:
            raise CommandError(
                "Password rollout blocked: resolve case-insensitive duplicate emails "
                "and active Slack placeholder accounts first."
            )
