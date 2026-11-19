"""Print the staff-only list of look-alike startups across accounts (plan E5).

Read-only. Prints one JSON object per group (``--format json``, the default)
or a short table. It never changes, merges or archives anything and never
contacts a founder.
"""
import json

from django.core.management.base import BaseCommand

from founder_tools.startup_lookalikes import cross_account_lookalikes


class Command(BaseCommand):
    help = "List startups in different accounts that share a site or a name (read-only, staff only)."

    def add_arguments(self, parser):
        parser.add_argument("--format", choices=("json", "table"), default="json")

    def handle(self, *args, **options):
        groups = cross_account_lookalikes()
        for group in groups:
            if options["format"] == "json":
                self.stdout.write(json.dumps(group, sort_keys=True))
                continue
            self.stdout.write(f"{group['match']}: {group['key']}")
            for company in group["companies"]:
                self.stdout.write(f"  org {company['organizationId']}  profile {company['profileId']}  "
                                  f"{company['name']}  {company['domain'] or '-'}")
        self.stderr.write(f"{len(groups)} look-alike group(s); nothing was changed.")
