from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("integrations", "0043_slack_dm_mirroring"),
    ]

    operations = [
        migrations.AddField(
            model_name="slackdmmirrorconversation",
            name="history_backfilled_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name="slackdmmirrorgrant",
            name="consent_version",
            field=models.CharField(
                default="slack-dm-mirror-v3-owner-direct-and-group",
                max_length=64,
            ),
        ),
    ]
