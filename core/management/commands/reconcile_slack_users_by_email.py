from __future__ import annotations

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.core.management.base import CommandError
from django.db.models import Q

from content_factory.models import OrganizationContentConfig
from integrations.models import UserIntegration
from integrations.services.slack import SlackService
from core.management.commands.cleanup_users import Command as CleanupUsersCommand


class Command(BaseCommand):
    help = "Link Slack-era user ids to email-login users when Slack profile email matches."

    def add_arguments(self, parser):
        parser.add_argument("--commit", action="store_true")
        parser.add_argument("--limit", type=int, default=0)

    def handle(self, *args, **options):
        User = get_user_model()
        slack_ids = set(User.objects.exclude(slack_id__isnull=True).exclude(slack_id="").values_list("slack_id", flat=True))
        slack_ids.update(
            UserIntegration.objects.exclude(slack_user_id__isnull=True)
            .exclude(slack_user_id="")
            .values_list("slack_user_id", flat=True)
        )
        slack_ids.update(
            OrganizationContentConfig.objects.exclude(connected_slack_user_id__isnull=True)
            .exclude(connected_slack_user_id="")
            .values_list("connected_slack_user_id", flat=True)
        )
        slack_ids = sorted(slack_id for slack_id in slack_ids if not str(slack_id).startswith("mlai_user:"))
        if options["limit"]:
            slack_ids = slack_ids[: options["limit"]]

        commit = bool(options["commit"])
        failures = []
        self.stdout.write(f"{'Committing' if commit else 'Dry-run'} Slack/email reconciliation for {len(slack_ids)} Slack id(s).")
        service = SlackService()
        for slack_id in slack_ids:
            try:
                profile = service.get_user_profile(slack_id)
            except Exception as exc:
                self.stdout.write(self.style.WARNING(f"{slack_id}: unable to fetch profile email ({exc})"))
                if commit:
                    failures.append(f"{slack_id}: profile lookup failed")
                continue
            email = str((profile or {}).get("email") or "").strip().lower()
            if not email:
                self.stdout.write(f"{slack_id}: no profile email")
                if commit:
                    failures.append(f"{slack_id}: profile has no email")
                continue
            email_user = User.objects.filter(email__iexact=email).first()
            slack_user = User.objects.filter(slack_id=slack_id).first()
            if not email_user:
                self.stdout.write(f"{slack_id}: no local email user for {email}")
                continue
            if email_user.slack_id == slack_id:
                self.stdout.write(f"{slack_id}: already linked to {email}")
                continue
            if email_user.slack_id:
                self.stdout.write(self.style.WARNING(f"{slack_id}: {email} already has Slack id {email_user.slack_id}"))
                if commit:
                    failures.append(f"{slack_id}: target already owns another Slack id")
                continue
            self.stdout.write(f"{slack_id}: link to {email}")
            if not commit:
                continue
            if slack_user and slack_user.pk != email_user.pk:
                merger = CleanupUsersCommand()
                merger.stdout = self.stdout
                merger.stderr = self.stderr
                try:
                    merger.merge_users_with_retry(
                        source_id=slack_user.pk,
                        target_id=email_user.pk,
                    )
                except (ValueError, CommandError) as exc:
                    raise CommandError(
                        f"{slack_id}: durable identity reconciliation failed: {exc}"
                    ) from exc
            else:
                # No duplicate principal exists, so there is no durable ownership
                # to transfer. Re-read before linking to avoid a stale profile row.
                updated = User.objects.filter(
                    Q(slack_id__isnull=True) | Q(slack_id=""),
                    pk=email_user.pk,
                ).update(slack_id=slack_id)
                if updated != 1:
                    raise CommandError(
                        f"{slack_id}: target identity changed during reconciliation"
                    )
        if failures:
            raise CommandError(
                "Slack/email reconciliation left unresolved identities: "
                + "; ".join(failures[:10])
            )
