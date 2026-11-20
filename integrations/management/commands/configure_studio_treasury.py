"""Explicit operator setup with a reviewed opening ledger; dry-run by default."""
from datetime import date
import json
from pathlib import Path
import re
from uuid import UUID

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from integrations.models import StudioTreasuryLedger


def validate(config):
    if not config.get("treasurers") or any(not re.fullmatch(r"[UW][A-Z0-9]+", value) for value in config["treasurers"]):
        raise ValueError("treasurers_required")
    UUID(config["linear_organization"])
    UUID(config["xero_connection_id"])
    date.fromisoformat(config["invoice_since"])
    if not config["customer_contacts"] or not config["builders"] or not config["projects"]:
        raise ValueError("source_scope_required")
    for builder, identity in config["builders"].items():
        UUID(builder)
        UUID(identity["xero_contact_id"])
        if not identity["senders"] or not re.fullmatch(r"[UW][A-Z0-9]+", identity["slack_id"]):
            raise ValueError("builder_mapping_required")
        if any(not re.fullmatch(r"[A-Za-z0-9._+%-]+@[A-Za-z0-9.-]+", address) for address in identity["senders"]):
            raise ValueError("invalid_invoice_sender")
    for project, agreement in config["projects"].items():
        UUID(project)
        UUID(agreement["team_id"])
        if agreement["basis"] not in {"actual", "size"} or not agreement["version"] or not agreement["pool"]:
            raise ValueError("billing_agreement_required")
        if type(agreement["builder_rate_cents"]) is not int or agreement["builder_rate_cents"] <= 0:
            raise ValueError("builder_rate_required")
        date.fromisoformat(agreement["effective_from"])
        for binding in agreement.get("calendars", []):
            if not binding["calendar_id"] or not binding["title_patterns"]:
                raise ValueError("explicit_calendar_binding_required")
            for pattern in binding["title_patterns"]:
                re.compile(pattern)
    identities = set()
    for line in config["funding_lines"]:
        identity = (str(UUID(line["invoice_id"])), str(UUID(line["line_id"])))
        if identity in identities or line["basis"] not in {"actual", "size"} or line["currency"] != "AUD":
            raise ValueError("invalid_funding_line")
        identities.add(identity)
        if type(line["net_cents"]) is not int or line["net_cents"] <= 0 or line["hours"] <= 0 or not line["item_code"]:
            raise ValueError("customer_labour_conversion_required")
        if date.fromisoformat(line["end"]) < date.fromisoformat(line["start"]):
            raise ValueError("invalid_funding_period")


class Command(BaseCommand):
    help = "Validate Studio agreements and reviewed opening consumption. Writes require --apply."

    def add_arguments(self, parser):
        parser.add_argument("--file", required=True)
        parser.add_argument("--apply", action="store_true")
        parser.add_argument("--verify-opening", action="store_true")

    def handle(self, *args, **options):
        try:
            document = json.loads(Path(options["file"]).read_text())
            if type(document["organization"]) is not int or document["organization"] <= 0:
                raise ValueError("organization_database_id_required")
            if not re.fullmatch(r"T[A-Z0-9]+", document["workspace"]):
                raise ValueError("invalid_workspace")
            validate(document["configuration"])
            history = document.get("opening_reservations", {})
            for item in history.values():
                if item["status"] != "paid" or not item["receipt"]["id"] or not item["invoices"]:
                    raise ValueError("opening_payment_evidence_required")
                for row in item["allocations"]:
                    if type(row["units"]) is not int or row["units"] <= 0 or type(row["cents"]) is not int or row["cents"] < 0:
                        raise ValueError("invalid_opening_consumption")
            if options["verify_opening"] and not document.get("opening_evidence"):
                raise ValueError("opening_reconciliation_evidence_required")
        except (ValueError, TypeError, KeyError, OSError) as exc:
            raise CommandError("Studio setup validation failed: " + str(exc)) from exc
        if not options["apply"]:
            self.stdout.write("Studio configuration validated. No state changed.")
            return
        with transaction.atomic():
            row, created = StudioTreasuryLedger.objects.select_for_update().get_or_create(
                organization_id=document["organization"], defaults={"workspace_id": document["workspace"]})
            if row.workspace_id != document["workspace"] or not created and history:
                raise CommandError("Existing ledger history must be reconciled through adjustments, not replaced.")
            row.configuration = document["configuration"]
            if created:
                row.reservations = history
            if options["verify_opening"]:
                row.opening_verified_at = timezone.now()
                row.opening_evidence = document["opening_evidence"]
            row.save()
        self.stdout.write("Studio configuration saved. Treasury remains controlled by the environment switches.")
