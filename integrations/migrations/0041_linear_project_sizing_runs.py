import uuid

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("integrations", "0040_mandatory_reconciliation_tracking"),
    ]

    operations = [
        migrations.CreateModel(
            name="LinearProjectSizingRun",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ("idempotency_key", models.CharField(db_index=True, max_length=64, unique=True)),
                ("project_id", models.CharField(db_index=True, max_length=255)),
                ("project_name", models.CharField(max_length=500)),
                ("requested_by_slack_user_id", models.CharField(db_index=True, max_length=255)),
                ("requested_by_linear_user_id", models.CharField(max_length=255)),
                (
                    "mode",
                    models.CharField(
                        choices=[
                            ("missing_only", "Only issues without an effort label"),
                            ("replace_existing", "Replace existing effort labels"),
                        ],
                        default="missing_only",
                        max_length=32,
                    ),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("preview", "Preview"),
                            ("applying", "Applying"),
                            ("completed", "Completed"),
                            ("cancelled", "Cancelled"),
                            ("expired", "Expired"),
                        ],
                        db_index=True,
                        default="preview",
                        max_length=24,
                    ),
                ),
                ("model_name", models.CharField(max_length=100)),
                ("rubric_version", models.CharField(max_length=100)),
                ("project_context", models.JSONField(blank=True, default=dict)),
                ("source_snapshot_at", models.DateTimeField()),
                ("expires_at", models.DateTimeField(db_index=True)),
                ("last_error", models.TextField(blank=True, default="")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "db_table": "linear_project_sizing_run",
                "ordering": ["-created_at"],
            },
        ),
        migrations.CreateModel(
            name="LinearProjectSizingItem",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("issue_id", models.CharField(max_length=255)),
                ("identifier", models.CharField(blank=True, default="", max_length=100)),
                ("title", models.CharField(max_length=500)),
                ("team_id", models.CharField(max_length=255)),
                ("expected_updated_at", models.CharField(max_length=100)),
                ("original_labels", models.JSONField(blank=True, default=list)),
                ("effort_label_name", models.CharField(max_length=100)),
                ("effort_label_id", models.CharField(max_length=255)),
                ("rationale", models.CharField(max_length=280)),
                ("sizing_metadata", models.JSONField(blank=True, default=dict)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("pending", "Pending"),
                            ("processing", "Processing"),
                            ("applied", "Applied"),
                            ("already_sized", "Already sized"),
                            ("skipped_terminal", "Skipped terminal"),
                            ("conflict", "Conflict"),
                            ("failed", "Failed"),
                        ],
                        db_index=True,
                        default="pending",
                        max_length=32,
                    ),
                ),
                ("result_payload", models.JSONField(blank=True, default=dict)),
                ("last_error", models.TextField(blank=True, default="")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "run",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="items",
                        to="integrations.linearprojectsizingrun",
                    ),
                ),
            ],
            options={
                "db_table": "linear_project_sizing_item",
                "ordering": ["id"],
            },
        ),
        migrations.AddIndex(
            model_name="linearprojectsizingrun",
            index=models.Index(fields=["project_id", "status"], name="linear_size_run_project_idx"),
        ),
        migrations.AddIndex(
            model_name="linearprojectsizingitem",
            index=models.Index(fields=["run", "status"], name="linear_size_item_status_idx"),
        ),
        migrations.AddConstraint(
            model_name="linearprojectsizingitem",
            constraint=models.UniqueConstraint(fields=("run", "issue_id"), name="linear_size_run_issue_uniq"),
        ),
    ]
