import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from core.models import User
from crm.contact_import import ImportFileError, TARGETS, apply_import, read_csv
from crm.models import ContactImport


class Command(BaseCommand):
    help = (
        "Import contacts from a CSV file. Filled fields are never overwritten and "
        "unsubscribed, bounced or banned people are never re-subscribed. "
        "Run with --dry-run first to check the column mapping and counts."
    )

    def add_arguments(self, parser):
        parser.add_argument("path", help="CSV file with a header row and an email column.")
        parser.add_argument(
            "--consent",
            required=True,
            choices=ContactImport.Consent.values,
            help="'granted' only when everyone in the file opted in to MLAI email.",
        )
        parser.add_argument("--consent-source", default="", help="Where the opt-ins came from.")
        parser.add_argument("--tag", action="append", default=[], help="Tag every imported contact; repeatable.")
        parser.add_argument(
            "--map",
            action="append",
            default=[],
            metavar="HEADER=FIELD",
            help=f"Override a column mapping. FIELD is one of: {', '.join(TARGETS)}.",
        )
        parser.add_argument("--delimiter", default=",")
        parser.add_argument("--encoding", default="utf-8-sig")
        parser.add_argument("--actor-email", help="MLAI account recorded as the importer.")
        parser.add_argument("--dry-run", action="store_true", help="Report counts and roll back.")

    def handle(self, *args, **options):
        path = Path(options["path"])
        if not path.is_file():
            raise CommandError(f"{path} is not a file.")
        overrides = {}
        for item in options["map"]:
            header, separator, target = item.partition("=")
            if not separator or not header.strip():
                raise CommandError(f"--map {item!r} must look like HEADER=FIELD.")
            overrides[header.strip()] = target.strip()

        actor = None
        if options["actor_email"]:
            actor = User.objects.filter(email__iexact=options["actor_email"].strip()).first()
            if actor is None:
                raise CommandError("--actor-email does not match an MLAI account.")

        try:
            with path.open(newline="", encoding=options["encoding"]) as handle:
                plan, parsed = read_csv(handle, delimiter=options["delimiter"], overrides=overrides)
        except (ImportFileError, UnicodeDecodeError) as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write("Column mapping:")
        for header, target in plan.items():
            self.stdout.write(f"  {header!r} -> {target}")

        record = apply_import(
            parsed,
            file_name=path.name,
            consent=options["consent"],
            consent_source=options["consent_source"],
            tags=options["tag"],
            actor=actor,
            dry_run=options["dry_run"],
        )
        self.stdout.write(
            json.dumps(
                {
                    "import_id": str(record.pk),
                    "dry_run": record.dry_run,
                    "status": record.status,
                    "counts": record.counts,
                    "error_sample": record.error_sample,
                },
                sort_keys=True,
            )
        )
