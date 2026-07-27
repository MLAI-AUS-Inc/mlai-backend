import json

from django.core.management.base import BaseCommand, CommandError

from org_memory.pilot_deployment import pilot_release_gate_report


class Command(BaseCommand):
    help = (
        "Fail a private-query release unless its exact organization has a "
        "current, key-matched pilot binding. This command performs no writes."
    )

    def add_arguments(self, parser):
        parser.add_argument("--organization-domain")
        parser.add_argument("--require-active", action="store_true")

    def handle(self, *args, **options):
        report = pilot_release_gate_report(
            organization_domain=options.get("organization_domain"),
            require_active=bool(options["require_active"]),
        )
        self.stdout.write(json.dumps(report, sort_keys=True))
        if not report["ready"]:
            raise CommandError("Admin Brain pilot release gate has blockers.")
