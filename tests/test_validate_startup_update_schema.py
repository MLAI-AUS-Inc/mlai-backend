from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, TestCase

from startup_updates.management.commands.validate_startup_update_schema import (
    STARTUP_UPDATE_SCHEMA_REQUIREMENTS,
)


class FakePostgresCursor:
    def __init__(self, *, table_exists=True, columns=None):
        self.table_exists = table_exists
        self.columns = columns or set()
        self._rows = []
        self.executed_sql = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def execute(self, sql, params=None):
        self.executed_sql.append(sql)
        if "information_schema.tables" in sql:
            self._rows = [(self.table_exists,)]
        elif "information_schema.columns" in sql:
            self._rows = [(column,) for column in sorted(self.columns)]
        else:
            raise AssertionError(f"Unexpected SQL: {sql}")

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class FakePostgresConnection:
    vendor = "postgresql"

    def __init__(self, *, table_exists=True, columns=None):
        self.cursor_instance = FakePostgresCursor(table_exists=table_exists, columns=columns)

    def cursor(self):
        return self.cursor_instance


class ValidateStartupUpdateSchemaCommandTests(SimpleTestCase):
    def _run_command(self, connection):
        with patch(
            "startup_updates.management.commands.validate_startup_update_schema.connections"
        ) as mock_connections:
            mock_connections.__getitem__.return_value = connection
            out = StringIO()
            call_command("validate_startup_update_schema", stdout=out)
            return out.getvalue()

    def test_command_succeeds_when_required_slack_columns_exist(self):
        required_columns = STARTUP_UPDATE_SCHEMA_REQUIREMENTS[0].column_names
        output = self._run_command(FakePostgresConnection(columns=required_columns))

        self.assertIn("Startup update schema is valid.", output)

    def test_command_fails_with_missing_slack_relevance_columns(self):
        required_columns = set(STARTUP_UPDATE_SCHEMA_REQUIREMENTS[0].column_names)
        required_columns.remove("heuristic_score")
        required_columns.remove("relevance_label")

        with self.assertRaises(CommandError) as context:
            self._run_command(FakePostgresConnection(columns=required_columns))

        message = str(context.exception)
        self.assertIn("integrations_slackthreadartifact is missing required column(s)", message)
        self.assertIn("heuristic_score", message)
        self.assertIn("relevance_label", message)

    def test_command_fails_with_missing_slack_thread_table(self):
        with self.assertRaises(CommandError) as context:
            self._run_command(FakePostgresConnection(table_exists=False))

        self.assertIn(
            "Missing required startup update table: integrations_slackthreadartifact.",
            str(context.exception),
        )


class ValidateStartupUpdateSchemaDatabaseTests(TestCase):
    def test_command_succeeds_against_migrated_database(self):
        out = StringIO()

        call_command("validate_startup_update_schema", stdout=out)

        self.assertIn("Startup update schema is valid.", out.getvalue())
