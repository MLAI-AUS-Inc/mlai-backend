"""Test runner that clones a migrated PostgreSQL template for each run.

Every ``manage.py test`` invocation normally builds its test database by
applying the whole migration chain (about 400 migrations, roughly three
minutes on CI). The PostgreSQL CI jobs run 15 or more invocations each, so most
of their wall time was spent re-applying identical migrations.

When ``MLAI_TEST_DATABASE_TEMPLATE`` is set, the first invocation in a job
builds the test database the normal way, then copies it into a template
database before any test runs. Each later invocation drops the test database
and recreates it with ``CREATE DATABASE ... TEMPLATE``, then lets Django run
its usual keep-database setup, which still applies any unapplied migration,
creates cache tables and serialises contents. The template is keyed on the
migration files and installed apps, so a different schema never reuses it.

Each run therefore starts from a database identical to a freshly migrated one.
SQLite, parallel runs and an explicit ``--keepdb`` keep Django's behaviour.
"""

import hashlib
import os
from pathlib import Path

from django.apps import apps
from django.conf import settings
from django.db import connections
from django.test.runner import DiscoverRunner

TEMPLATE_ENV = "MLAI_TEST_DATABASE_TEMPLATE"


def _schema_fingerprint():
    """Return a short hash of every migration file and the installed apps."""
    digest = hashlib.sha256()
    for app_label in sorted(settings.INSTALLED_APPS):
        digest.update(app_label.encode())
        digest.update(b"\0")
    for app_config in sorted(apps.get_app_configs(), key=lambda config: config.label):
        migrations_dir = Path(app_config.path) / "migrations"
        if not migrations_dir.is_dir():
            continue
        for migration in sorted(migrations_dir.glob("*.py")):
            digest.update(str(migration.relative_to(app_config.path)).encode())
            digest.update(b"\0")
            digest.update(migration.read_bytes())
            digest.update(b"\0")
    return digest.hexdigest()[:16]


def _quote(connection, name):
    return connection.ops.quote_name(name)


class TemplateDatabaseRunner(DiscoverRunner):
    """DiscoverRunner that reuses one migrated PostgreSQL template per job."""

    def _template_connection(self):
        if not os.environ.get(TEMPLATE_ENV) or self.keepdb or self.parallel > 1:
            return None
        aliases = list(connections)
        if aliases != ["default"]:
            return None
        connection = connections["default"]
        if connection.vendor != "postgresql":
            return None
        return connection

    def setup_databases(self, **kwargs):
        connection = self._template_connection()
        if connection is None:
            return super().setup_databases(**kwargs)

        test_name = connection.creation._get_test_db_name()
        template_name = f"{test_name}_tpl_{_schema_fingerprint()}"[:63]
        with connection._nodb_cursor() as cursor:
            cursor.execute("SELECT 1 FROM pg_database WHERE datname = %s", [template_name])
            template_exists = cursor.fetchone() is not None
            if template_exists:
                cursor.execute(f"DROP DATABASE IF EXISTS {_quote(connection, test_name)}")
                cursor.execute(
                    f"CREATE DATABASE {_quote(connection, test_name)} "
                    f"TEMPLATE {_quote(connection, template_name)}"
                )

        if template_exists:
            if self.verbosity >= 1:
                print(f"Cloned test database from migrated template {template_name}.")
            self.keepdb = True
            return super().setup_databases(**kwargs)

        old_config = super().setup_databases(**kwargs)
        # CREATE DATABASE ... TEMPLATE needs the source to have no sessions.
        connection.close()
        with connection._nodb_cursor() as cursor:
            cursor.execute(
                f"CREATE DATABASE {_quote(connection, template_name)} "
                f"TEMPLATE {_quote(connection, connection.settings_dict['NAME'])}"
            )
        if self.verbosity >= 1:
            print(f"Saved migrated test database as template {template_name}.")
        return old_config
