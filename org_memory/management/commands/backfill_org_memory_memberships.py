import json
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from organizations.models import Organization
from org_memory.membership_backfill import (
    apply_reviewed_membership_report,
    build_membership_candidate_report,
    load_report,
)


class Command(BaseCommand):
    help = (
        "Generate reviewed membership candidates from legacy bindings, or apply an "
        "explicitly approved candidate report."
    )

    def add_arguments(self, parser):
        parser.add_argument("--organization-domain", required=True)
        parser.add_argument("--output", help="Write the generated JSON report to this path")
        parser.add_argument("--reviewed-input", help="Apply an explicitly reviewed JSON report")
        parser.add_argument("--reviewed-by", help="Email of the active staff reviewer")

    def handle(self, *args, **options):
        try:
            organization = Organization.objects.get(domain=options["organization_domain"])
        except Organization.DoesNotExist as exc:
            raise CommandError("Organization does not exist") from exc

        reviewed_input = options.get("reviewed_input")
        reviewed_by = options.get("reviewed_by")
        if reviewed_input:
            if not reviewed_by:
                raise CommandError("--reviewed-by is required with --reviewed-input")
            try:
                reviewer = get_user_model().objects.get(email__iexact=reviewed_by)
            except get_user_model().DoesNotExist as exc:
                raise CommandError("Reviewer does not exist") from exc
            result = apply_reviewed_membership_report(
                organization=organization,
                report=load_report(reviewed_input),
                reviewer=reviewer,
            )
            self.stdout.write(json.dumps(result, sort_keys=True))
            return

        report = build_membership_candidate_report(organization)
        rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
        output = options.get("output")
        if output:
            path = Path(output).expanduser().resolve()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(rendered, encoding="utf-8")
            self.stdout.write(f"Wrote {len(report['candidates'])} candidates to {path}")
        else:
            self.stdout.write(rendered.rstrip())
