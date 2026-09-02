from django.db import migrations

# Tables left behind by apps that were removed from the codebase without a
# drop migration:
# - the original `roo` agentic Slack-bot app (deleted 2025-12-16) left
#   roo_articlegeneration; the current `roo` points app never owned it.
# - `innovate_connect_alliance` (deleted 2026-05-10) had a cleanup migration,
#   but that file was lost in a later migration renumbering, so the drops are
#   re-run here defensively.
ORPHAN_TABLES = (
    "roo_articlegeneration",
    "innovate_connect_alliance_videosubmission",
    "innovate_connect_alliance_team_members",
    "innovate_connect_alliance_announcement",
    "innovate_connect_alliance_team",
)

ICA_APP_LABEL = "innovate_connect_alliance"


def drop_orphan_tables(apps, schema_editor):
    ContentType = apps.get_model("contenttypes", "ContentType")
    Permission = apps.get_model("auth", "Permission")

    stale_content_types = ContentType.objects.filter(
        app_label=ICA_APP_LABEL
    ) | ContentType.objects.filter(app_label="roo", model="articlegeneration")
    Permission.objects.filter(content_type__in=stale_content_types).delete()
    stale_content_types.delete()

    connection = schema_editor.connection
    existing_tables = set(connection.introspection.table_names())
    cascade_suffix = " CASCADE" if connection.vendor == "postgresql" else ""

    for table_name in ORPHAN_TABLES:
        if table_name in existing_tables:
            quoted_table = schema_editor.quote_name(table_name)
            schema_editor.execute(f"DROP TABLE IF EXISTS {quoted_table}{cascade_suffix}")


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0057_passwordresetemaildelivery"),
        ("contenttypes", "0002_remove_content_type_name"),
    ]

    operations = [
        migrations.RunPython(
            drop_orphan_tables,
            migrations.RunPython.noop,
        ),
    ]
