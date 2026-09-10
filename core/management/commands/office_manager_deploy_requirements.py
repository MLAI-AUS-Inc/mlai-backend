"""Read-only gate for initially deploying the disabled Office Manager feature."""
from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import connection


def integration_required(database, *, enabled):
    if enabled:
        return True
    # Any historical state requires the full integration, including disabled
    # days and completed claims: late accepted messages can still need repair.
    # Immutable provenance records can describe ordinary paid bookings without
    # any Office Manager execution or pending external delivery. They do not
    # require a Slack companion. Runtime rows and unknown future tables do.
    passive_evidence_tables = {
        "roo_officemanagerprovenancereconciliation",
        "roo_officemanagerprovenancebucketrepair",
        "roo_officemanagerrefundreversalprovenance",
    }
    tables = sorted(t for t in database.introspection.table_names()
                    if t.startswith("roo_officemanager") and t not in passive_evidence_tables)
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
