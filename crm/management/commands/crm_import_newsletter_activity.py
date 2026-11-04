"""Checksum-bound, rollback-first import of explicit newsletter click dates."""

import csv
import json

from django.core.management.base import BaseCommand, CommandError

from community_chat.admin_access import require_mlai_admin
from core.models import User
from crm.newsletter_activity_import import import_newsletter_activity


class Command(BaseCommand):
    help = "Import dated newsletter activity only, preserving all preferences. Defaults to rollback."

    def add_arguments(self, parser):
        parser.add_argument("path")
        parser.add_argument("--expected-sha256", required=True)
        parser.add_argument("--exported-at", required=True)
        parser.add_argument("--actor-email", required=True)
        parser.add_argument("--apply", action="store_true")

    def handle(self, *args, **options):
        actor = User.objects.filter(email__iexact=options["actor_email"].strip()).first()
        require_mlai_admin(actor)
        try:
            result = import_newsletter_activity(
                options["path"], expected_sha256=options["expected_sha256"],
                exported_at=options["exported_at"], actor=actor, dry_run=not options["apply"],
            )
        except (ValueError, OSError, UnicodeError, csv.Error) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(json.dumps(result, sort_keys=True))
