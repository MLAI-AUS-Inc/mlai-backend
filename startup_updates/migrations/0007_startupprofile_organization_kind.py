from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("startup_updates", "0006_repair_slack_thread_relevance_columns"),
    ]

    operations = [
        migrations.AddField(
            model_name="startupprofile",
            name="organization_kind",
            field=models.CharField(blank=True, default="", max_length=32),
        ),
    ]
