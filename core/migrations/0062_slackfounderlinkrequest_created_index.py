from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0061_clear_synthetic_web_slack_ids"),
    ]

    operations = [
        migrations.AddIndex(
            model_name="slackfounderlinkrequest",
            index=models.Index(
                fields=["slack_user", "created_at"],
                name="core_sflr_user_created_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="slackfounderlinkrequest",
            index=models.Index(
                fields=["created_at"],
                name="core_sflr_created_idx",
            ),
        ),
    ]
