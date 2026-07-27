from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import uuid


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("org_memory", "0021_memory_selector_shadow"),
    ]

    operations = [
        migrations.CreateModel(
            name="MemoryPilotQueryAudit",
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
                ("rubric_version", models.CharField(max_length=64)),
                (
                    "risk",
                    models.CharField(
                        choices=[
                            ("standard", "Standard"),
                            ("high", "High"),
                        ],
                        db_index=True,
                        default="standard",
                        max_length=16,
                    ),
                ),
                ("answer_correct", models.BooleanField(blank=True, null=True)),
                (
                    "faithfulness_correct",
                    models.BooleanField(blank=True, null=True),
                ),
                ("abstention_correct", models.BooleanField()),
                (
                    "current_state_correct",
                    models.BooleanField(blank=True, null=True),
                ),
                ("temporal_correct", models.BooleanField(blank=True, null=True)),
                ("citation_count", models.PositiveIntegerField(default=0)),
                ("correct_citation_count", models.PositiveIntegerField(default=0)),
                (
                    "permission_leak",
                    models.BooleanField(db_index=True, default=False),
                ),
                (
                    "public_admin_leak",
                    models.BooleanField(db_index=True, default=False),
                ),
                ("idempotency_key", models.CharField(max_length=128)),
                ("batch_hash", models.CharField(max_length=64)),
                ("reviewed_at", models.DateTimeField(db_index=True)),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                (
                    "organization",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="memory_pilot_query_audits",
                        to="organizations.organization",
                    ),
                ),
                (
                    "query_log",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="pilot_audits",
                        to="org_memory.memoryquerylog",
                    ),
                ),
                (
                    "reviewer",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="reviewed_memory_pilot_queries",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "ordering": ("-reviewed_at",),
            },
        ),
        migrations.AddConstraint(
            model_name="memorypilotqueryaudit",
            constraint=models.UniqueConstraint(
                fields=("organization", "idempotency_key"),
                name="orgmem_pilot_audit_idem_uniq",
            ),
        ),
        migrations.AddConstraint(
            model_name="memorypilotqueryaudit",
            constraint=models.UniqueConstraint(
                fields=("query_log", "rubric_version"),
                name="orgmem_pilot_audit_query_rubric_uniq",
            ),
        ),
        migrations.AddConstraint(
            model_name="memorypilotqueryaudit",
            constraint=models.CheckConstraint(
                check=models.Q(
                    ("correct_citation_count__lte", models.F("citation_count"))
                ),
                name="orgmem_pilot_audit_citations_valid",
            ),
        ),
        migrations.AddIndex(
            model_name="memorypilotqueryaudit",
            index=models.Index(
                fields=("organization", "rubric_version", "reviewed_at"),
                name="orgmem_pilot_audit_window",
            ),
        ),
    ]
