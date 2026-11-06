"""Report persisted public-channel membership evidence without Slack calls."""
import json
import math
import re

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from integrations.services.slack_member_coverage import load_coverage


class Command(BaseCommand):
    help = "Read-only JSON report of member scopes and persisted community-channel membership evidence."
    requires_system_checks = []

    def add_arguments(self, parser):
        parser.add_argument("--workspace", required=True, help="Exact Slack workspace ID; workspaces are never combined.")
        parser.add_argument("--max-age-hours", type=float, default=24,
                            help="Maximum age of inventory membership observations (default: 24 hours).")

    def handle(self, *args, **options):
        workspace = options["workspace"].strip()
        max_age = options["max_age_hours"]
        if not re.fullmatch(r"T[A-Z0-9]{1,99}", workspace):
            raise CommandError("Supply a valid Slack workspace ID with --workspace.")
        if not math.isfinite(max_age) or not 0 < max_age <= 24 * 365:
            raise CommandError("--max-age-hours must be positive and at most 8760.")
        report = load_coverage(workspace_id=workspace, now=timezone.now(), max_age_hours=max_age)
        self.stdout.write(json.dumps(report, indent=2, sort_keys=True))
