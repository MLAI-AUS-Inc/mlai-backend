"""Queue a reviewed, exact existing mirror; dry-run is the default."""

import json

from django.core.management.base import BaseCommand, CommandError

from integrations.services import slack_dm_mirror as dm
from integrations.services.slack_open_recovery import recover_existing_conversation
from integrations.services.slack_owner_inventory_api import InventoryError


class Command(BaseCommand):
    help = 'Review one unfinished Slack mirror and optionally queue its existing owner-open worker.'

    def add_arguments(self, parser):
        parser.add_argument('--grant-id', type=int, required=True)
        parser.add_argument('--device-id', type=int, required=True)
        parser.add_argument('--source-id', required=True)
        parser.add_argument('--apply', action='store_true')
        parser.add_argument('--expected-plan', help='Exact plan fingerprint returned by the reviewed dry run.')

    def handle(self, *args, **options):
        if options['grant_id'] < 1 or options['device_id'] < 1 or not options['source_id'].strip():
            raise CommandError('A positive grant/device ID and exact source ID are required.')
        if options['apply'] and not options['expected_plan']:
            raise CommandError('--apply requires --expected-plan from a reviewed dry run.')
        try:
            result = recover_existing_conversation(
                grant_id=options['grant_id'], device_id=options['device_id'],
                source_id=options['source_id'].strip(), apply=options['apply'],
                expected_plan=options['expected_plan'],
            )
        except InventoryError as exc:
            raise CommandError(exc.code) from exc
        except dm.SlackDmMirrorAuthorizationError as exc:
            raise CommandError('slack_authority_changed') from exc
        self.stdout.write(json.dumps(result, sort_keys=True))
