import json

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from organizations.models import Organization
from org_memory.selector_shadow import (
    SelectorShadowError,
    build_selector_dataset,
    write_selector_dataset,
)


class Command(BaseCommand):
    help = (
        "Export a pseudonymised, currently authorised organisational-memory "
        "selector dataset. Disabled by default."
    )

    def add_arguments(self, parser):
        parser.add_argument("--organization-domain", required=True)
        parser.add_argument("--output", required=True)
        parser.add_argument("--limit", type=int)
        parser.add_argument("--overwrite", action="store_true")

    def handle(self, *args, **options):
        if not settings.ORG_MEMORY_SELECTOR_EXPORT_ENABLED:
            raise CommandError("Selector dataset export is disabled.")
        try:
            organization = Organization.objects.get(
                domain__iexact=options["organization_domain"]
            )
        except Organization.DoesNotExist as exc:
            raise CommandError("Organization does not exist.") from exc
        try:
            dataset = build_selector_dataset(
                organization=organization,
                limit=options.get("limit"),
            )
            destination = write_selector_dataset(
                dataset,
                options["output"],
                overwrite=options["overwrite"],
            )
        except SelectorShadowError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(
            json.dumps(
                {
                    "dataset_hash": dataset.dataset_hash,
                    "eligible_trace_count": dataset.manifest[
                        "eligible_trace_count"
                    ],
                    "labeled_trace_count": dataset.manifest[
                        "labeled_trace_count"
                    ],
                    "excluded_trace_count": dataset.manifest[
                        "excluded_trace_count"
                    ],
                    "output": str(destination),
                },
                sort_keys=True,
            )
        )
