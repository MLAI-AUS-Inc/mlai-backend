import uuid

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("integrations", "0041_linear_project_sizing_runs"),
    ]

    operations = [
        migrations.CreateModel(
            name="LinearMeetingActionBatch",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("requested_by_slack_user_id", models.CharField(db_index=True, max_length=255)),
                ("slack_channel_id", models.CharField(blank=True, default="", max_length=255)),
                ("slack_thread_ts", models.CharField(blank=True, default="", max_length=255)),
                ("source_fingerprint", models.CharField(blank=True, db_index=True, default="", max_length=64)),
                ("status", models.CharField(choices=[("pending", "Pending"), ("processing", "Processing"), ("partial", "Partially completed"), ("completed", "Completed"), ("rejected", "Rejected"), ("expired", "Expired")], db_index=True, default="pending", max_length=24)),
                ("expires_at", models.DateTimeField(db_index=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={"db_table": "linear_meeting_action_batch", "ordering": ["-created_at"]},
        ),
        migrations.CreateModel(
            name="LinearMeetingActionItem",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("position", models.PositiveSmallIntegerField()),
                ("issue_input", models.JSONField(default=dict)),
                ("display", models.JSONField(blank=True, default=dict)),
                ("reason", models.TextField(blank=True, default="")),
                ("status", models.CharField(choices=[("pending", "Pending"), ("processing", "Processing"), ("approved", "Approved"), ("rejected", "Rejected"), ("failed", "Failed"), ("expired", "Expired")], db_index=True, default="pending", max_length=24)),
                ("linear_issue_payload", models.JSONField(blank=True, default=dict)),
                ("last_error", models.TextField(blank=True, default="")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("batch", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="items", to="integrations.linearmeetingactionbatch")),
            ],
            options={"db_table": "linear_meeting_action_item", "ordering": ["position", "created_at"]},
        ),
        migrations.AddConstraint(
            model_name="linearmeetingactionitem",
            constraint=models.UniqueConstraint(fields=("batch", "position"), name="linear_meeting_batch_position_uniq"),
        ),
    ]
