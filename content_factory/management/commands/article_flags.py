"""Print the effective value of every founder Articles setting (read-only)."""

import json

from django.core.management.base import BaseCommand

from content_factory.article_flags import flag_rows


class Command(BaseCommand):
    """One line per setting: name, effective value, env or code default, and whether a release can set it."""

    help = "Print every ARTICLES_*, MY_STARTUP_*, CONTENT_FACTORY_* and WEBSITE_* setting's effective value. Secrets show only set or unset."

    def add_arguments(self, parser):
        parser.add_argument("--json", action="store_true", help="Print a JSON list instead of aligned text.")
        parser.add_argument("--only-flags", action="store_true", help="Only true/false switches.")

    def handle(self, *args, **options):
        rows = flag_rows()
        if options["only_flags"]:
            rows = [row for row in rows if row["value"] in {"true", "false"}]
        if options["json"]:
            self.stdout.write(json.dumps(rows, indent=2))
            return
        width = max((len(row["name"]) for row in rows), default=0)
        for row in rows:
            deploy = "deploy" if row["deploy"] else "host-only"
            self.stdout.write(f"{row['name']:<{width}}  {row['value']:<7}  {row['source']:<7}  {deploy}")
