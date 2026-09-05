"""Bounded existing-Luma ingestion with verified account/email matching."""

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from community_chat.models import CommunityChatDevice
from community_chat.volunteer.access import flag
from community_chat.volunteer.receipts import record_luma_guest
from integrations.services.luma import LumaAttendeeReportService


class Command(BaseCommand):
    help = "Ingest verified Luma check-ins for selected events; never registers guests."

    def add_arguments(self, parser):
        parser.add_argument("--event-id", action="append", required=True)

    def handle(self, *args, **options):
        if not flag("enabled") or not flag("attendance_enabled"):
            raise CommandError("Volunteer attendance ingestion is disabled.")
        event_ids = list(dict.fromkeys(options["event_id"]))
        if len(event_ids) > 20:
            raise CommandError("At most 20 explicit events may be processed per run.")
        service = LumaAttendeeReportService()
        linked_ids = CommunityChatDevice.objects.filter(status="verified").values(
            "user_id"
        )
        count = 0
        for event_id in event_ids:
            for guest in service.list_guests(event_id=event_id):
                data = (
                    guest.get("guest")
                    if isinstance(guest.get("guest"), dict)
                    else guest
                )
                email = str(data.get("user_email") or "").strip()
                if not email:
                    continue
                user = (
                    get_user_model()
                    .objects.filter(
                        email__iexact=email,
                        email_verified_at__isnull=False,
                        is_active=True,
                        pk__in=linked_ids,
                    )
                    .first()
                )
                if (
                    user is not None
                    and record_luma_guest(user=user, event_id=event_id, guest=guest)
                    is not None
                ):
                    count += 1
        self.stdout.write(
            f"Processed {count} verified member check-ins. Registration alone earns nothing."
        )
