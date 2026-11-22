#!/usr/bin/env python3
"""Run a single discovery tick on the approved, isolated Chat staging API."""

import importlib.util
import json
import os
from pathlib import Path
from textwrap import dedent

CONTROLLER = Path("/etc/mlai-backend-staging-auto-deploy/auto-deploy.py")
CONTAINER = "mlai-chat-staging-api-staging-api-1"
STAGING_COMMAND = dedent("""
    import os
    from urllib.parse import urlsplit
    from django.conf import settings
    from django.core.management import call_command
    from django.db import connection

    db = connection.settings_dict
    cache = urlsplit(settings.CACHES["default"]["LOCATION"])
    if (
        os.environ.get("DJANGO_SETTINGS_MODULE") != "mlai.settings_staging_chat"
        or settings.APP_ENV != "chat-staging"
        or (db["HOST"], db["NAME"], db["USER"]) != ("postgres-api", "staging_account_api", "staging_account_api")
        or cache.hostname != "redis-api"
        or settings.EMAIL_BACKEND != "django.core.mail.backends.locmem.EmailBackend"
    ):
        raise SystemExit("Unexpected scheduler target")
    call_command("run_scheduled_discovery")
""")


def tick(controller):
    controller.validate()
    with controller.locked() as acquired:
        if not acquired:
            return "deployment in progress"
        if controller.journal.exists() or controller.peer_recovery_pending() or controller.paused.exists():
            return "deployment paused or recovering"

        # Verify the exact running image, labels, bridge, firewall and approved config.
        controller.running(controller.values(controller.env))
        controller.run([
            "docker", "exec", "--env", "JOBS_SCHEDULER_ENABLED=false", CONTAINER,
            "timeout", "--kill-after=5s", "90s",
            "python", "manage.py", "shell", "-c", STAGING_COMMAND,
        ], timeout=105)
        return "completed"


def main():
    os.umask(0o077)
    if CONTROLLER.is_symlink() or CONTROLLER.stat().st_uid != 0 or CONTROLLER.stat().st_mode & 0o022:
        raise SystemExit("Untrusted installed controller")
    spec = importlib.util.spec_from_file_location("staging_controller", CONTROLLER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    config = CONTROLLER.with_name("config.json")
    module.private_file(config)
    print("Chat staging scheduler:", tick(module.Controller(json.loads(config.read_text()))))


if __name__ == "__main__":
    main()
