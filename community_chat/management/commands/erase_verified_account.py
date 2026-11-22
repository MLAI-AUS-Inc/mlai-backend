"""Preview or finish one externally verified whole-account deletion."""

import json
from uuid import UUID

from django.core.management.base import BaseCommand, CommandError

from community_chat.deletion_account import AccountErasureBlocked, finish_account


class Command(BaseCommand):
    help = "Preview or remove a verified whole account after reviewing its dependencies."

    def add_arguments(self, parser):
        parser.add_argument("request_id", type=UUID)
        parser.add_argument("--apply", action="store_true")

    def handle(self, *args, **options):
        try:
            result = finish_account(options["request_id"], apply=options["apply"])
        except AccountErasureBlocked as error:
            report = {"status": "needs_attention", "reason": str(error), "dependencies": error.dependencies}
            raise CommandError(json.dumps(report, sort_keys=True)) from None
        self.stdout.write(json.dumps(result, sort_keys=True))
