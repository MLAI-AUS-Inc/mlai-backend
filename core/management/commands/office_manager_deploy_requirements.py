"""Read-only gate for initially deploying the disabled Office Manager feature."""
from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import connection


def integration_required(database, *, enabled):
    if enabled:
        return True
    # Any historical state requires the full integration, including disabled
    # days and completed claims: late accepted messages can still need repair.
    tables = sorted(t for t in database.introspection.table_names()
                    if t.startswith("roo_officemanager"))
    with database.cursor() as cursor:
        for table in tables:
            cursor.execute(f"SELECT 1 FROM {database.ops.quote_name(table)} LIMIT 1")
            if cursor.fetchone() is not None:
                return True
    # Missing/empty tables are safe only after the independent migration
    # identity/provenance audit in deploy.sh has passed. DB errors propagate.
    return False


class Command(BaseCommand):
    help = "Report whether Office Manager deployment needs its live companion."

    def handle(self, *args, **options):
        required = integration_required(connection, enabled=settings.OFFICE_MANAGER_ENABLED)
        self.stdout.write("true" if required else "false")
