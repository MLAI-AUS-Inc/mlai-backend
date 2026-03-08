from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0029_add_research_memory_fields"),
    ]

    operations = [
        migrations.CreateModel(
            name="ContentFactoryRun",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("run_id", models.CharField(db_index=True, max_length=100, unique=True)),
                ("workflow", models.CharField(db_index=True, max_length=50)),
                ("domain", models.CharField(blank=True, db_index=True, default="", max_length=255)),
                ("github_repo", models.CharField(blank=True, default="", max_length=255)),
                ("slack_user_id", models.CharField(blank=True, default="", max_length=50)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("queued", "Queued"),
                            ("running", "Running"),
                            ("completed", "Completed"),
                            ("failed", "Failed"),
                            ("blocked", "Blocked"),
                            ("awaiting_confirmation", "Awaiting Confirmation"),
                            ("awaiting_approval", "Awaiting Approval"),
                            ("denied", "Denied"),
                        ],
                        db_index=True,
                        default="queued",
                        max_length=40,
                    ),
                ),
                ("current_step", models.CharField(blank=True, default="", max_length=120)),
                (
                    "approval_state",
                    models.CharField(
                        choices=[
                            ("not_required", "Not Required"),
                            ("approval_required", "Approval Required"),
                            ("approved", "Approved"),
                            ("denied", "Denied"),
                        ],
                        default="not_required",
                        max_length=30,
                    ),
                ),
                ("artifact_root", models.CharField(blank=True, default="", max_length=500)),
                ("step_order", models.JSONField(blank=True, default=list)),
                ("acceptance_summary", models.JSONField(blank=True, default=dict)),
                ("verification_summary", models.JSONField(blank=True, default=dict)),
                ("run_request", models.JSONField(blank=True, default=dict)),
                ("result", models.JSONField(blank=True, default=dict)),
                ("error", models.TextField(blank=True, default="")),
                ("resume_available", models.BooleanField(default=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "db_table": "content_factory_run",
                "ordering": ["-updated_at"],
            },
        ),
        migrations.CreateModel(
            name="ContentFactoryRunStep",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("step_key", models.CharField(max_length=120)),
                ("display_order", models.IntegerField(default=0)),
                ("required", models.BooleanField(default=True)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("pending", "Pending"),
                            ("running", "Running"),
                            ("completed", "Completed"),
                            ("failed", "Failed"),
                            ("blocked", "Blocked"),
                            ("skipped", "Skipped"),
                        ],
                        db_index=True,
                        default="pending",
                        max_length=20,
                    ),
                ),
                ("attempts", models.IntegerField(default=0)),
                ("message", models.TextField(blank=True, default="")),
                ("started_at", models.DateTimeField(blank=True, null=True)),
                ("completed_at", models.DateTimeField(blank=True, null=True)),
                ("error", models.TextField(blank=True, default="")),
                ("latest_attempt_path", models.CharField(blank=True, default="", max_length=500)),
                ("artifacts", models.JSONField(blank=True, default=list)),
                (
                    "run",
                    models.ForeignKey(on_delete=models.deletion.CASCADE, related_name="steps", to="core.contentfactoryrun"),
                ),
            ],
            options={
                "db_table": "content_factory_run_step",
                "ordering": ["display_order", "id"],
                "unique_together": {("run", "step_key")},
            },
        ),
        migrations.CreateModel(
            name="ContentFactoryRunStepAttempt",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("attempt", models.IntegerField()),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("pending", "Pending"),
                            ("running", "Running"),
                            ("completed", "Completed"),
                            ("failed", "Failed"),
                            ("blocked", "Blocked"),
                            ("skipped", "Skipped"),
                        ],
                        default="pending",
                        max_length=20,
                    ),
                ),
                ("message", models.TextField(blank=True, default="")),
                ("started_at", models.DateTimeField(blank=True, null=True)),
                ("completed_at", models.DateTimeField(blank=True, null=True)),
                ("artifacts", models.JSONField(blank=True, default=list)),
                ("error", models.TextField(blank=True, default="")),
                ("input_path", models.CharField(blank=True, default="", max_length=500)),
                ("output_path", models.CharField(blank=True, default="", max_length=500)),
                ("notes_path", models.CharField(blank=True, default="", max_length=500)),
                ("status_path", models.CharField(blank=True, default="", max_length=500)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "step",
                    models.ForeignKey(
                        on_delete=models.deletion.CASCADE,
                        related_name="attempt_history",
                        to="core.contentfactoryrunstep",
                    ),
                ),
            ],
            options={
                "db_table": "content_factory_run_step_attempt",
                "ordering": ["step_id", "attempt"],
                "unique_together": {("step", "attempt")},
            },
        ),
        migrations.AddIndex(
            model_name="contentfactoryrun",
            index=models.Index(fields=["workflow", "status"], name="cf_run_workflow_status_idx"),
        ),
        migrations.AddIndex(
            model_name="contentfactoryrun",
            index=models.Index(fields=["domain", "status"], name="cf_run_domain_status_idx"),
        ),
        migrations.AddIndex(
            model_name="contentfactoryrunstep",
            index=models.Index(fields=["run", "display_order"], name="cf_step_run_order_idx"),
        ),
        migrations.AddIndex(
            model_name="contentfactoryrunstep",
            index=models.Index(fields=["run", "status"], name="cf_step_run_status_idx"),
        ),
    ]
