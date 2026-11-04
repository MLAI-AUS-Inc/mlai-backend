"""Run financial refresh tests with an explicitly approved disposable schema.

The inventory digest checks scope; user approval is separately required by
AGENTS.md. Environment files, production credentials and networking are blocked.
"""
import argparse
import hashlib
import importlib
import json
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
INVENTORY = ROOT / "docs/pulse-financial-refresh-test-migrations.json"
LABELS = [
    "startup_updates.tests_financial_refresh_database",
    "startup_updates.tests_financial_history_database",
    "startup_updates.tests_revisions.MonthlyRevisionTests",
    "startup_updates.tests_revisions.MonthlyEvidencePipelineTests",
    "startup_updates.tests_revisions.FounderEvidenceApiTests",
]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--approved-inventory-sha256", required=True)
    parser.add_argument("--test-label", action="append")
    args = parser.parse_args()
    raw = INVENTORY.read_bytes()
    if hashlib.sha256(raw).hexdigest() != args.approved_inventory_sha256:
        raise SystemExit("The inventory differs from the explicitly approved digest.")
    inventory = json.loads(raw)
    sys.path.insert(0, str(ROOT))
    with tempfile.TemporaryDirectory(prefix="mlai-financial-refresh-tests-") as directory:
        environment = {
            "PATH": os.defpath, "APP_ENV": "test", "APP_RELEASE": "financial-refresh-synthetic-tests",
            "DEBUG": "true", "SECRET_KEY": "financial-refresh-synthetic-test-key",
            "DATABASE_URL": f"sqlite:///{directory}/startup.sqlite3",
            "DJANGO_SETTINGS_MODULE": "mlai.settings", "ALLOWED_HOSTS": "testserver,localhost",
            "CONNECTOR_CREDENTIAL_KEYS": '{"test":"MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="}',
            "CONNECTOR_CREDENTIAL_ACTIVE_KEY_ID": "test",
        }
        with (
            patch.dict(os.environ, environment, clear=True),
            patch("dotenv.load_dotenv", return_value=False),
            patch("socket.socket.connect", side_effect=AssertionError("Network forbidden")),
            patch("socket.create_connection", side_effect=AssertionError("Network forbidden")),
        ):
            import django
            from django.conf import settings
            settings.DATABASES["default"]["TEST"] = {"NAME": f"{directory}/test_startup.sqlite3"}
            settings.CACHES = {alias: {"BACKEND": "django.core.cache.backends.locmem.LocMemCache",
                "LOCATION": f"financial-refresh-tests-{alias}"} for alias in settings.CACHES}
            settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
            django.setup()
            from django.db.backends.base.base import BaseDatabaseWrapper
            from django.db.migrations.loader import MigrationLoader
            from django.test.runner import DiscoverRunner
            expected = {(row["app"], row["name"]): row["sha256"] for row in inventory["migrations"]}
            loader = MigrationLoader(None)
            if set(loader.disk_migrations) != set(expected):
                raise RuntimeError("Migration inventory changed; obtain new approval.")
            for key, migration in loader.disk_migrations.items():
                module = importlib.import_module(migration.__module__)
                if hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest() != expected[key]:
                    raise RuntimeError(f"Migration changed: {key}; obtain new approval.")
            original = BaseDatabaseWrapper.ensure_connection

            def only_disposable_sqlite(wrapper):
                config = wrapper.settings_dict
                if config["ENGINE"] != "django.db.backends.sqlite3" or not str(config["NAME"]).startswith(directory + "/"):
                    raise RuntimeError("Refusing a non-disposable database.")
                opening = wrapper.connection is None
                result = original(wrapper)
                if opening:
                    # This database is throwaway; avoid syncing every schema
                    # operation to disk while applying the approved inventory.
                    with wrapper.cursor() as cursor:
                        cursor.execute("PRAGMA synchronous = OFF")
                        cursor.execute("PRAGMA journal_mode = MEMORY")
                return result

            with patch.object(BaseDatabaseWrapper, "ensure_connection", only_disposable_sqlite):
                return DiscoverRunner(verbosity=2, interactive=False).run_tests(args.test_label or LABELS)


if __name__ == "__main__":
    raise SystemExit(main())
