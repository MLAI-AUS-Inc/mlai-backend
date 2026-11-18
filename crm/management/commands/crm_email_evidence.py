"""Read the local CRM and system-email ledgers without sending or altering mail."""

import json
from datetime import date

from django.core.management.base import BaseCommand, CommandError
from django.core.serializers.json import DjangoJSONEncoder
from django.utils import timezone

from crm.deliverability import evidence


class Command(BaseCommand):
    help = 'Summarise email evidence for up to 31 Melbourne dates.'

    def add_arguments(self, parser):
        parser.add_argument('--from-day', required=True, type=date.fromisoformat)
        parser.add_argument('--through-day', required=True, type=date.fromisoformat)

    def handle(self, *args, **options):
        try:
            report = evidence.report(options['from_day'], options['through_day'], timezone.now())
        except ValueError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(json.dumps(report, cls=DjangoJSONEncoder))
