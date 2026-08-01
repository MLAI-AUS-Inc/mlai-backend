from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("org_memory", "0023_memory_pilot_deployment"),
    ]

    operations = [
        migrations.AlterField(
            model_name="memoryoutboxevent",
            name="event_type",
            field=models.CharField(
                choices=[
                    ("source_version.captured", "Source version captured"),
                    ("source.access_restored", "Source access restored"),
                    ("source.access_revoked", "Source access revoked"),
                    ("source.tombstoned", "Source tombstoned"),
                ],
                max_length=64,
            ),
        ),
    ]
