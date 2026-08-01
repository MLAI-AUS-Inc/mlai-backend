import json

from django.core.management.base import BaseCommand, CommandError

from organizations.models import Organization
from org_memory.extraction_health import (
    reconcile_legacy_extraction_dead_letters,
)
from org_memory.models import OrganizationMembership


class Command(BaseCommand):
    help = (
        "Preview or reconcile bounded superseded extraction dead letters by scheduling "
        "the current extraction target."
    )

    def add_arguments(self, parser):
        parser.add_argument("--organization-domain", required=True)
        parser.add_argument("--provider", required=True)
        parser.add_argument("--superseded-schema-version")
        parser.add_argument("--superseded-extractor-version")
        parser.add_argument("--superseded-prompt-version")
        parser.add_argument("--operator-email")
        parser.add_argument("--limit", type=int, default=1000)
        parser.add_argument("--apply", action="store_true")

    def handle(self, *args, **options):
        organization = Organization.objects.filter(
            domain__iexact=options["organization_domain"]
        ).first()
        if organization is None:
            raise CommandError("Organization does not exist.")
        limit = int(options["limit"])
        if not 1 <= limit <= 10000:
            raise CommandError("--limit must be between 1 and 10000.")
        superseded_versions = {
            "superseded_schema_version": options.get("superseded_schema_version"),
            "superseded_extractor_version": options.get("superseded_extractor_version"),
            "superseded_prompt_version": options.get("superseded_prompt_version"),
        }
        if not any(superseded_versions.values()):
            raise CommandError(
                "At least one superseded schema, extractor, or prompt version is required."
            )
        operator = None
        if options["apply"]:
            operator_email = str(options.get("operator_email") or "").strip()
            if not operator_email:
                raise CommandError("--operator-email is required with --apply.")
            membership = OrganizationMembership.objects.select_related("user").filter(
                organization=organization,
                user__email__iexact=operator_email,
            ).first()
            if membership is None:
                raise CommandError("Operator is not a member of the organization.")
            operator = membership.user
        try:
            report = reconcile_legacy_extraction_dead_letters(
                organization=organization,
                provider=options["provider"],
                **superseded_versions,
                apply=options["apply"],
                resolved_by=operator,
                limit=limit,
            )
        except ValueError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(json.dumps(report, sort_keys=True))
