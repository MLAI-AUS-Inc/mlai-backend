"""Read-only: write a redacted replay record for one run (plan F5).

The record is a draft built from allowlisted fields only (see
``content_factory.replay_records``). Nothing is written to the database: on
PostgreSQL the read runs in ``SET TRANSACTION READ ONLY`` and on any engine
the transaction is rolled back. Running it against production needs an owner
go-ahead and ``--allow-production-read``; the output is reviewed before any
record is committed (owner decision D11).
"""
import json
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection as database, transaction

from content_factory.replay_records import RecordError, export_record


class Command(BaseCommand):
    help = "Read-only: print (or save) a redacted replay-record-v1 draft for one Content Factory run."

    def add_arguments(self, parser):
        parser.add_argument("--run-pk", type=int, required=True, help="Primary key of the ContentFactoryRun.")
        parser.add_argument("--output", type=str, default="", help="Write the record to this new file instead of stdout.")
        parser.add_argument("--allow-production-read", action="store_true",
                            help="Required outside local and test environments, after an owner go-ahead.")

    def handle(self, *args, **options):
        from workflow_runs.models import ContentFactoryRun
        if getattr(settings, "IS_PRODUCTION_ENV", True) and not options["allow_production_read"]:
            raise CommandError("Refusing to read outside a local or test environment without --allow-production-read.")
        output = Path(options["output"]) if options["output"] else None
        if output is not None and output.exists():
            raise CommandError("The output file already exists; choose a new path.")
        with transaction.atomic():
            if database.vendor == "postgresql":
                with database.cursor() as cursor:
                    cursor.execute("SET TRANSACTION READ ONLY")
            run = ContentFactoryRun.objects.filter(pk=options["run_pk"]).first()
            if run is None:
                raise CommandError("Run not found.")
            steps = list(run.steps.order_by("display_order", "pk")[:64])
            try:
                record = export_record(run, steps=steps)
            except RecordError as exc:
                raise CommandError(str(exc)) from exc
            transaction.set_rollback(True)
        text = json.dumps(record, indent=2, sort_keys=False) + "\n"
        if output is None:
            self.stdout.write(text, ending="")
        else:
            with output.open("x", encoding="utf-8") as handle:
                handle.write(text)
            self.stderr.write(f"Wrote {output}; review the redaction before committing it.")
