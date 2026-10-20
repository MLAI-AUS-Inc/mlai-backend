"""Run approved event-discount DB tests with synthetic configuration and no network.

The test runner creates disposable databases from migration state. Its use
requires the specific local migration approval recorded in the feature runbook.
No .env file or inherited credential is loaded. Production is never a target.
"""

import os
import argparse
import getpass
from pathlib import Path
import sys
import uuid
from unittest.mock import patch


def main(labels):
    """Run tests against SQLite or a new DB on a dedicated temporary PG socket."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--postgres-socket")
    parser.add_argument("--postgres-port", type=int, default=65439)
    parser.add_argument("labels", nargs="*")
    arguments = parser.parse_args(labels)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    environment = {
        "PATH": os.defpath, "APP_ENV": "test", "DEBUG": "true",
        "SECRET_KEY": "synthetic-event-discount-test-key-not-for-deployment",
        "DATABASE_URL": "sqlite:///:memory:", "ALLOWED_HOSTS": "testserver,localhost",
        "DJANGO_SETTINGS_MODULE": "mlai.settings",
    }
    with (
        patch.dict(os.environ, environment, clear=True),
        patch("dotenv.load_dotenv", return_value=False),
        patch("socket.socket.connect", side_effect=AssertionError("Network access forbidden")),
        patch("socket.create_connection", side_effect=AssertionError("Network access forbidden")),
    ):
        import django
        if arguments.postgres_socket:
            socket_dir = Path(arguments.postgres_socket).resolve()
            if not socket_dir.name.startswith("mlai-event-discounts-pg-") or socket_dir.parent != Path("/tmp").resolve():
                raise SystemExit("PostgreSQL verification requires its own /tmp/mlai-event-discounts-pg-* socket directory.")
            if not (socket_dir / "data" / "PG_VERSION").is_file() or not 1024 < arguments.postgres_port < 65536:
                raise SystemExit("The dedicated disposable PostgreSQL cluster is not ready.")
            from django.conf import settings
            settings.DATABASES = {"default": {
                "ENGINE": "django.db.backends.postgresql", "NAME": "postgres",
                "USER": getpass.getuser(), "HOST": str(socket_dir), "PORT": arguments.postgres_port,
                "TEST": {"NAME": "test_roo_event_discounts_" + uuid.uuid4().hex[:12]},
            }}
        django.setup()
        from django.core.management import call_command
        call_command("test", *(arguments.labels or ["roo.tests_event_discounts"]), interactive=False, verbosity=2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
