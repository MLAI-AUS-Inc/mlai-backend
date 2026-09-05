from __future__ import annotations

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.core.management.base import CommandError
from django.db import transaction

from content_factory.models import OrganizationContentConfig
from integrations.models import UserIntegration
from integrations.services.slack import SlackService
from core.management.commands.cleanup_users import Command as CleanupUsersCommand

from core.slack_founder_links import (
    invalidate_unused_slack_founder_link_requests,
    user_participates_in_slack_founder_link,
)


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
            if user_participates_in_slack_founder_link(email_user) or (
                slack_user is not None
                and user_participates_in_slack_founder_link(slack_user)
            ):
                self.stdout.write(
                    self.style.WARNING(
                        f"{slack_id}: explicit Roo-Founder Tools link exists; manual support required"
                    )
                )
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
                # to transfer. Lock and re-check the target before assigning the
                # Slack identity, including links created during this sweep.
                with transaction.atomic():
                    locked_email_user = User.objects.select_for_update().get(
                        pk=email_user.pk
                    )
                    if user_participates_in_slack_founder_link(locked_email_user):
                        self.stdout.write(
                            self.style.WARNING(
                                f"{slack_id}: explicit Roo-Founder Tools link appeared; "
                                "manual support required"
                            )
                        )
                        continue
                    current_target_slack_id = str(
                        locked_email_user.slack_id or ""
                    ).strip()
                    if current_target_slack_id:
                        if current_target_slack_id == slack_id:
                            self.stdout.write(
                                f"{slack_id}: already linked while reconciliation was running"
                            )
                        else:
                            self.stdout.write(
                                self.style.WARNING(
                                    f"{slack_id}: target gained another Slack identity; "
                                    "manual support required"
                                )
                            )
                        continue
                    invalidate_unused_slack_founder_link_requests(
                        locked_email_user
                    )
                    locked_email_user.slack_id = slack_id
                    locked_email_user.save(update_fields=["slack_id"])
        if failures:
            raise CommandError(
                "Slack/email reconciliation left unresolved identities: "
                + "; ".join(failures[:10])
            )
