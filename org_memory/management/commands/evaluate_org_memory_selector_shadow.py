import json

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from organizations.models import Organization
from org_memory.selector_shadow import (
    LearnedMemorySelectorV2,
    SelectorShadowError,
    run_selector_shadow,
)


class Command(BaseCommand):
    help = (
        "Compare a strict local learned-selector artifact with authorised "
        "production traces without changing production ranking."
    )

    def add_arguments(self, parser):
        parser.add_argument("--organization-domain", required=True)
        parser.add_argument("--artifact", required=True)
        parser.add_argument("--limit", type=int)

    def handle(self, *args, **options):
        if not settings.ORG_MEMORY_SELECTOR_SHADOW_ENABLED:
            raise CommandError("Selector shadow evaluation is disabled.")
        try:
            organization = Organization.objects.get(
                domain__iexact=options["organization_domain"]
            )
        except Organization.DoesNotExist as exc:
            raise CommandError("Organization does not exist.") from exc
        try:
            selector = LearnedMemorySelectorV2.from_path(options["artifact"])
            run = run_selector_shadow(
                organization=organization,
                selector=selector,
                limit=options.get("limit"),
            )
        except (OSError, SelectorShadowError) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(
            json.dumps(
                {
                    "run_id": str(run.pk),
                    "status": run.status,
                    "eligible_trace_count": run.eligible_trace_count,
                    "labeled_trace_count": run.labeled_trace_count,
                    "evaluated_trace_count": run.evaluated_trace_count,
                    "error_code": run.error_code,
                    "metrics": run.metrics,
                },
                sort_keys=True,
            )
        )
