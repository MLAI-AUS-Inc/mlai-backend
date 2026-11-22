"""Process one bounded batch from the startup invitation outbox."""
from django.core.management.base import BaseCommand

from founder_tools.startup_invite_delivery import process_due


class Command(BaseCommand):
    help = "Send a batch of committed startup invitations through transactional Resend."

    def handle(self, **options):
        count = process_due()
        if count:
            self.stdout.write(f"Processed {count} startup invitation deliveries.")
