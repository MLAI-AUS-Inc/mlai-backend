"""Dedicated community inference worker; never blocks personal Dot delivery."""
import json
import logging
from pathlib import Path
import time

from django.core.management.base import BaseCommand, CommandError
from django.db import close_old_connections

from agent_runtime import community
from agent_runtime.native_installations import provision_native_installations
from agent_runtime.scheduler_health import report_failure

logger = logging.getLogger(__name__)


def tick(marker):
    result = {}
    stages = {"recovered": community.recover,
        "provisioned": lambda: provision_native_installations(app_id=community.APP_ID, limit=5),
        "admitted": lambda: community.poll_mentions(limit=10),
        "generated": lambda: community.run_pending(limit=1), "replied": community.dispatch_replies}
    for name, operation in stages.items():
        try:
            result[name] = operation()
        except Exception as exc:
            result[name] = "unavailable"
            report_failure(logger, f"community_{name}", exc)
        marker.touch()
    return result


class Command(BaseCommand):
    help = "Run the opt-in, isolated community assistant. Never applies migrations."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true")
        parser.add_argument("--health-file", default="/tmp/community-agent-heartbeat")

    def handle(self, *args, **options):
        if not community.available():
            raise CommandError("Community inference and its provider must be explicitly configured.")
        marker = Path(options["health_file"])
        marker.unlink(missing_ok=True)
        while True:
            close_old_connections()
            self.stdout.write(json.dumps(tick(marker), sort_keys=True))
            if options["once"]:
                break
            time.sleep(5)
