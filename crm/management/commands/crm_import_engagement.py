"""Operator entry point for the reviewed contact engagement snapshot."""

import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from core.models import User
from crm.engagement_import import import_snapshot, load_permission_evidence, load_snapshot


class Command(BaseCommand):
    help = "Import reviewed ratings and contact evidence, preserving all delivery preferences. Defaults to dry run."

    def add_arguments(self, parser):
        parser.add_argument("path")
        parser.add_argument("--expected-sha256", required=True)
        parser.add_argument("--actor-email", required=True)
        parser.add_argument("--permission-evidence")
        parser.add_argument("--apply", action="store_true", help="Persist the reviewed import; otherwise roll back.")

    def handle(self, *args, **options):
        try:
            rows, digest = load_snapshot(options["path"], expected_sha256=options["expected_sha256"])
            evidence = load_permission_evidence(options["permission_evidence"])
            actor = User.objects.filter(email__iexact=options["actor_email"].strip()).first()
            if actor is None:
                raise ValueError("The actor must be an existing MLAI admin account.")
            result = import_snapshot(
                rows, digest=digest, file_name=Path(options["path"]).name,
                evidence_by_email=evidence, actor=actor, dry_run=not options["apply"],
            )
        except (ValueError, OSError, UnicodeError) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(json.dumps(result, sort_keys=True))
