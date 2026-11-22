"""Review and resolve account-erasure dependencies."""

import json
from uuid import UUID

from django.core.management.base import BaseCommand, CommandError

from community_chat.deletion_inventory import related_record_counts
from community_chat.deletion_startup import handoff_startup
from community_chat.models import AccountDeletionRequest


class Command(BaseCommand):
    help = "List account relationships for a deletion case or hand off one shared startup."

    def add_arguments(self, parser):
        parser.add_argument("request_id", type=UUID)
        parser.add_argument("--company", type=UUID)
        parser.add_argument("--successor", type=int)
        parser.add_argument("--apply", action="store_true")

    def handle(self, *args, **options):
        case = AccountDeletionRequest.objects.filter(pk=options["request_id"]).first()
        if (
            case is None
            or case.scope != AccountDeletionRequest.Scope.ACCOUNT
            or case.user_id is None
            or case.status == AccountDeletionRequest.Status.COMPLETED
        ):
            raise CommandError("An open whole-account deletion case is required.")
        company = options["company"]
        successor = options["successor"]
        if company is None and successor is None and not options["apply"]:
            self.stdout.write(json.dumps(related_record_counts(case.user_id), sort_keys=True))
            return
        if company is None or successor is None:
            raise CommandError("Supply both a company and an approved successor.")
        try:
            result = handoff_startup(
                request_id=case.pk, company_id=company, successor_id=successor, apply=options["apply"],
            )
        except ValueError as error:
            raise CommandError(str(error)) from error
        self.stdout.write(result)
