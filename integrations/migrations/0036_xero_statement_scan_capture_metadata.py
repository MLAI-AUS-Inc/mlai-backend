from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("integrations", "0035_event_source_provenance"),
    ]

    operations = [
        migrations.AddField(
            model_name="xerostatementscan",
            name="capture_metadata",
            field=models.JSONField(blank=True, default=dict),
        ),
    ]
