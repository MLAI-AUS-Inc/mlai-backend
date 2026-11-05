"""Recover instant booking operations without changing schema."""
import json
from django.core.management.base import BaseCommand
from mentoring.instant_booking import process_due_bookings


class Command(BaseCommand):
    help = 'Process a bounded batch of mentoring calendar, refund and settlement work.'

    def add_arguments(self, parser):
        parser.add_argument('--limit', type=int, default=25)

    def handle(self, *args, **options):
        self.stdout.write(json.dumps(process_due_bookings(options['limit'])))
