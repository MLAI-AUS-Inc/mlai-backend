from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("integrations", "0044_slack_dm_group_history_backfill"),
    ]

    operations = [
        migrations.AddField(
            model_name="communitybridgedelivery",
            name="dependency_attempts",
            field=models.PositiveSmallIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="communitybridgedelivery",
            name="dependency_first_seen_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name="communitybridgedelivery",
            name="status",
            field=models.CharField(
                choices=[
                    ("pending", "Pending"),
                    ("waiting_parent", "Waiting for parent"),
                    ("processing", "Processing"),
                    ("completed", "Completed"),
                    ("failed", "Failed"),
                    ("dead", "Dead"),
                ],
                db_index=True,
                default="pending",
                max_length=20,
            ),
        ),
        migrations.AlterField(
            model_name="slackdmmirrordelivery",
            name="status",
            field=models.CharField(
                choices=[
                    ("pending", "Pending"),
                    ("waiting_parent", "Waiting for parent"),
                    ("processing", "Processing"),
                    ("completed", "Completed"),
                    ("failed", "Failed"),
                    ("dead", "Dead"),
                ],
                db_index=True,
                default="pending",
                max_length=20,
            ),
        ),
    ]
