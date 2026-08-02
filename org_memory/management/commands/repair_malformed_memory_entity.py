import json

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from organizations.models import Organization
from org_memory.models import MemoryEntity
from org_memory.pilot_deployment import PilotDeploymentError, resolve_pilot_operator


REPAIR_SCHEMA_VERSION = "org-memory-malformed-entity-repair-v1"


def _relationship_snapshot(entity):
    return {
        "subject_claim_ids": [
            str(value)
            for value in entity.subject_claims.order_by("pk").values_list("pk", flat=True)
        ],
        "object_claim_ids": [
            str(value)
            for value in entity.object_claims.order_by("pk").values_list("pk", flat=True)
        ],
        "current_state_ids": [
            str(value)
            for value in entity.current_states.order_by("pk").values_list("pk", flat=True)
        ],
    }


def _entity_report(entity):
    return {
        "entity_id": str(entity.pk),
        "canonical_name": entity.canonical_name,
        "normalized_name": entity.normalized_name,
        "entity_type": entity.entity_type,
        "classification": entity.classification,
        "aliases": entity.aliases,
        "external_refs": entity.external_refs,
        "metadata": entity.metadata,
        "relationships": _relationship_snapshot(entity),
    }


class Command(BaseCommand):
    help = (
        "Preview, quarantine, or restore one exact malformed organisational-memory "
        "entity without deleting claims or source evidence."
    )

    def add_arguments(self, parser):
        parser.add_argument("--organization-domain", required=True)
        parser.add_argument("--entity-id", required=True)
        parser.add_argument("--operation-id", required=True)
        parser.add_argument("--reason")
        parser.add_argument("--operator-email")
        mutation = parser.add_mutually_exclusive_group()
        mutation.add_argument("--apply", action="store_true")
        mutation.add_argument("--restore", action="store_true")

    def handle(self, *args, **options):
        organization = Organization.objects.filter(
            domain__iexact=options["organization_domain"]
        ).first()
        if organization is None:
            raise CommandError("Organization does not exist.")
        operation_id = str(options["operation_id"] or "").strip()
        if not operation_id or len(operation_id) > 128:
            raise CommandError("--operation-id must contain at most 128 characters.")
        entity = MemoryEntity.objects.filter(
            organization=organization,
            pk=options["entity_id"],
        ).first()
        if entity is None:
            raise CommandError("The exact entity does not exist in this organization.")

        applying = bool(options["apply"])
        restoring = bool(options["restore"])
        operator = None
        if applying or restoring:
            operator_email = str(options.get("operator_email") or "").strip()
            if not operator_email:
                raise CommandError("--operator-email is required for a mutation.")
            try:
                operator = resolve_pilot_operator(organization, operator_email)
            except PilotDeploymentError as exc:
                raise CommandError(
                    "Operator lacks an active manage_sources capability."
                ) from exc

        report = {
            "schema_version": REPAIR_SCHEMA_VERSION,
            "organization_domain": organization.domain,
            "operation_id": operation_id,
            "mode": "restore" if restoring else "apply" if applying else "preview",
            "changed": False,
            "before": _entity_report(entity),
        }
        if not applying and not restoring:
            self.stdout.write(json.dumps(report, sort_keys=True))
            return

        with transaction.atomic():
            entity = MemoryEntity.objects.select_for_update().get(pk=entity.pk)
            metadata = dict(entity.metadata or {})
            repair = metadata.get("malformed_entity_repair")
            if restoring:
                if not isinstance(repair, dict) or repair.get("operation_id") != operation_id:
                    raise CommandError(
                        "The entity is not quarantined by the requested operation."
                    )
                previous_metadata = repair.get("previous_metadata")
                if not isinstance(previous_metadata, dict):
                    raise CommandError("The repair record does not contain restorable metadata.")
                entity.metadata = previous_metadata
                entity.save(update_fields=("metadata", "updated_at"))
                report["changed"] = True
            else:
                reason = str(options.get("reason") or "").strip()
                if not reason:
                    raise CommandError("--reason is required with --apply.")
                if metadata.get("retrieval_quarantined") is True:
                    if isinstance(repair, dict) and repair.get("operation_id") == operation_id:
                        report["idempotent"] = True
                    else:
                        raise CommandError("The entity is already retrieval-quarantined.")
                else:
                    snapshot = _relationship_snapshot(entity)
                    previous_metadata = dict(metadata)
                    metadata.update(
                        {
                            "retrieval_quarantined": True,
                            "malformed_entity_repair": {
                                "schema_version": REPAIR_SCHEMA_VERSION,
                                "operation_id": operation_id,
                                "reason": reason[:1000],
                                "operator_user_id": str(operator.pk),
                                "operator_email": operator.email,
                                "applied_at": timezone.now().isoformat(),
                                "previous_metadata": previous_metadata,
                                "relationship_snapshot": snapshot,
                            },
                        }
                    )
                    entity.metadata = metadata
                    entity.save(update_fields=("metadata", "updated_at"))
                    report["changed"] = True
            report["after"] = _entity_report(entity)
        self.stdout.write(json.dumps(report, sort_keys=True))
