"""Plan, activate and inspect the finite consent-based sender rollout."""

import json
from datetime import date

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from crm.community_optins import sync_community_optins
from crm.warmup import activate_run, plan_run, run_warmup_scheduler, status_report


class Command(BaseCommand):
    help = "Import proven community opt-ins and manage a finite 21-day email rollout."

    def add_arguments(self, parser):
        parser.add_argument("action", choices=["import", "plan", "activate", "status", "tick"])
        parser.add_argument("--run-id", default="")
        parser.add_argument("--start-date", type=date.fromisoformat)
        parser.add_argument("--actor-email", default="")
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        try:
            action, run_id = options["action"], options["run_id"]
            actor = get_user_model().objects.filter(email__iexact=options["actor_email"]).first()
            if action == "import":
                from community_chat.admin_access import require_mlai_admin
                require_mlai_admin(actor)
                result = sync_community_optins(actor=actor, dry_run=options["dry_run"])
            elif action == "plan":
                if not options["start_date"]:
                    raise ValueError("start_date_required")
                result = plan_run(run_id, options["start_date"], actor=actor, dry_run=options["dry_run"])
            elif action == "activate":
                result = activate_run(run_id)
            elif action == "tick":
                result = run_warmup_scheduler()
            else:
                result = status_report(run_id)
        except (ValueError, PermissionError) as exc:
            raise CommandError(str(exc)) from None
        self.stdout.write(json.dumps(result, sort_keys=True))
