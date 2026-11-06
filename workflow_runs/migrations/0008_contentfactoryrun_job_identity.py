from django.db import migrations, models


class Migration(migrations.Migration):
    """Preserve Job read identity without changing Run authority or recency."""

    dependencies = [
        ("workflow_runs", "0007_contentfactoryrun_slack_thread_indexes"),
    ]

    operations = [
        migrations.AddField(
            model_name="contentfactoryrun",
            name="job_domain",
            field=models.CharField(blank=True, max_length=255, null=True),
        ),
        migrations.AddField(
            model_name="contentfactoryrun",
            name="job_slack_user_id",
            field=models.CharField(blank=True, max_length=50, null=True),
        ),
        migrations.AddField(
            model_name="contentfactoryrun",
            name="job_created_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="contentfactoryrun",
            name="job_updated_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
