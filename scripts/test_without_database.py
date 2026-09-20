"""Run explicitly selected unittest/SimpleTestCase suites without DB or network.

This does not use Django's test runner and never constructs a test database.
Database-backed suites are rejected before execution. Environment credentials
and dotenv loading are excluded even when run from a configured checkout.
"""

import importlib
import hashlib
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch


def iter_tests(suite):
    for test in suite:
        if isinstance(test, unittest.TestSuite):
            yield from iter_tests(test)
        else:
            yield test


def main(labels):
    if not labels:
        raise SystemExit("Supply explicit unittest or SimpleTestCase labels.")
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    safe_environment = {
        "PATH": os.defpath,
        "APP_ENV": "test",
        "APP_RELEASE": "database-free-tests",
        "DEBUG": "true",
        "SECRET_KEY": "synthetic-unit-test-signing-key-not-for-deployment",
        "DATABASE_URL": "sqlite:///:memory:",
        "DJANGO_SETTINGS_MODULE": "mlai.settings",
        "ALLOWED_HOSTS": "testserver,localhost",
    }
    with (
        patch.dict(os.environ, safe_environment, clear=True),
        patch("dotenv.load_dotenv", return_value=False),
        patch("socket.socket.connect", side_effect=AssertionError("Network access forbidden")),
        patch("socket.create_connection", side_effect=AssertionError("Network access forbidden")),
        patch(
            "django.db.backends.base.base.BaseDatabaseWrapper.ensure_connection",
            side_effect=AssertionError("Database access forbidden"),
        ),
    ):
        settings_module = importlib.import_module("mlai.settings")
        settings_module.DATABASES = {"default": {"ENGINE": "django.db.backends.dummy"}}
        import django
        django.setup()
        if labels == ["--check-models"]:
            from django.core.management import call_command
            call_command("check")
            call_command("makemigrations", check=True, dry_run=True, interactive=False)
            return 0
        if labels == ["--describe-migrations"]:
            # Loading the graph with connection=None reads files only. It does
            # not inspect a database, create migration files or run operations.
            from django.db.migrations.loader import MigrationLoader
            loader = MigrationLoader(None)
            inventory = []
            for (app, name), migration in sorted(loader.disk_migrations.items()):
                path = Path(sys.modules[migration.__module__].__file__)
                inventory.append({
                    "app": app, "name": name,
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                })
            print(json.dumps({
                "purpose": "Approval inventory for disposable local regression databases only",
                "django_version": django.get_version(),
                "migration_count": len(inventory),
                "leaf_nodes": loader.graph.leaf_nodes(),
                "migrations": inventory,
            }, indent=2))
            return 0
        from django.test import TransactionTestCase
        suite = unittest.defaultTestLoader.loadTestsFromNames(labels)
        for test in iter_tests(suite):
            if isinstance(test, TransactionTestCase):
                raise SystemExit(f"Database-backed test requires separate migration approval: {test.id()}")
        result = unittest.TextTestRunner(verbosity=2).run(suite)
        return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
