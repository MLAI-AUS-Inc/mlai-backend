"""One optional control-plane scheduler; Hermes itself does not own these jobs."""

import json
import logging
from pathlib import Path
import time

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import close_old_connections

from agent_runtime import actions, events, runtime
from agent_runtime.models import AgentActionProposal
from agent_runtime.policy import AgentPermissionDenied
from agent_runtime.scheduler_health import report_failure

logger = logging.getLogger(__name__)


def tick(*, schedule=False, fanout=False, deliver=False, webhooks=False, native=False, limit=50, health_file=None, stdout=None):
    if not getattr(settings, "AGENT_RUNTIME_ENABLED", False):
        raise CommandError("AGENT_RUNTIME_ENABLED must be explicitly enabled.")
    if deliver and not getattr(settings, "AGENT_RUNTIME_ACTIONS_ENABLED", False):
        raise CommandError("Action delivery is disabled.")
    from agent_runtime.sources import live_access
    result = {"recovered_tasks": 0, "unknown_actions": 0,
              "scheduled": 0, "event_tasks": 0, "webhooks": 0, "sent": 0, "unconfirmed": 0, "blocked": 0,
              "errors": {}}
    marker = Path(health_file) if health_file else None

    def stage(name, operation):
        result.setdefault(name, 0)
        try:
            result[name] = operation()
        except Exception as exc:
            result["errors"][name] = result["errors"].get(name, 0) + 1
            report_failure(logger, name, exc, stdout=stdout)
        finally:
            if marker:
                try:
                    marker.touch()
                except OSError as exc:
                    result["errors"]["heartbeat"] = result["errors"].get("heartbeat", 0) + 1
                    report_failure(logger, "heartbeat", exc, stdout=stdout)

    stage("recovered_tasks", lambda: runtime.recover_tasks(limit=limit))
    stage("unknown_actions", lambda: actions.recover_actions(limit=limit))
    if schedule:
        stage("scheduled", lambda: runtime.schedule_due_responsibilities(access_checker=live_access, limit=limit))
    if fanout:
        stage("event_tasks", lambda: events.dispatch_subscriptions(access_checker=live_access, limit=limit))
    if native:
        from agent_runtime.native_installations import provision_native_installations
        from agent_runtime.native_invocations import poll_native_mentions, dispatch_native_replies, expire_native_invocations
        stage("native_provisioned", lambda: provision_native_installations(limit=limit))
        stage("native_invocations", lambda: poll_native_mentions(limit=limit))
        stage("native_replies", lambda: dispatch_native_replies(limit=limit))
        stage("native_expired", lambda: expire_native_invocations(limit=limit))
    if webhooks:
        from agent_runtime.event_webhooks import dispatch_webhooks
        stage("webhooks", lambda: dispatch_webhooks(limit=limit))
    if native:
        from agent_runtime.native_alerts import check_native_listening
        stage("native_alerts", lambda: check_native_listening())

    def deliver_actions():
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

    if deliver:
        stage("action_delivery", deliver_actions)
    return result


class Command(BaseCommand):
    help = "Recover agent work and optionally schedule, fan out events, or deliver exact approved actions. Never migrates."

    def add_arguments(self, parser):
        parser.add_argument("--health-file", default="/tmp/native-agent-heartbeat")
        parser.add_argument("--once", action="store_true")
        parser.add_argument("--schedule", action="store_true")
        parser.add_argument("--events", action="store_true")
        parser.add_argument("--deliver-actions", action="store_true")
        parser.add_argument("--webhooks", action="store_true")
        parser.add_argument("--native", action="store_true", help="Provision native Dot bots, poll mentions and deliver invocation replies.")
        parser.add_argument("--interval", type=float, default=5)
        parser.add_argument("--limit", type=int, default=50)

    def handle(self, *args, **options):
        if not 1 <= options["interval"] <= 60 or not 1 <= options["limit"] <= 100:
            raise CommandError("Interval must be 1–60 seconds and limit 1–100.")
        marker = Path(options["health_file"])
        marker.unlink(missing_ok=True)
        while True:
            close_old_connections()
            try:
                outcome = tick(schedule=options["schedule"], fanout=options["events"],
                               deliver=options["deliver_actions"], webhooks=options["webhooks"], native=options["native"], limit=options["limit"],
                               health_file=marker, stdout=self.stdout)
                self.stdout.write(json.dumps(outcome, sort_keys=True))
            except CommandError:
                raise
            except Exception as exc:
                report_failure(logger, "tick", exc, stdout=self.stdout)
            finally:
                close_old_connections()
            if options["once"]:
                return
            time.sleep(options["interval"])
