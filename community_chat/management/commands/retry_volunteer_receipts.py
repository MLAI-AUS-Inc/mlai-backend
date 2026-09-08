"""Retry durable reward receipts; never create source evidence from telemetry."""

from django.core.management.base import BaseCommand

from community_chat.volunteer.access import community_id, flag
from community_chat.volunteer.models import VolunteerSourceReceipt
from community_chat.volunteer.receipts import process_receipt


class Command(BaseCommand):
    help = "Retry pending Volunteer receipts; awards still require server flags."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=100)

    def handle(self, *args, **options):
        if not flag("enabled"):
            self.stdout.write("Volunteer is disabled; no receipts processed.")
            return
        count = 0
        for receipt in VolunteerSourceReceipt.objects.filter(
            community=community_id(), status="pending"
        ).order_by("created_at")[: min(max(options["limit"], 1), 1000)]:
            process_receipt(receipt)
            count += 1
        self.stdout.write(f"Processed {count} pending receipts.")
