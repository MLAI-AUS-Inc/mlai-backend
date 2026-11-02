"""Run synthetic Pulse integration tests against an explicitly approved schema.

Only fresh disposable databases are permitted. Dotenv, external networking and
production credentials are excluded. The approved digest is not authorization
by itself; AGENTS.md requires the user's approval of this exact inventory.
"""

import argparse
import hashlib
import importlib
import json
import os
import pwd
import socket
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
INVENTORY = ROOT / "docs/pulse-reliability-runtime-migrations.json"
REGRESSIONS = [
    "community_chat.tests.test_startup_updates_database",
    "startup_updates.tests_revisions.MonthlyRevisionTests",
    "startup_updates.tests_revisions.MonthlyEvidencePipelineTests",
    "startup_updates.tests_revisions.FounderEvidenceApiTests",
    "startup_updates.tests_update_identity",
    "startup_updates.tests_reporting_canaries",
    "startup_updates.tests_founder_metrics",
    "startup_updates.tests_financial_history_database",
    "vibe_raising.tests_company_scoping",
    "vibe_raising.tests_progress_api",
]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--approved-inventory-sha256", required=True)
    parser.add_argument("--postgres-port", type=int)
    parser.add_argument(
        "--worker-source",
        type=Path,
        help="Exact Valley checkout under test; external calls remain blocked.",
    )
    parser.add_argument("--include-regressions", action="store_true")
    parser.add_argument("--test-label", action="append", dest="test_labels")
    args = parser.parse_args()
    raw = INVENTORY.read_bytes()
    if hashlib.sha256(raw).hexdigest() != args.approved_inventory_sha256:
        raise SystemExit("Migration approval inventory changed.")
    inventory = json.loads(raw)
    sys.path.insert(0, str(ROOT))
    if args.worker_source:
        worker = args.worker_source.resolve()
        if not (worker / "valley/workflows/receipts.py").is_file():
            raise SystemExit(
                "Supply the compatible Valley checkout containing the receipt validator."
            )
        sys.path.insert(0, str(worker))
    with tempfile.TemporaryDirectory(prefix="mlai-pulse-reliability-") as directory:
        database_name = f"test_pulse_reliability_{os.getpid()}"
        environment = {
            "PATH": os.defpath,
            "APP_ENV": "test",
            "APP_RELEASE": "pulse-synthetic-tests",
            "DEBUG": "true",
            "SECRET_KEY": "synthetic-test-signing-key",
            "DATABASE_URL": f"sqlite:///{directory}/startup.sqlite3",
            "DJANGO_SETTINGS_MODULE": "mlai.settings",
            "ALLOWED_HOSTS": "testserver,localhost",
            "CONNECTOR_CREDENTIAL_KEYS": '{"test":"MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="}',
            "CONNECTOR_CREDENTIAL_ACTIVE_KEY_ID": "test",
            "MLAI_BASE_URL": "http://backend.invalid",
            "MLAI_API_KEY": "synthetic",
            "OPENAI_API_KEY": "synthetic",
        }
        if args.postgres_port:
            if not 1024 < args.postgres_port < 65536:
                raise SystemExit("Use a dedicated unprivileged local test port.")
            user = pwd.getpwuid(os.getuid()).pw_name
            environment["DATABASE_URL"] = (
                f"postgresql://{user}@127.0.0.1:{args.postgres_port}/postgres"
            )
        original_connect = socket.socket.connect

        def local_only(sock, address):
            if (
                args.postgres_port
                and isinstance(address, tuple)
                and address[:2] == ("127.0.0.1", args.postgres_port)
            ):
                return original_connect(sock, address)
            raise AssertionError("External network forbidden")

        with (
            patch.dict(os.environ, environment, clear=True),
            patch("dotenv.load_dotenv", return_value=False),
            patch("socket.socket.connect", local_only),
            patch(
                "socket.create_connection",
                side_effect=AssertionError("External network forbidden"),
            ),
        ):
            import django
            from django.conf import settings

            settings.DATABASES["default"]["TEST"] = {
                "NAME": database_name
                if args.postgres_port
                else f"{directory}/test_startup.sqlite3"
            }
            settings.CACHES = {
                alias: {
                    "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
                    "LOCATION": f"pulse-tests-{alias}",
                }
                for alias in settings.CACHES
            }
            settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
            django.setup()
            from django.db.backends.base.base import BaseDatabaseWrapper
            from django.db.migrations.loader import MigrationLoader
            from django.test.runner import DiscoverRunner

            expected = {
                (row["app"], row["name"]): row["sha256"]
                for row in inventory["migrations"]
            }
            loader = MigrationLoader(None)
            if set(loader.disk_migrations) != set(expected):
                raise RuntimeError(
                    "Migration inventory changed; obtain exact approval."
                )
            for key, migration in loader.disk_migrations.items():
                module = importlib.import_module(migration.__module__)
                if (
                    hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest()
                    != expected[key]
                ):
                    raise RuntimeError(f"Migration changed: {key}")
            original = BaseDatabaseWrapper.ensure_connection

            def disposable_only(wrapper):
                config = wrapper.settings_dict
                sqlite = config["ENGINE"] == "django.db.backends.sqlite3" and str(
                    config["NAME"]
                ).startswith(directory + "/")
                postgres = (
                    args.postgres_port
                    and config["ENGINE"] == "django.db.backends.postgresql"
                    and config.get("HOST") == "127.0.0.1"
                    and str(config.get("PORT")) == str(args.postgres_port)
                    and config["NAME"] in {None, "postgres", "__no_db__", database_name}
                )
                if not sqlite and not postgres:
                    raise RuntimeError("Refusing a non-disposable database.")
                return original(wrapper)

            with patch.object(
                BaseDatabaseWrapper, "ensure_connection", disposable_only
            ):
                return DiscoverRunner(verbosity=2, interactive=False).run_tests(
                    (
                        args.test_labels
                        or ["startup_updates.tests_generation_reliability"]
                    )
                    + (REGRESSIONS if args.include_regressions else [])
                )


if __name__ == "__main__":
    raise SystemExit(main())
