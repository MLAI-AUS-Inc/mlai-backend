from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone
import uuid


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("org_memory", "0022_memory_pilot_query_audit"),
    ]

    operations = [
        migrations.CreateModel(
            name="MemoryPilotDeployment",
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
                (
                    "state",
                    models.CharField(
                        choices=[
                            ("staged", "Staged"),
                            ("active", "Active"),
                            ("suspended", "Suspended"),
                        ],
                        db_index=True,
                        default="staged",
                        max_length=16,
                    ),
                ),
                (
                    "approval_manifest_hash",
                    models.CharField(db_index=True, max_length=64),
                ),
                ("approval_review_due_at", models.DateTimeField(db_index=True)),
                ("allowlist_key_version", models.CharField(max_length=64)),
                ("actor_ref_hashes", models.JSONField(default=list)),
                ("context_ref_hashes", models.JSONField(default=list)),
                ("approved_provider_count", models.PositiveIntegerField()),
                ("approved_source_scope_count", models.PositiveIntegerField()),
                ("stage_idempotency_key", models.CharField(max_length=128)),
                (
                    "activation_idempotency_key",
                    models.CharField(blank=True, default="", max_length=128),
                ),
                (
                    "staged_at",
                    models.DateTimeField(
                        db_index=True,
                        default=django.utils.timezone.now,
                    ),
                ),
                (
                    "activated_at",
                    models.DateTimeField(blank=True, db_index=True, null=True),
                ),
                (
                    "suspended_at",
                    models.DateTimeField(blank=True, db_index=True, null=True),
                ),
                (
                    "suspension_reason",
                    models.CharField(
                        blank=True,
                        choices=[
                            ("manual_stop", "Manual stop"),
                            ("suspected_leak", "Suspected leak"),
                            ("approval_revoked", "Approval revoked"),
                            ("scope_changed", "Scope changed"),
                            ("credential_rotation", "Credential rotation"),
                            ("pilot_complete", "Pilot complete"),
                            ("superseded", "Superseded"),
                        ],
                        default="",
                        max_length=32,
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "activated_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="activated_memory_pilot_deployments",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "organization",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="memory_pilot_deployments",
                        to="organizations.organization",
                    ),
                ),
                (
                    "staged_by",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="staged_memory_pilot_deployments",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "suspended_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="suspended_memory_pilot_deployments",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "ordering": ("-created_at",),
            },
        ),
        migrations.AddConstraint(
            model_name="memorypilotdeployment",
            constraint=models.UniqueConstraint(
                fields=("organization", "stage_idempotency_key"),
                name="orgmem_pilot_stage_idem_uniq",
            ),
        ),
        migrations.AddConstraint(
            model_name="memorypilotdeployment",
            constraint=models.UniqueConstraint(
                fields=(
                    "organization",
                    "approval_manifest_hash",
                    "allowlist_key_version",
                ),
                name="orgmem_pilot_approval_key_uniq",
            ),
        ),
        migrations.AddConstraint(
            model_name="memorypilotdeployment",
            constraint=models.UniqueConstraint(
                condition=models.Q(("state", "staged")),
                fields=("organization",),
                name="orgmem_pilot_one_staged",
            ),
        ),
        migrations.AddConstraint(
            model_name="memorypilotdeployment",
            constraint=models.UniqueConstraint(
                condition=models.Q(("state", "active")),
                fields=("organization",),
                name="orgmem_pilot_one_active",
            ),
        ),
        migrations.AddConstraint(
            model_name="memorypilotdeployment",
            constraint=models.UniqueConstraint(
                condition=models.Q(
                    ("activation_idempotency_key", ""),
                    _negated=True,
                ),
                fields=("organization", "activation_idempotency_key"),
                name="orgmem_pilot_activate_idem_uniq",
            ),
        ),
        migrations.AddConstraint(
            model_name="memorypilotdeployment",
            constraint=models.CheckConstraint(
                check=models.Q(
                    models.Q(
                        ("activated_at__isnull", True),
                        ("state", "staged"),
                        ("suspended_at__isnull", True),
                        ("suspension_reason", ""),
                    ),
                    models.Q(
                        ("activated_at__isnull", False),
                        ("state", "active"),
                        ("suspended_at__isnull", True),
                        ("suspension_reason", ""),
                    ),
                    models.Q(
                        ("state", "suspended"),
                        ("suspended_at__isnull", False),
                    ),
                    _connector="OR",
                ),
                name="orgmem_pilot_state_timestamps",
            ),
        ),
        migrations.AddIndex(
            model_name="memorypilotdeployment",
            index=models.Index(
                fields=("organization", "state", "approval_review_due_at"),
                name="orgmem_pilot_runtime_state",
            ),
        ),
    ]
