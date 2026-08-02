import json

from django.core.management.base import BaseCommand, CommandError

from organizations.models import Organization
from org_memory.extraction import (
    configured_extraction_target,
    schedule_source_extraction,
)
from org_memory.extraction_health import eligible_source_versions


class Command(BaseCommand):
    help = (
        "Preview or schedule bounded semantic re-extraction of current authorised "
        "source versions for the configured extraction target."
    )

    def add_arguments(self, parser):
        parser.add_argument("--organization-domain", required=True)
        parser.add_argument("--provider")
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
        target = configured_extraction_target()
        versions = list(
            eligible_source_versions(
                organization=organization,
                provider=options.get("provider"),
            ).order_by("captured_at", "pk")[:limit]
        )
        result = {
            "schema_version": "org-memory-reextraction-schedule-v1",
            "organization_domain": organization.domain,
            "provider": options.get("provider") or "all",
            "apply": bool(options["apply"]),
            "eligible": len(versions),
            "scheduled": 0,
            "existing": 0,
            "skipped": 0,
            "target_fingerprint": target.fingerprint,
        }
        if options["apply"]:
            for version in versions:
                scheduled = schedule_source_extraction(
                    source_version=version,
                    target=target,
                )
                for key in ("scheduled", "existing", "skipped"):
                    result[key] += int(scheduled.get(key) or 0)
        self.stdout.write(json.dumps(result, sort_keys=True))
