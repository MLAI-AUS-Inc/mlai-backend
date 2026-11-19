"""Checks for the migrated test database reuse in ``mlai/test_runner.py``.

Fakes and SQLite's own in-process memory stand in for the databases: no Django
connection is opened, no migration is applied and no PostgreSQL is contacted.
"""

import os
import re
import sqlite3
import tempfile
import uuid
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from django.test.runner import DiscoverRunner

from mlai import test_runner
from mlai.test_runner import TEMPLATE_ENV, TemplateDatabaseRunner

FINGERPRINT = "0123456789abcdef"


class FakeSqliteConnection:
    """The parts of Django's SQLite wrapper that the runner touches."""

    vendor = "sqlite"

    def __init__(self, *, in_memory=True):
        self.test_name = f"file:memorydb_unit_{uuid.uuid4().hex}?mode=memory&cache=shared"
        self.creation = SimpleNamespace(
            _get_test_db_name=lambda: self.test_name,
            is_in_memory_db=lambda name: in_memory,
        )
        self.connection = None

    def ensure_connection(self):
        if self.connection is None:
            self.connection = sqlite3.connect(self.test_name, uri=True)


class FakePostgresConnection:
    """Records the statements sent on the maintenance connection."""

    vendor = "postgresql"

    def __init__(self, databases):
        self.databases = set(databases)
        self.statements = []
        self.events = []
        self.creation = SimpleNamespace(_get_test_db_name=lambda: "test_mlai")
        self.ops = SimpleNamespace(quote_name=lambda name: f'"{name}"')
        self.settings_dict = {"NAME": "test_mlai"}

    def close(self):
        self.events.append("close")

    @contextmanager
    def _nodb_cursor(self):
        yield _FakePostgresCursor(self)


class _FakePostgresCursor:
    def __init__(self, connection):
        self.connection = connection
        self.rows = []

    def execute(self, sql, params=None):
        self.connection.statements.append(sql)
        self.connection.events.append(sql)
        if sql.startswith("SELECT 1 FROM pg_database"):
            self.rows = [(1,)] if params[0] in self.connection.databases else []
        elif sql.startswith("SELECT datname FROM pg_database"):
            self.rows = [(name,) for name in sorted(self.connection.databases) if re.search(params[0], name)]

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return list(self.rows)


class RunnerCase(TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        for patcher in (
            patch.dict(os.environ, {TEMPLATE_ENV: "1"}),
            patch.object(tempfile, "gettempdir", return_value=self.temporary.name),
            patch.object(test_runner, "_schema_fingerprint", return_value=FINGERPRINT),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.copy_directory = Path(self.temporary.name) / f"mlai-test-database-{os.getuid()}"
        self.copy_path = self.copy_directory / f"{FINGERPRINT}.sqlite3"

    def run_setup(self, connection, django_setup, *, keepdb=False, aliases=("default",)):
        """Run the runner's database setup with Django's own setup replaced."""
        runner = TemplateDatabaseRunner(verbosity=0, keepdb=keepdb)
        calls = []

        def setup_databases(self_, **kwargs):
            calls.append(self_.keepdb)
            return django_setup(self_)

        registry = {alias: connection for alias in aliases}
        with (
            patch.object(test_runner, "connections", registry),
            patch.object(DiscoverRunner, "setup_databases", setup_databases),
        ):
            result = runner.setup_databases()
        return runner, calls, result


class SqliteReuseTests(RunnerCase):
    def migrate(self, connection):
        """Stand in for Django building the schema from migrations."""

        def django_setup(runner):
            connection.ensure_connection()
            connection.connection.execute("CREATE TABLE thing (name TEXT)")
            connection.connection.execute("INSERT INTO thing VALUES ('seed')")
            connection.connection.commit()
            return "old-config"

        return django_setup

    def build_copy(self):
        connection = FakeSqliteConnection()
        self.addCleanup(lambda: connection.connection.close())
        return self.run_setup(connection, self.migrate(connection))

    def test_first_run_migrates_normally_and_saves_a_private_copy(self):
        runner, calls, result = self.build_copy()

        self.assertEqual(calls, [False])
        self.assertFalse(runner.keepdb)
        self.assertEqual(result, "old-config")
        self.assertEqual(sorted(path.name for path in self.copy_directory.iterdir()), [self.copy_path.name])
        self.assertEqual(self.copy_directory.stat().st_mode & 0o777, 0o700)
        saved = sqlite3.connect(self.copy_path)
        self.addCleanup(saved.close)
        self.assertEqual(saved.execute("SELECT name FROM thing").fetchall(), [("seed",)])

    def test_later_run_starts_from_the_copy_and_keeps_django_setup(self):
        self.build_copy()
        connection = FakeSqliteConnection()
        seen = []

        def django_setup(runner):
            # Django opens its connection here and holds it for the whole run.
            connection.ensure_connection()
            seen.append(connection.connection.execute("SELECT name FROM thing").fetchall())
            return "old-config"

        runner, calls, result = self.run_setup(connection, django_setup)
        self.addCleanup(connection.connection.close)

        # Django still runs its keep-database setup, against the loaded copy.
        self.assertEqual(calls, [True])
        self.assertTrue(runner.keepdb)
        self.assertEqual(result, "old-config")
        self.assertEqual(seen, [[("seed",)]])
        # The database outlives the runner's own short-lived connection.
        self.assertEqual(connection.connection.execute("SELECT count(*) FROM thing").fetchone(), (1,))

    def test_rows_written_during_a_run_never_reach_the_saved_copy(self):
        self.build_copy()
        first = FakeSqliteConnection()

        def dirty_setup(runner):
            first.ensure_connection()
            first.connection.execute("INSERT INTO thing VALUES ('left behind')")
            first.connection.commit()

        self.run_setup(first, dirty_setup)
        first.connection.close()

        second = FakeSqliteConnection()
        seen = []

        def clean_setup(runner):
            second.ensure_connection()
            seen.append(second.connection.execute("SELECT name FROM thing").fetchall())

        self.run_setup(second, clean_setup)
        self.addCleanup(second.connection.close)

        self.assertEqual(seen, [[("seed",)]])

    def test_a_different_schema_never_loads_the_saved_copy(self):
        self.build_copy()
        connection = FakeSqliteConnection()
        self.addCleanup(lambda: connection.connection.close())

        with patch.object(test_runner, "_schema_fingerprint", return_value="f" * 16):
            runner, calls, _result = self.run_setup(connection, self.migrate(connection))

        self.assertEqual(calls, [False])
        self.assertFalse(runner.keepdb)
        self.assertTrue((self.copy_directory / f"{'f' * 16}.sqlite3").is_file())

    def test_a_copy_directory_others_can_write_is_not_trusted(self):
        self.build_copy()
        self.copy_directory.chmod(0o770)
        connection = FakeSqliteConnection()
        self.addCleanup(lambda: connection.connection.close())

        runner, calls, _result = self.run_setup(connection, self.migrate(connection))

        self.assertEqual(calls, [False])
        self.assertFalse(runner.keepdb)

    def test_a_symlinked_copy_directory_is_not_trusted(self):
        elsewhere = Path(self.temporary.name) / "elsewhere"
        elsewhere.mkdir(mode=0o700)
        self.copy_directory.symlink_to(elsewhere)
        connection = FakeSqliteConnection()
        self.addCleanup(lambda: connection.connection.close())

        _runner, calls, _result = self.run_setup(connection, self.migrate(connection))

        self.assertEqual(calls, [False])
        self.assertEqual(list(elsewhere.iterdir()), [])

    def test_a_symlinked_copy_is_replaced_rather_than_loaded(self):
        self.build_copy()
        planted = Path(self.temporary.name) / "planted.sqlite3"
        self.copy_path.rename(planted)
        self.copy_path.symlink_to(planted)
        connection = FakeSqliteConnection()
        self.addCleanup(lambda: connection.connection.close())

        _runner, calls, _result = self.run_setup(connection, self.migrate(connection))

        self.assertEqual(calls, [False])
        self.assertFalse(self.copy_path.is_symlink())
        self.assertTrue(self.copy_path.is_file())


class DjangoBehaviourKeptTests(RunnerCase):
    def assert_django_default(self, connection, **options):
        runner, calls, result = self.run_setup(connection, lambda runner: "old-config", **options)
        self.assertEqual(calls, [options.get("keepdb", False)])
        self.assertEqual(result, "old-config")
        self.assertFalse(self.copy_directory.exists())
        return runner

    def test_switch_unset_keeps_django_setup(self):
        with patch.dict(os.environ, {TEMPLATE_ENV: ""}):
            self.assert_django_default(FakeSqliteConnection())

    def test_explicit_keepdb_keeps_django_setup(self):
        connection = FakePostgresConnection({"test_mlai", f"test_mlai_tpl_{FINGERPRINT}"})
        self.assert_django_default(connection, keepdb=True)
        self.assertEqual(connection.statements, [])

    def test_more_than_one_database_keeps_django_setup(self):
        self.assert_django_default(FakeSqliteConnection(), aliases=("default", "replica"))

    def test_file_backed_sqlite_test_database_keeps_django_setup(self):
        self.assert_django_default(FakeSqliteConnection(in_memory=False))

    def test_other_database_engines_keep_django_setup(self):
        self.assert_django_default(SimpleNamespace(vendor="mysql"))


class PostgresTemplateTests(RunnerCase):
    template = f"test_mlai_tpl_{FINGERPRINT}"

    def test_first_run_migrates_normally_then_saves_the_template(self):
        connection = FakePostgresConnection({"postgres", "mlai"})

        def django_setup(runner):
            connection.events.append("django setup")
            return "old-config"

        runner, calls, result = self.run_setup(connection, django_setup)

        self.assertEqual(calls, [False])
        self.assertFalse(runner.keepdb)
        self.assertEqual(result, "old-config")
        # The template is copied only after Django finished and the connection
        # to the source database was closed.
        self.assertEqual(connection.events, [
            "SELECT 1 FROM pg_database WHERE datname = %s",
            "django setup",
            "close",
            f'CREATE DATABASE "{self.template}" TEMPLATE "test_mlai"',
        ])

    def test_later_run_recreates_the_database_and_drops_stale_worker_clones(self):
        connection = FakePostgresConnection({
            "postgres", "mlai", "test_mlai", self.template,
            # Left by an earlier --parallel run.
            "test_mlai_1", "test_mlai_2", "test_mlai_10",
            # Other projects and look-alikes stay untouched.
            "test_mlaix_1", "test_mlai_1_backup", "xtest_mlai_3",
        })

        def django_setup(runner):
            connection.events.append("django setup")
            return "old-config"

        runner, calls, result = self.run_setup(connection, django_setup)

        self.assertEqual(calls, [True])
        self.assertTrue(runner.keepdb)
        self.assertEqual(result, "old-config")
        self.assertEqual(connection.events, [
            "SELECT 1 FROM pg_database WHERE datname = %s",
            "SELECT datname FROM pg_database WHERE datname ~ %s",
            'DROP DATABASE IF EXISTS "test_mlai_1"',
            'DROP DATABASE IF EXISTS "test_mlai_10"',
            'DROP DATABASE IF EXISTS "test_mlai_2"',
            'DROP DATABASE IF EXISTS "test_mlai"',
            f'CREATE DATABASE "test_mlai" TEMPLATE "{self.template}"',
            "django setup",
        ])


class SchemaFingerprintTests(TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        for label in ("alpha", "beta"):
            (self.root / label / "migrations").mkdir(parents=True)
            (self.root / label / "migrations" / "0001_initial.py").write_text(f"# {label}\n")
        (self.root / "no_migrations").mkdir()
        configs = [
            SimpleNamespace(label=label, path=str(self.root / label))
            for label in ("beta", "no_migrations", "alpha")
        ]
        patcher = patch.object(test_runner, "apps", SimpleNamespace(get_app_configs=lambda: configs))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_same_migrations_give_the_same_fingerprint(self):
        self.assertEqual(test_runner._schema_fingerprint(), test_runner._schema_fingerprint())
        self.assertRegex(test_runner._schema_fingerprint(), r"^[0-9a-f]{16}$")

    def test_edited_added_and_moved_migrations_change_the_fingerprint(self):
        seen = {test_runner._schema_fingerprint()}
        (self.root / "alpha" / "migrations" / "0001_initial.py").write_text("# alpha, edited\n")
        seen.add(test_runner._schema_fingerprint())
        (self.root / "alpha" / "migrations" / "0002_more.py").write_text("# more\n")
        seen.add(test_runner._schema_fingerprint())
        (self.root / "alpha" / "migrations" / "0002_more.py").rename(self.root / "beta" / "migrations" / "0002_more.py")
        seen.add(test_runner._schema_fingerprint())
        self.assertEqual(len(seen), 4)

    def test_django_version_and_installed_apps_change_the_fingerprint(self):
        baseline = test_runner._schema_fingerprint()
        with patch.object(test_runner.django, "get_version", return_value="0.0"):
            self.assertNotEqual(test_runner._schema_fingerprint(), baseline)
        with patch.object(test_runner, "settings", SimpleNamespace(INSTALLED_APPS=["only.this"])):
            self.assertNotEqual(test_runner._schema_fingerprint(), baseline)
