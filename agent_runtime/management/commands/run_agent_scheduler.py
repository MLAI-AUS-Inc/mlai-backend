"""One optional control-plane scheduler; Hermes itself does not own these jobs."""

import json
import time

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import close_old_connections

from agent_runtime import actions, events, runtime
from agent_runtime.models import AgentActionProposal
from agent_runtime.policy import AgentPermissionDenied


def tick(*, schedule=False, fanout=False, deliver=False, webhooks=False, limit=50):
    if not getattr(settings, "AGENT_RUNTIME_ENABLED", False):
        raise CommandError("AGENT_RUNTIME_ENABLED must be explicitly enabled.")
    if deliver and not getattr(settings, "AGENT_RUNTIME_ACTIONS_ENABLED", False):
        raise CommandError("Action delivery is disabled.")
    from agent_runtime.sources import live_access
    result = {"recovered_tasks": runtime.recover_tasks(limit=limit),
              "unknown_actions": actions.recover_actions(limit=limit),
              "scheduled": 0, "event_tasks": 0, "webhooks": 0, "sent": 0, "unconfirmed": 0, "blocked": 0}
    if schedule:
        result["scheduled"] = runtime.schedule_due_responsibilities(access_checker=live_access, limit=limit)
    if fanout:
        result["event_tasks"] = events.dispatch_subscriptions(access_checker=live_access, limit=limit)
    if webhooks:
        from agent_runtime.event_webhooks import dispatch_webhooks
        result["webhooks"] = dispatch_webhooks(limit=limit)
    if deliver:
        from agent_runtime.sources import execute_approved_action
        ids = list(AgentActionProposal.objects.filter(status="approved").order_by("created_at").values_list("pk", flat=True)[:limit])
        for action_id in ids:
            try:
                claimed = actions.claim_action(action_id, access_checker=live_access)
                outcome = actions.deliver_claimed_action(action_id, lease_token=claimed.lease_token,
                                                          executor=execute_approved_action, access_checker=live_access)
            except (AgentPermissionDenied, runtime.AgentRuntimeError):
                result["blocked"] += 1
                continue
            result["sent" if outcome.status == "sent" else "unconfirmed"] += 1
    return result


class Command(BaseCommand):
    help = "Recover agent work and optionally schedule, fan out events, or deliver exact approved actions. Never migrates."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true")
        parser.add_argument("--schedule", action="store_true")
        parser.add_argument("--events", action="store_true")
        parser.add_argument("--deliver-actions", action="store_true")
        parser.add_argument("--webhooks", action="store_true")
        parser.add_argument("--interval", type=float, default=5)
        parser.add_argument("--limit", type=int, default=50)

    def handle(self, *args, **options):
        if not 1 <= options["interval"] <= 60 or not 1 <= options["limit"] <= 100:
            raise CommandError("Interval must be 1–60 seconds and limit 1–100.")
        while True:
            close_old_connections()
            try:
                outcome = tick(schedule=options["schedule"], fanout=options["events"],
                               deliver=options["deliver_actions"], webhooks=options["webhooks"], limit=options["limit"])
                self.stdout.write(json.dumps(outcome, sort_keys=True))
            finally:
                close_old_connections()
            if options["once"]:
                return
            time.sleep(options["interval"])
