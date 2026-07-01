# Generated manually for jobs feedback auto-tuning.

from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


class Migration(migrations.Migration):

    dependencies = [
        ("jobs", "0002_jobrun_execution_config"),
    ]

    operations = [
        migrations.CreateModel(
            name="JobDisqualifierCandidate",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("keyword", models.CharField(max_length=255, unique=True)),
                ("category", models.CharField(default="community", max_length=64)),
                ("status", models.CharField(choices=[("review", "Review"), ("active", "Active"), ("rejected", "Rejected")], db_index=True, default="review", max_length=32)),
                ("severity", models.CharField(choices=[("suppress", "Suppress"), ("penalize", "Penalize")], default="penalize", max_length=32)),
                ("penalty", models.FloatField(default=0.08)),
                ("signal_count", models.IntegerField(default=0)),
                ("confidence", models.FloatField(default=0.0)),
                ("last_seen_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
        ),
        migrations.CreateModel(
            name="JobFeedback",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("feedback_type", models.CharField(choices=[("good", "Good"), ("flag", "Flag"), ("disqualify", "Disqualify")], db_index=True, max_length=32)),
                ("rank", models.IntegerField(blank=True, null=True)),
                ("reason", models.CharField(blank=True, max_length=255)),
                ("keyword", models.CharField(blank=True, max_length=255)),
                ("raw_text", models.TextField(blank=True)),
                ("slack_user_id", models.CharField(blank=True, max_length=255)),
                ("slack_channel_id", models.CharField(blank=True, max_length=255)),
                ("slack_message_ts", models.CharField(blank=True, max_length=255)),
                ("processed_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("job", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="feedback", to="jobs.joblisting")),
                ("run", models.ForeignKey(blank=True, db_column="run_id", null=True, on_delete=django.db.models.deletion.CASCADE, related_name="feedback", to="jobs.jobrun", to_field="run_id")),
            ],
        ),
    ]
