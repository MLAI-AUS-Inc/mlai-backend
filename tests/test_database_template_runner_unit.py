"""Checks for the migrated test database reuse in ``mlai/test_runner.py``.

Fakes and SQLite's own in-process memory stand in for the databases: no Django
connection is opened, no migration is applied and no PostgreSQL is contacted.
"""

import io
import os
import re
import sqlite3
import tempfile
import uuid
from contextlib import contextmanager, redirect_stdout
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
        # The configured database, until Django switches to the test database.
        self.settings_dict = {"NAME": ":memory:"}
        self.connection = None

    def ensure_connection(self):
        if self.connection is None:
            self.connection = sqlite3.connect(self.test_name, uri=True)


class FakePostgresConnection:
    """Records the statements sent on the maintenance connection."""

    vendor = "postgresql"

    def __init__(self, databases, *, test_name="test_mlai"):
        self.databases = set(databases)
        self.statements = []
        self.events = []
        self.test_name = test_name
        self.creation = SimpleNamespace(_get_test_db_name=lambda: test_name)
        self.ops = SimpleNamespace(quote_name=lambda name: f'"{name}"')
        # The configured database, until Django switches to the test database.
        self.settings_dict = {"NAME": "mlai"}

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
        # The path needs quoting inside a SQLite URI.
        self.temporary = tempfile.TemporaryDirectory(prefix="mlai runner #1 ")
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

    def run_setup(
        self, connection, django_setup, *, keepdb=False, configured=("default",),
        switches_database=True, verbosity=0, parallel=1, **setup_arguments,
    ):
        """Run the runner's database setup with Django's own setup replaced."""
        runner = TemplateDatabaseRunner(verbosity=verbosity, keepdb=keepdb, parallel=parallel)
        calls = []

        def setup_databases(self_, **kwargs):
            calls.append(self_.keepdb)
            selected = kwargs.get("aliases")
            if switches_database and hasattr(connection, "test_name") and (
                selected is None or "default" in selected
            ):
                # Django points the connection at the test database it built.
                connection.settings_dict["NAME"] = connection.test_name
            return django_setup(self_)

        registry = {alias: connection for alias in configured}
        with (
            patch.object(test_runner, "connections", registry),
            patch.object(DiscoverRunner, "setup_databases", setup_databases),
        ):
            result = runner.setup_databases(**setup_arguments)
        return runner, calls, result


class SqliteReuseTests(RunnerCase):
    def migrate(self, connection):
        """Stand in for Django building the schema from migrations."""

        def django_setup(runner):
            connection.ensure_connection()
            connection.connection.execute("CREATE TABLE django_migrations (name TEXT)")
            connection.connection.execute("CREATE TABLE thing (name TEXT)")
            connection.connection.execute("INSERT INTO thing VALUES ('seed')")
            connection.connection.commit()
            return "old-config"

        return django_setup

    def build_copy(self, **options):
        connection = FakeSqliteConnection()
        self.addCleanup(lambda: connection.connection.close())
        return self.run_setup(connection, self.migrate(connection), **options)

    def assert_rebuilt(self, **options):
        """A run that could not use the saved state migrates and saves again."""
        connection = FakeSqliteConnection()
        self.addCleanup(lambda: connection.connection.close())
        runner, calls, _result = self.run_setup(connection, self.migrate(connection), **options)
        self.assertEqual(calls, [False])
        self.assertFalse(runner.keepdb)
        return connection

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

    def test_a_copy_directory_owned_by_someone_else_is_not_trusted(self):
        other_user = os.getuid() + 1
        (Path(self.temporary.name) / f"mlai-test-database-{other_user}").mkdir(mode=0o700)

        with patch.object(os, "getuid", return_value=other_user):
            self.assertIsNone(test_runner._sqlite_copy_directory())

    def test_a_copy_directory_everyone_can_write_is_not_trusted(self):
        self.build_copy()
        self.copy_directory.chmod(0o707)

        self.assertIsNone(test_runner._sqlite_copy_directory())

    def test_a_file_in_place_of_the_copy_directory_is_not_trusted(self):
        self.copy_directory.write_text("not a directory")

        self.assertIsNone(test_runner._sqlite_copy_directory())
        self.assert_rebuilt()
        self.assertEqual(self.copy_directory.read_text(), "not a directory")

    def test_the_copy_appears_only_once_it_is_complete(self):
        seen = []
        real_replace = os.replace

        def replace(source, target):
            written = sqlite3.connect(source)
            seen.append((Path(target).exists(), written.execute("SELECT name FROM thing").fetchall()))
            written.close()
            real_replace(source, target)

        with patch.object(os, "replace", replace):
            self.build_copy()

        self.assertEqual(seen, [(False, [("seed",)])])
        self.assertEqual([path.name for path in self.copy_directory.iterdir()], [self.copy_path.name])

    def test_a_damaged_copy_is_rebuilt_and_replaced(self):
        for damage in (b"not a database" * 400, b""):
            with self.subTest(size=len(damage)):
                self.build_copy()
                self.copy_path.write_bytes(damage)

                self.assert_rebuilt()

                saved = sqlite3.connect(self.copy_path)
                self.assertEqual(saved.execute("SELECT name FROM thing").fetchall(), [("seed",)])
                saved.close()
                self.copy_path.unlink()

    def test_a_selection_without_database_tests_neither_loads_nor_saves(self):
        connection = FakeSqliteConnection()

        runner, calls, _result = self.run_setup(connection, lambda runner: [], aliases={})

        self.assertEqual(calls, [False])
        self.assertFalse(runner.keepdb)
        self.assertFalse(self.copy_directory.exists())

        self.build_copy()
        runner, calls, _result = self.run_setup(FakeSqliteConnection(), lambda runner: [], aliases={})

        self.assertEqual(calls, [False])
        self.assertFalse(runner.keepdb)

    def test_the_configured_database_is_never_saved_as_the_copy(self):
        # Django returned without switching to a test database.
        connection = FakeSqliteConnection()

        runner, calls, _result = self.run_setup(connection, lambda runner: [], switches_database=False)

        self.assertEqual(calls, [False])
        self.assertIsNone(connection.connection)
        self.assertEqual(list(self.copy_directory.iterdir()), [])

    def test_reuse_is_reported_at_normal_verbosity(self):
        output = io.StringIO()
        with redirect_stdout(output):
            self.build_copy(verbosity=1)
            connection = FakeSqliteConnection()
            self.run_setup(connection, lambda runner: connection.ensure_connection(), verbosity=1)
            self.addCleanup(connection.connection.close)

        self.assertEqual(output.getvalue().splitlines(), [
            f"Saved migrated test database as copy {self.copy_path.name}.",
            f"Loaded test database from migrated copy {self.copy_path.name}.",
        ])


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
        self.assert_django_default(FakeSqliteConnection(), configured=("default", "replica"))

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

    def test_the_configured_database_never_becomes_the_template(self):
        for options in ({"aliases": {}}, {"switches_database": False}):
            with self.subTest(options=options):
                connection = FakePostgresConnection({"postgres", "mlai"})

                runner, calls, result = self.run_setup(connection, lambda runner: [], **options)

                self.assertEqual(calls, [False])
                self.assertFalse(runner.keepdb)
                self.assertEqual(result, [])
                self.assertEqual([sql for sql in connection.statements if sql.startswith("CREATE")], [])
                self.assertNotIn("close", connection.events)

    def test_a_long_database_name_keeps_the_whole_fingerprint(self):
        long_name = "test_" + "x" * 58
        connection = FakePostgresConnection({"postgres"}, test_name=long_name)

        self.run_setup(connection, lambda runner: "old-config")

        template = re.fullmatch(r'CREATE DATABASE "(.+)" TEMPLATE ".+"', connection.statements[-1]).group(1)
        self.assertEqual(len(template), 63)
        self.assertTrue(template.endswith(f"_tpl_{FINGERPRINT}"))

    def test_a_parallel_run_still_starts_from_the_template(self):
        connection = FakePostgresConnection({"postgres", "mlai", "test_mlai", self.template, "test_mlai_1"})

        runner, calls, _result = self.run_setup(connection, lambda runner: "old-config", parallel=2)

        self.assertEqual(calls, [True])
        self.assertIn('DROP DATABASE IF EXISTS "test_mlai_1"', connection.statements)
        self.assertIn(f'CREATE DATABASE "test_mlai" TEMPLATE "{self.template}"', connection.statements)

    def test_worker_clones_are_matched_by_the_literal_database_name(self):
        name = "test.mlai"
        connection = FakePostgresConnection(
            {"postgres", name, f"{name}_tpl_{FINGERPRINT}", f"{name}_1", "testxmlai_1"},
            test_name=name,
        )

        self.run_setup(connection, lambda runner: "old-config")

        dropped = [sql for sql in connection.statements if sql.startswith("DROP")]
        self.assertEqual(dropped, [f'DROP DATABASE IF EXISTS "{name}_1"', f'DROP DATABASE IF EXISTS "{name}"'])

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
        def with_apps(*names):
            with patch.object(test_runner, "settings", SimpleNamespace(INSTALLED_APPS=list(names))):
                return test_runner._schema_fingerprint()

        baseline = with_apps("one.app", "two.app")
        with patch.object(test_runner.django, "get_version", return_value="0.0"):
            self.assertNotEqual(with_apps("one.app", "two.app"), baseline)
        # Same number of apps, so only the names can tell them apart.
        self.assertNotEqual(with_apps("one.app", "other.app"), baseline)
        self.assertEqual(with_apps("two.app", "one.app"), baseline)

    def test_the_order_apps_are_listed_in_does_not_change_the_fingerprint(self):
        baseline = test_runner._schema_fingerprint()
        configs = test_runner.apps.get_app_configs()
        with patch.object(test_runner, "apps", SimpleNamespace(get_app_configs=lambda: configs[::-1])):
            self.assertEqual(test_runner._schema_fingerprint(), baseline)
