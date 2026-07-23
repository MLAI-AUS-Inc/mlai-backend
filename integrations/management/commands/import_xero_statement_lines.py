import json
import sys

from django.core.management.base import BaseCommand, CommandError

from integrations.services.xero_statement_reconciliation import import_xero_statement_lines
from organizations.models import Organization


class Command(BaseCommand):
    help = "Import a complete browser-observed Xero unreconciled statement queue from stdin."

    def add_arguments(self, parser):
        parser.add_argument("--domain", default="mlai.au")
        parser.add_argument("--bank-account-id", required=True)
        parser.add_argument("--currency", default="AUD")

    def handle(self, *args, **options):
        organization = Organization.objects.filter(domain__iexact=options["domain"]).first()
        if organization is None:
            raise CommandError(f"Unknown organisation domain: {options['domain']}")
        try:
            payload = json.load(sys.stdin)
        except (TypeError, ValueError) as exc:
            raise CommandError("stdin must contain a JSON list of statement lines") from exc
        try:
            saved = import_xero_statement_lines(
                organization=organization,
                bank_account_id=options["bank_account_id"],
                currency=options["currency"],
                lines=payload,
            )
        except ValueError as exc:
            raise CommandError(str(exc)) from exc
        ready = sum(1 for line in saved if line.ready_in_xero)
        self.stdout.write(self.style.SUCCESS(
            f"Imported {len(saved)} Xero statement lines ({ready} already ready, {len(saved) - ready} candidates)."
        ))
