"""Qualify campaign behavior using synthetic providers in a disposable test database."""

import json

from django.core.management.base import BaseCommand

from crm.deliverability.simulation import SCENARIOS, run


class Command(BaseCommand):
    help = 'Run a synthetic campaign scenario. Requires an empty, explicitly named disposable test database; rolls back all rows.'

    def add_arguments(self, parser):
        parser.add_argument('--scenario', required=True, choices=SCENARIOS)
        parser.add_argument('--recipients', type=int, default=5000)
        parser.add_argument('--days', type=int, default=8)

    def handle(self, *args, **options):
        result = run(options['scenario'], recipients=options['recipients'], days=options['days'])
        self.stdout.write(json.dumps(result, sort_keys=True))
