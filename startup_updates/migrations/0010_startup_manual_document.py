import uuid

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("founder_tools", "0006_repair_company_organization_column"),
        ("startup_updates", "0009_startup_data_deletion_request"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="StartupManualDocument",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("original_filename", models.CharField(max_length=512)),
                ("content_type", models.CharField(blank=True, default="", max_length=255)),
                ("file_size_bytes", models.PositiveIntegerField(default=0)),
                ("storage_path", models.CharField(max_length=1024, unique=True)),
                (
                    "extraction_status",
                    models.CharField(
                        choices=[
                            ("pending", "Pending"),
                            ("hydrated", "Hydrated"),
                            ("processed", "Processed"),
                            ("unsupported", "Unsupported"),
                            ("error", "Error"),
                        ],
                        db_index=True,
                        default="pending",
                        max_length=20,
                    ),
                ),
                ("extracted_text", models.TextField(blank=True, default="")),
                ("text_size_chars", models.PositiveIntegerField(default=0)),
                ("parse_notes", models.TextField(blank=True, default="")),
                ("last_error", models.TextField(blank=True, default="")),
                ("metadata", models.JSONField(blank=True, default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "company",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="manual_documents",
                        to="founder_tools.viberaisingcompany",
                    ),
                ),
                (
                    "created_by",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="startup_manual_documents",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "organization",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="manual_documents",
                        to="organizations.organization",
                    ),
                ),
            ],
            options={
                "db_table": "integrations_startupmanualdocument",
                "ordering": ["-created_at"],
            },
        ),
        migrations.AddIndex(
            model_name="startupmanualdocument",
            index=models.Index(fields=["organization", "created_at"], name="manual_doc_org_created_idx"),
        ),
        migrations.AddIndex(
            model_name="startupmanualdocument",
            index=models.Index(fields=["company", "created_at"], name="manual_doc_company_created_idx"),
        ),
        migrations.AddIndex(
            model_name="startupmanualdocument",
            index=models.Index(fields=["created_by", "created_at"], name="manual_doc_user_created_idx"),
        ),
    ]
