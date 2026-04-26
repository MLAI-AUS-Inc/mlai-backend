import uuid

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("content_factory", "0003_website_baseline_snapshot"),
        ("workflow_runs", "0001_split_content_factory_apps"),
    ]

    operations = [
        migrations.CreateModel(
            name="VibeMarketingComponentComment",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("component_id", models.CharField(db_index=True, max_length=255)),
                ("component_type", models.CharField(blank=True, default="", max_length=120)),
                ("component_label", models.CharField(blank=True, default="", max_length=255)),
                ("source_section_id", models.CharField(blank=True, default="", max_length=255)),
                ("selector", models.CharField(blank=True, default="", max_length=500)),
                ("body", models.TextField()),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("draft", "Draft"),
                            ("submitted", "Submitted"),
                            ("applied", "Applied"),
                            ("superseded", "Superseded"),
                        ],
                        db_index=True,
                        default="draft",
                        max_length=20,
                    ),
                ),
                ("batch_id", models.CharField(blank=True, db_index=True, default="", max_length=100)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "actor",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="vibe_marketing_component_comments",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "run",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="component_comments",
                        to="workflow_runs.contentfactoryrun",
                    ),
                ),
            ],
            options={
                "db_table": "content_factory_vibe_component_comment",
                "ordering": ["created_at", "id"],
            },
        ),
        migrations.AddIndex(
            model_name="vibemarketingcomponentcomment",
            index=models.Index(fields=["run", "status"], name="vibe_comment_run_status_idx"),
        ),
        migrations.AddIndex(
            model_name="vibemarketingcomponentcomment",
            index=models.Index(fields=["run", "batch_id"], name="vibe_comment_run_batch_idx"),
        ),
    ]
