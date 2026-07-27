import json

from django.core.management.base import BaseCommand, CommandError

from org_memory.runtime import MemoryRuntimeError, schedule_memory_cycle


class Command(BaseCommand):
    help = "Recover leases, dispatch outbox/actions, and enqueue due connection syncs."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=100)
        parser.add_argument(
            "--configuration",
            default="",
            help="Restrict scheduling to one memory connection UUID.",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Schedule the selected active connection even when it is not due.",
        )
        parser.add_argument(
            "--organization-id",
            type=int,
            help="Restrict daily reconciliation reports to one organisation.",
        )
        parser.add_argument(
            "--force-daily",
            action="store_true",
            help="Run today's daily reconciliation even before its configured hour.",
        )
        parser.add_argument(
            "--skip-daily",
            action="store_true",
            help="Skip daily reconciliation/report coordination for this scheduler tick.",
        )

    def handle(self, *args, **options):
        if options["limit"] < 1:
            raise CommandError("--limit must be positive.")
        if options["force"] and not options["configuration"]:
            raise CommandError("--force requires --configuration.")
        if options["force_daily"] and options["skip_daily"]:
            raise CommandError("--force-daily and --skip-daily cannot be combined.")
        if options["configuration"] and options["organization_id"]:
            raise CommandError(
                "--organization-id cannot be combined with --configuration."
            )
        try:
            result = schedule_memory_cycle(
                limit=options["limit"],
                configuration_id=options["configuration"] or None,
                force=options["force"],
                organization_id=options["organization_id"],
                run_daily=not options["skip_daily"],
                force_daily=options["force_daily"],
            )
        except (MemoryRuntimeError, ValueError) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(json.dumps(result, sort_keys=True, default=str))
