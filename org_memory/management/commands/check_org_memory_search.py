import json

from django.core.management.base import BaseCommand, CommandError
from django.db import connection

from org_memory.embeddings import configured_embedding_target


class Command(BaseCommand):
    help = "Preflight PostgreSQL full-text and pgvector support for organisational memory."

    def add_arguments(self, parser):
        parser.add_argument(
            "--require-vector",
            action="store_true",
            help="Fail unless PostgreSQL makes the vector extension available.",
        )
        parser.add_argument(
            "--require-installed",
            action="store_true",
            help="Fail unless the vector extension is already installed in this database.",
        )

    def handle(self, *args, **options):
        target = configured_embedding_target()
        report = {
            "database_vendor": connection.vendor,
            "embedding_model": target.model,
            "embedding_version": target.version,
            "embedding_dimensions": target.dimensions,
            "vector_available": False,
            "vector_installed": False,
            "vector_version": None,
        }
        if connection.vendor == "postgresql":
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT default_version, installed_version
                    FROM pg_available_extensions
                    WHERE name = 'vector'
                    """
                )
                row = cursor.fetchone()
            if row:
                report["vector_available"] = True
                report["vector_installed"] = bool(row[1])
                report["vector_version"] = row[1] or row[0]

        self.stdout.write(json.dumps(report, sort_keys=True))
        if options["require_vector"] and not report["vector_available"]:
            raise CommandError(
                "PostgreSQL does not expose the vector extension; refusing an unsafe memory migration."
            )
        if options["require_installed"] and not report["vector_installed"]:
            raise CommandError("The vector extension is not installed in this database.")
