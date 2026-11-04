"""Manual, rollback-first entry point for the daily read-only-source rating job."""

import json

from django.core.management.base import BaseCommand

from community_chat.admin_access import require_mlai_admin
from core.models import User
from crm.engagement_refresh import refresh_ratings


class Command(BaseCommand):
    help = "Refresh engagement ratings only; no permission changes or email sending. Defaults to dry-run."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true")
        parser.add_argument("--actor-email", required=True)
        parser.add_argument("--max-seconds", type=int, default=35)

    def handle(self, *args, **options):
        actor = User.objects.filter(email__iexact=options["actor_email"].strip()).first()
        require_mlai_admin(actor)
        result = refresh_ratings(
            dry_run=not options["apply"], max_seconds=max(1, min(60, options["max_seconds"])),
        )
        self.stdout.write(json.dumps(result, sort_keys=True))
