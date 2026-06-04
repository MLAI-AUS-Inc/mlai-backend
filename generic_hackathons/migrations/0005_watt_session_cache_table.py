from django.core.management import call_command
from django.db import migrations


def create_watt_session_cache_table(apps, schema_editor):
    """Create the DB table backing CACHES['watt_session'] (DatabaseCache). Idempotent — Django's
    createcachetable skips the table if it already exists. Uses the migration's DB connection."""
    call_command(
        "createcachetable",
        "watt_unity_session_cache",
        database=schema_editor.connection.alias,
        verbosity=0,
    )


class Migration(migrations.Migration):

    dependencies = [
        ("generic_hackathons", "0004_generichackathonjoinrequest_and_more"),
    ]

    operations = [
        migrations.RunPython(create_watt_session_cache_table, migrations.RunPython.noop),
    ]
