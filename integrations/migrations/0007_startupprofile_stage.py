from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("integrations", "0006_gmailthreadartifact_startupprofile_gmailsynccursor_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="startupprofile",
            name="stage",
            field=models.CharField(blank=True, default="", max_length=64),
        ),
    ]
