from __future__ import annotations

from dataclasses import dataclass

from django.core.management.base import BaseCommand, CommandError
from django.db import connections


@dataclass(frozen=True)
class TableRequirement:
    table_name: str
    column_names: frozenset[str]


STARTUP_UPDATE_SCHEMA_REQUIREMENTS = (
    TableRequirement(
        table_name="integrations_slackthreadartifact",
        column_names=frozenset(
            {
                "classified_at",
                "extraction_hints",
                "heuristic_reasons",
                "heuristic_score",
                "needs_extraction",
                "relevance_label",
                "relevance_reason",
                "relevance_score",
            }
        ),
    ),
)


def _postgres_table_exists(cursor, table_name: str) -> bool:
    cursor.execute(
        """
        SELECT EXISTS (
            SELECT 1
            FROM information_schema.tables
            WHERE table_schema = ANY (current_schemas(false))
              AND table_name = %s
        )
        """,
        [table_name],
    )
    row = cursor.fetchone()
    return bool(row and row[0])


def _postgres_column_names(cursor, table_name: str) -> set[str]:
    cursor.execute(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = ANY (current_schemas(false))
          AND table_name = %s
        """,
        [table_name],
    )
    return {str(row[0]) for row in cursor.fetchall()}


def _introspection_table_exists(connection, cursor, table_name: str) -> bool:
    return table_name in set(connection.introspection.table_names(cursor))


def _introspection_column_names(connection, cursor, table_name: str) -> set[str]:
    return {field.name for field in connection.introspection.get_table_description(cursor, table_name)}


def validate_startup_update_schema(connection) -> list[str]:
    errors: list[str] = []
    with connection.cursor() as cursor:
        for requirement in STARTUP_UPDATE_SCHEMA_REQUIREMENTS:
            if connection.vendor == "postgresql":
                table_exists = _postgres_table_exists(cursor, requirement.table_name)
                existing_columns = (
                    _postgres_column_names(cursor, requirement.table_name) if table_exists else set()
                )
            else:
                table_exists = _introspection_table_exists(connection, cursor, requirement.table_name)
                existing_columns = (
                    _introspection_column_names(connection, cursor, requirement.table_name)
                    if table_exists
                    else set()
                )

            if not table_exists:
                errors.append(f"Missing required startup update table: {requirement.table_name}.")
                continue

            missing_columns = sorted(requirement.column_names - existing_columns)
            if missing_columns:
                errors.append(
                    f"{requirement.table_name} is missing required column(s): "
                    f"{', '.join(missing_columns)}."
                )
    return errors


class Command(BaseCommand):
    help = "Validate production startup update database schema required by the current code."

    def add_arguments(self, parser):
        parser.add_argument(
            "--database",
            default="default",
            help="Database alias to validate. Defaults to default.",
        )

    def handle(self, *args, **options):
        database = options["database"]
        connection = connections[database]
        errors = validate_startup_update_schema(connection)
        if errors:
            details = "\n".join(f"- {error}" for error in errors)
            raise CommandError(f"Startup update schema validation failed:\n{details}")

        self.stdout.write(self.style.SUCCESS("Startup update schema is valid."))
