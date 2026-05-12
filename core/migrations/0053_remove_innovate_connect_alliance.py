from django.db import migrations


ICA_APP_LABEL = "innovate_connect_alliance"
ICA_HACKATHON_SLUG = "innovate-connect-alliance"
ICA_TABLES = (
    "innovate_connect_alliance_videosubmission",
    "innovate_connect_alliance_team_members",
    "innovate_connect_alliance_announcement",
    "innovate_connect_alliance_team",
)


def remove_innovate_connect_alliance(apps, schema_editor):
    Hackathon = apps.get_model("core", "Hackathon")
    ContentType = apps.get_model("contenttypes", "ContentType")
    Permission = apps.get_model("auth", "Permission")

    Hackathon.objects.filter(slug=ICA_HACKATHON_SLUG).delete()

    content_types = ContentType.objects.filter(app_label=ICA_APP_LABEL)
    Permission.objects.filter(content_type__in=content_types).delete()
    content_types.delete()

    connection = schema_editor.connection
    existing_tables = set(connection.introspection.table_names())
    cascade_suffix = " CASCADE" if connection.vendor == "postgresql" else ""

    for table_name in ICA_TABLES:
        if table_name in existing_tables:
            quoted_table = schema_editor.quote_name(table_name)
            schema_editor.execute(f"DROP TABLE IF EXISTS {quoted_table}{cascade_suffix}")


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0052_remove_user_role_has_team"),
    ]

    operations = [
        migrations.RunPython(
            remove_innovate_connect_alliance,
            migrations.RunPython.noop,
        ),
    ]
