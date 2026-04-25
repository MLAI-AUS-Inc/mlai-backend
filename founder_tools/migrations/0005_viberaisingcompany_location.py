from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("founder_tools", "0004_move_legacy_contenttypes"),
    ]

    operations = [
        migrations.AddField(
            model_name="viberaisingcompany",
            name="location",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
    ]
