"""Fill empty seed keywords from each org's name and description (plan E9). Dry-run by default.

Never run against production without the owner's go-ahead for that run. First
confirm the production value of MY_STARTUP_ONE_FIELD_START_ENABLED (plan A1):
while it is off, new-journey orgs also get no first-profile seeds.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from content_factory.seed_backfill import apply_plan, plan_for
from organizations.models import Organization


class Command(BaseCommand):
    help = "Fill empty Organization.seed_keywords from the saved name and description. Dry-run unless --commit."

    def add_arguments(self, parser):
        parser.add_argument("--org-id", type=int, action="append", default=[],
                            help="Only this organisation (repeatable).")
        parser.add_argument("--limit", type=int, default=0, help="Stop after this many organisations (0: all).")
        parser.add_argument("--commit", action="store_true", help="Write the seeds. Without it nothing is written.")

    def handle(self, *args, **options):
        commit = bool(options["commit"])
        queryset = Organization.objects.order_by("pk")
        if options["org_id"]:
            queryset = queryset.filter(pk__in=options["org_id"])
        if options["limit"]:
            queryset = queryset[: options["limit"]]
        counts = {"fill": 0, "has_seeds": 0, "nothing_to_derive": 0, "written": 0}
        self.stdout.write(f"{'Committing' if commit else 'Dry-run'} seed keyword backfill.")
        for organization in queryset.iterator():
            plan = plan_for(organization)
            counts[plan.outcome] += 1
            if plan.outcome == "has_seeds":
                continue
            line = f"  org {plan.organization_id} ({plan.domain}): {plan.outcome}"
            if plan.outcome == "fill":
                line += f" from {', '.join(plan.sources)} -> {plan.seeds}"
                if commit and apply_plan(plan):
                    counts["written"] += 1
                    line += " [written]"
            else:
                line += " (no name or description; research will ask for seed topics)"
            self.stdout.write(line)
        self.stdout.write(
            f"Summary: {counts['fill']} to fill, {counts['written']} written, "
            f"{counts['nothing_to_derive']} with nothing to derive, {counts['has_seeds']} already seeded."
        )
        if not commit:
            self.stdout.write("Dry-run: nothing was written. Re-run with --commit to write.")
