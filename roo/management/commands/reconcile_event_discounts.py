"""Reconcile durable event coupon operations without inventing new redemptions."""

import time

from django.core.management.base import BaseCommand, CommandError
from django.db import DatabaseError

from roo.event_discounts import process_pending
from roo.models import EventDiscountRedemption


class Command(BaseCommand):
    """Run one bounded batch, a supervised loop, or a read-only status report."""

    help = "Reconcile confirmed Roo event-discount redemptions. No new requests are created."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=1)
        parser.add_argument("--loop", action="store_true", help="Run under an operator's process supervisor.")
        parser.add_argument("--interval", type=int, default=30)
        parser.add_argument("--status", action="store_true", help="Read aggregate pending/review counts only; perform no provider calls.")

    def handle(self, *args, **options):
        if not 1 <= options["limit"] <= 100 or not 5 <= options["interval"] <= 300:
            raise CommandError("Use limit 1..100 and interval 5..300 seconds.")
        if options["status"]:
            rows = EventDiscountRedemption.objects.filter(status="processing")
            self.stdout.write(f"pending={rows.count()} needs_review={rows.filter(needs_review=True).count()}")
            return
        while True:
            try:
                processed = process_pending(limit=options["limit"])
                self.stdout.write(f"Event discount reconciliation: {processed} attempts.")
            except DatabaseError:
                # Never log provider responses, member details, codes or SQL.
                self.stderr.write("Event discount database unavailable; durable leases will be retried.")
                if not options["loop"]:
                    raise CommandError("Event discount reconciliation could not finish.") from None
            if not options["loop"]:
                return
            time.sleep(options["interval"])
