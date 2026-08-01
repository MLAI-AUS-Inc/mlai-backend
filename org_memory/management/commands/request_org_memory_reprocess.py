import json
from types import SimpleNamespace

from django.core.management.base import BaseCommand, CommandError

from organizations.models import Organization
from org_memory.control_plane import SourceControlError, request_runtime_action
from org_memory.models import (
    MemoryActionType,
    MemoryConnectionConfiguration,
    MemoryConnectionState,
    MemoryProvider,
    MemoryScopeStatus,
    OrganizationMembership,
)


class Command(BaseCommand):
    help = (
        "Preview or request one idempotent reprocess of an exact active "
        "organisational-memory connection."
    )

    def add_arguments(self, parser):
        parser.add_argument("--organization-domain", required=True)
        parser.add_argument("--provider", required=True, choices=MemoryProvider.values)
        parser.add_argument("--configuration-id", required=True)
        parser.add_argument("--idempotency-key", required=True)
        parser.add_argument("--operator-email")
        parser.add_argument("--apply", action="store_true")

    def handle(self, *args, **options):
        organization = Organization.objects.filter(
            domain__iexact=options["organization_domain"]
        ).first()
        if organization is None:
            raise CommandError("Organization does not exist.")
        configuration = (
            MemoryConnectionConfiguration.objects.filter(
                pk=options["configuration_id"],
                organization=organization,
                provider=options["provider"],
                lifecycle_state=MemoryConnectionState.ACTIVE,
                deleted_at__isnull=True,
                source_scopes__selected=True,
                source_scopes__status=MemoryScopeStatus.SELECTED,
            )
            .distinct()
            .first()
        )
        if configuration is None:
            raise CommandError(
                "The exact active connection with a selected scope does not exist."
            )

        idempotency_key = str(options["idempotency_key"] or "").strip()
        if not idempotency_key or len(idempotency_key) > 128:
            raise CommandError("--idempotency-key must contain at most 128 characters.")
        existing = configuration.action_requests.filter(
            idempotency_key=idempotency_key
        ).first()
        report = {
            "schema_version": "org-memory-reprocess-request-v1",
            "organization_domain": organization.domain,
            "provider": configuration.provider,
            "configuration_id": str(configuration.pk),
            "selected_scope_count": configuration.source_scopes.filter(
                selected=True,
                status=MemoryScopeStatus.SELECTED,
            ).count(),
            "idempotency_key": idempotency_key,
            "apply": bool(options["apply"]),
            "created": False,
            "action_id": str(existing.pk) if existing else None,
            "action_status": existing.status if existing else None,
        }
        if not options["apply"]:
            self.stdout.write(json.dumps(report, sort_keys=True))
            return

        operator_email = str(options.get("operator_email") or "").strip()
        if not operator_email:
            raise CommandError("--operator-email is required with --apply.")
        membership = OrganizationMembership.objects.select_related("user").filter(
            organization=organization,
            user__email__iexact=operator_email,
        ).first()
        if membership is None or not membership.is_effective_at():
            raise CommandError("Operator is not an active member of the organization.")

        actor = SimpleNamespace(user=membership.user, identity=None)
        authorization = SimpleNamespace(membership=membership)
        try:
            action, created = request_runtime_action(
                configuration,
                action=MemoryActionType.REPROCESS,
                actor=actor,
                authorization=authorization,
                request_id=f"management-command:{idempotency_key}",
                idempotency_key=idempotency_key,
                scope_external_ids=[],
            )
        except SourceControlError as exc:
            raise CommandError(str(exc)) from exc
        report.update(
            {
                "created": created,
                "action_id": str(action.pk),
                "action_status": action.status,
            }
        )
        self.stdout.write(json.dumps(report, sort_keys=True))
