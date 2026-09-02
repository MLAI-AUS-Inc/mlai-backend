from __future__ import annotations

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction

from content_factory.models import OrganizationContentConfig
from integrations.models import UserIntegration
from integrations.services.slack import SlackService
from roo.models import CoworkingBooking, OfficeManagerAssignment


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
        self.stdout.write(f"{'Committing' if commit else 'Dry-run'} Slack/email reconciliation for {len(slack_ids)} Slack id(s).")
        service = SlackService()
        for slack_id in slack_ids:
            try:
                profile = service.get_user_profile(slack_id)
            except Exception as exc:
                self.stdout.write(self.style.WARNING(f"{slack_id}: unable to fetch profile email ({exc})"))
                continue
            email = str((profile or {}).get("email") or "").strip().lower()
            if not email:
                self.stdout.write(f"{slack_id}: no profile email")
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
                continue
            self.stdout.write(f"{slack_id}: link to {email}")
            if not commit:
                continue
            try:
                with transaction.atomic():
                    locked_ids = [email_user.pk]
                    if slack_user and slack_user.pk != email_user.pk:
                        locked_ids.append(slack_user.pk)
                    locked_users = {
                        user.pk: user
                        for user in User.objects.select_for_update()
                        .filter(pk__in=locked_ids)
                        .order_by("pk")
                    }
                    locked_email_user = locked_users[email_user.pk]
                    locked_slack_user = (
                        locked_users.get(slack_user.pk)
                        if slack_user and slack_user.pk != email_user.pk
                        else None
                    )
                    if locked_slack_user is not None:
                        has_coworking_ownership = (
                            CoworkingBooking.objects.select_for_update()
                            .filter(user=locked_slack_user)
                            .exists()
                        )
                        has_office_manager_ownership = (
                            OfficeManagerAssignment.objects.select_for_update()
                            .filter(user=locked_slack_user)
                            .exists()
                        )
                        if (
                            has_coworking_ownership
                            or has_office_manager_ownership
                        ):
                            raise ValueError(
                                "durable coworking/Office Manager ownership "
                                "requires the atomic cleanup_users merge"
                            )
                        locked_slack_user.slack_id = None
                        locked_slack_user.save(update_fields=["slack_id"])
                    locked_email_user.slack_id = slack_id
                    locked_email_user.save(update_fields=["slack_id"])
            except ValueError as exc:
                self.stdout.write(
                    self.style.WARNING(f"{slack_id}: reconciliation refused ({exc})")
                )
