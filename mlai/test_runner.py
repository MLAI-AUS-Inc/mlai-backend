"""Test runner that reuses one migrated test database per CI job.

Every ``manage.py test`` invocation normally builds its test database by
applying the whole migration chain (about 400 migrations, roughly three
minutes on CI). The larger CI jobs run about 15 invocations each, so most of
their wall time was spent re-applying identical migrations.

When ``MLAI_TEST_DATABASE_TEMPLATE`` is set, the first invocation in a job
builds the test database the normal way, then saves a copy before any test
runs. Each later invocation starts from that copy and lets Django run its
usual keep-database setup, which still applies any unapplied migration,
creates cache tables and serialises contents. The copy is keyed on the
migration files, the installed apps and the Django version, so a different
schema never reuses it.

PostgreSQL keeps the copy as a template database and recreates the test
database with ``CREATE DATABASE ... TEMPLATE``. SQLite keeps it as a file in a
directory only the current user can write, and loads it into the in-memory
test database with SQLite's backup API, so tests still run against the same
in-memory database Django would have created.

Each run therefore starts from a database identical to a freshly migrated one.
``--parallel`` works with both: Django clones its per-worker databases from
that fresh database, and stale PostgreSQL worker clones are dropped first. An
explicit ``--keepdb``, a file-backed SQLite test database, multi-database
setups and a selection with no database tests keep Django's behaviour.

The key covers what CI can change between two commands of one job. It does not
cover project code that migrations import, so the switch is for CI's
single-checkout jobs and stays unset on a developer machine.
"""

import hashlib
import os
import re
import sqlite3
import stat
import tempfile
from pathlib import Path
from urllib.parse import quote

import django
from django.apps import apps
from django.conf import settings
from django.db import connections
from django.test.runner import DiscoverRunner

TEMPLATE_ENV = "MLAI_TEST_DATABASE_TEMPLATE"


def _schema_fingerprint():
    """Return a short hash of the migration files, installed apps and Django."""
    digest = hashlib.sha256()
    digest.update(django.get_version().encode())
    digest.update(b"\0")
    for app_label in sorted(settings.INSTALLED_APPS):
        digest.update(app_label.encode())
        digest.update(b"\0")
    for app_config in sorted(apps.get_app_configs(), key=lambda config: config.label):
        migrations_dir = Path(app_config.path) / "migrations"
        if not migrations_dir.is_dir():
            continue
        digest.update(app_config.label.encode())
        digest.update(b"\0")
        for migration in sorted(migrations_dir.glob("*.py")):
            digest.update(str(migration.relative_to(app_config.path)).encode())
            digest.update(b"\0")
            digest.update(migration.read_bytes())
            digest.update(b"\0")
    return digest.hexdigest()[:16]


def _quote(connection, name):
    return connection.ops.quote_name(name)


def _template_name(test_name, fingerprint):
    """Name the PostgreSQL template within the 63-character identifier limit.

    The fingerprint is kept whole: a shortened one could let a database built
    from other migrations pass for this one.
    """
    suffix = f"_tpl_{fingerprint}"
    return f"{test_name[:63 - len(suffix)]}{suffix}"


def _load_sqlite_copy(copy_path, target):
    """Fill ``target`` from the saved copy; False when the copy is unusable."""
    try:
        saved = sqlite3.connect(f"file:{quote(str(copy_path))}?mode=ro", uri=True)
        try:
            saved.backup(target)
        finally:
            saved.close()
        migrated = target.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'django_migrations'"
        ).fetchone()
    except sqlite3.Error:
        return False
    return migrated is not None


def _sqlite_copy_directory():
    """Return a directory only this user can write, or None when there is none.

    The saved copy becomes the test database, so it must not come from a path
    another local user could have written first.
    """
    if not hasattr(os, "getuid"):
        return None
    directory = Path(tempfile.gettempdir()) / f"mlai-test-database-{os.getuid()}"
    try:
        directory.mkdir(mode=0o700)
    except FileExistsError:
        pass
    except OSError:
        return None
    try:
        details = directory.lstat()
    except OSError:
        return None
    owned_and_private = (
        stat.S_ISDIR(details.st_mode)
        and details.st_uid == os.getuid()
        and not details.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    )
    return directory if owned_and_private else None


class TemplateDatabaseRunner(DiscoverRunner):
    """DiscoverRunner that reuses one migrated test database per job."""

    def _template_connection(self):
        if not os.environ.get(TEMPLATE_ENV) or self.keepdb:
            return None
        aliases = list(connections)
        if aliases != ["default"]:
            return None
        connection = connections["default"]
        if connection.vendor == "postgresql":
            return connection
        if connection.vendor == "sqlite" and connection.creation.is_in_memory_db(
            connection.creation._get_test_db_name()
        ):
            return connection
        return None

    def setup_databases(self, **kwargs):
        connection = self._template_connection()
        if connection is None:
            return super().setup_databases(**kwargs)
        aliases = kwargs.get("aliases")
        if aliases is not None and "default" not in aliases:
            # No selected test uses the database, so Django builds none. There
            # is nothing to reuse and no migrated database to save.
            return super().setup_databases(**kwargs)
        if connection.vendor == "sqlite":
            return self._setup_sqlite(connection, **kwargs)
        return self._setup_postgresql(connection, **kwargs)

    def _setup_postgresql(self, connection, **kwargs):
        test_name = connection.creation._get_test_db_name()
        template_name = _template_name(test_name, _schema_fingerprint())
        with connection._nodb_cursor() as cursor:
            cursor.execute("SELECT 1 FROM pg_database WHERE datname = %s", [template_name])
            template_exists = cursor.fetchone() is not None
            if template_exists:
                # A parallel run leaves one clone per worker behind, and the
                # keep-database setup would reuse those as they were left.
                cursor.execute(
                    "SELECT datname FROM pg_database WHERE datname ~ %s",
                    [f"^{re.escape(test_name)}_[0-9]+$"],
                )
                for (worker_clone,) in cursor.fetchall():
                    cursor.execute(f"DROP DATABASE IF EXISTS {_quote(connection, worker_clone)}")
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
        if connection.settings_dict["NAME"] != test_name:
            # Django did not switch to a test database, so the configured
            # database is still selected and must not become the template.
            return old_config
        # CREATE DATABASE ... TEMPLATE needs the source to have no sessions.
        connection.close()
        with connection._nodb_cursor() as cursor:
            cursor.execute(
                f"CREATE DATABASE {_quote(connection, template_name)} "
                f"TEMPLATE {_quote(connection, test_name)}"
            )
        if self.verbosity >= 1:
            print(f"Saved migrated test database as template {template_name}.")
        return old_config

    def _setup_sqlite(self, connection, **kwargs):
        directory = _sqlite_copy_directory()
        if directory is None:
            return super().setup_databases(**kwargs)
        copy_path = directory / f"{_schema_fingerprint()}.sqlite3"
        test_name = connection.creation._get_test_db_name()

        if copy_path.is_file() and not copy_path.is_symlink():
            # A shared in-memory database lives while one connection holds it.
            # Fill it from the saved copy and hold this connection until Django
            # has opened its own, which it keeps for the whole run.
            anchor = sqlite3.connect(test_name, uri=True)
            try:
                if _load_sqlite_copy(copy_path, anchor):
                    if self.verbosity >= 1:
                        print(f"Loaded test database from migrated copy {copy_path.name}.")
                    self.keepdb = True
                    return super().setup_databases(**kwargs)
            finally:
                anchor.close()
            # A damaged or unmigrated copy would fail or slow every later run.
            # Closing the only connection discarded what was loaded, so build
            # normally and replace the copy.
            if self.verbosity >= 1:
                print(f"Rebuilding unusable migrated copy {copy_path.name}.")

        old_config = super().setup_databases(**kwargs)
        if connection.settings_dict["NAME"] != test_name:
            # Django did not switch to a test database, so the configured
            # database is still selected and must not become the saved copy.
            return old_config
        connection.ensure_connection()
        # Write beside the final name and rename, so a killed run never leaves
        # a partial copy that a later invocation would trust.
        partial_path = directory / f"{copy_path.name}.{os.getpid()}.partial"
        partial_path.unlink(missing_ok=True)
        target = sqlite3.connect(partial_path)
        try:
            connection.connection.backup(target)
        finally:
            target.close()
        os.replace(partial_path, copy_path)
        if self.verbosity >= 1:
            print(f"Saved migrated test database as copy {copy_path.name}.")
        return old_config
