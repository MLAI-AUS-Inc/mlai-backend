from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0038_merge_content_factory_schema_branches"),
    ]

    operations = [
        migrations.AddField(
            model_name="organizationcontentconfig",
            name="default_timezone",
            field=models.CharField(
                blank=True,
                default="",
                help_text="Default IANA timezone for scheduled content suggestions when a user timezone is unavailable",
                max_length=64,
            ),
        ),
        migrations.CreateModel(
            name="ScheduledDiscoveryDispatch",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("slack_user_id", models.CharField(db_index=True, max_length=50)),
                ("domain", models.CharField(db_index=True, max_length=255)),
                ("timezone", models.CharField(default="Australia/Melbourne", max_length=64)),
                ("local_date", models.DateField(db_index=True)),
                ("trigger_source", models.CharField(db_index=True, default="daily_scheduler", max_length=50)),
                (
                    "state",
                    models.CharField(
                        choices=[
                            ("queued", "Queued"),
                            ("topic_selection_sent", "Topic Selection Sent"),
                            ("confirmed", "Confirmed"),
                            ("cancelled", "Cancelled"),
                            ("failed", "Failed"),
                            ("failed_timeout", "Failed Timeout"),
                        ],
                        db_index=True,
                        default="queued",
                        max_length=30,
                    ),
                ),
                ("content_factory_job_id", models.CharField(blank=True, db_index=True, default="", max_length=100)),
                ("last_error", models.TextField(blank=True, default="")),
                ("slack_channel_id", models.CharField(blank=True, default="", max_length=100)),
                ("slack_message_ts", models.CharField(blank=True, default="", max_length=50)),
                ("slack_thread_ts", models.CharField(blank=True, default="", max_length=50)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "db_table": "scheduled_discovery_dispatch",
                "ordering": ["-local_date", "-updated_at"],
            },
        ),
        migrations.AddConstraint(
            model_name="scheduleddiscoverydispatch",
            constraint=models.UniqueConstraint(
                fields=("slack_user_id", "domain", "local_date", "trigger_source"),
                name="scheduled_discovery_dispatch_unique_target_day",
            ),
        ),
        migrations.AddIndex(
            model_name="scheduleddiscoverydispatch",
            index=models.Index(fields=["state", "updated_at"], name="sched_disc_state_updated_idx"),
        ),
        migrations.AddIndex(
            model_name="scheduleddiscoverydispatch",
            index=models.Index(fields=["domain", "slack_user_id", "state"], name="sched_disc_target_state_idx"),
        ),
    ]
