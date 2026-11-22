"""Process an expiring queue of verified deletion completion notices."""

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from community_chat.deletion_notice import claim_notice, deliver_notice, expire_notices
from system_email import transport_resend


class Command(BaseCommand):
    help = "Expire old completion-notice addresses and deliver notices for verified cases."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=20)

    def handle(self, *args, **options):
        limit = options["limit"]
        if not 1 <= limit <= 100:
            raise CommandError("limit must be between 1 and 100")
        expired = expire_notices(timezone.now(), limit=limit)
        self.stdout.write(f"Expired completion notice recipients: {expired}")
        if not settings.COMMUNITY_CHAT_ACCOUNT_DELETION_NOTICE_ENABLED:
            return
        if not transport_resend.api_key() or not transport_resend.from_address():
            raise CommandError("The completion notice provider is unavailable.")
        processed = 0
        for _ in range(limit):
            notice = claim_notice(timezone.now())
            if notice is None:
                break
            processed += deliver_notice(notice)
        self.stdout.write(f"Processed completion notices: {processed}")
