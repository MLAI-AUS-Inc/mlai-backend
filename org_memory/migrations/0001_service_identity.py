# Generated manually for the organisational-memory trust boundary.

import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("organizations", "0002_organization_company_linkedin_url"),
    ]

    operations = [
        migrations.CreateModel(
            name="ServicePrincipal",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("name", models.CharField(max_length=128, unique=True)),
                ("scopes", models.JSONField(blank=True, default=list)),
                ("allowed_surfaces", models.JSONField(blank=True, default=list)),
                ("is_active", models.BooleanField(db_index=True, default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("organization", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="service_principals", to="organizations.organization")),
            ],
            options={"ordering": ("name",)},
        ),
        migrations.CreateModel(
            name="OrganizationSlackWorkspace",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("slack_team_id", models.CharField(max_length=32, unique=True)),
                ("name", models.CharField(blank=True, default="", max_length=255)),
                ("is_active", models.BooleanField(db_index=True, default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("organization", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="slack_workspaces", to="organizations.organization")),
            ],
            options={"ordering": ("organization__name", "slack_team_id")},
        ),
        migrations.CreateModel(
            name="ServicePrincipalCredential",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("secret_hash", models.CharField(max_length=256)),
                ("token_hint", models.CharField(db_index=True, max_length=48)),
                ("expires_at", models.DateTimeField(blank=True, db_index=True, null=True)),
                ("revoked_at", models.DateTimeField(blank=True, db_index=True, null=True)),
                ("last_used_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("created_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="created_service_principal_credentials", to=settings.AUTH_USER_MODEL)),
                ("principal", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="credentials", to="org_memory.serviceprincipal")),
                ("rotated_from", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="rotations", to="org_memory.serviceprincipalcredential")),
            ],
            options={"ordering": ("-created_at",)},
        ),
        migrations.CreateModel(
            name="ServicePrincipalAuditEvent",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("event_type", models.CharField(db_index=True, max_length=64)),
                ("request_id", models.CharField(blank=True, db_index=True, default="", max_length=128)),
                ("remote_address", models.GenericIPAddressField(blank=True, null=True)),
                ("metadata", models.JSONField(blank=True, default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("credential", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="audit_events", to="org_memory.serviceprincipalcredential")),
                ("principal", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="audit_events", to="org_memory.serviceprincipal")),
            ],
            options={"ordering": ("-created_at",)},
        ),
        migrations.CreateModel(
            name="OrganizationSlackIdentity",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("slack_user_id", models.CharField(max_length=32)),
                ("is_active", models.BooleanField(db_index=True, default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("user", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="organization_slack_identities", to=settings.AUTH_USER_MODEL)),
                ("workspace", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="identities", to="org_memory.organizationslackworkspace")),
            ],
            options={"ordering": ("workspace", "slack_user_id")},
        ),
        migrations.CreateModel(
            name="ActorAssertionReceipt",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("nonce", models.CharField(max_length=128)),
                ("request_id", models.CharField(db_index=True, max_length=128)),
                ("event_id", models.CharField(db_index=True, max_length=128)),
                ("expires_at", models.DateTimeField(db_index=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("principal", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="actor_assertion_receipts", to="org_memory.serviceprincipal")),
            ],
        ),
        migrations.AddConstraint(
            model_name="organizationslackidentity",
            constraint=models.UniqueConstraint(fields=("workspace", "slack_user_id"), name="orgmem_workspace_slack_user_uniq"),
        ),
        migrations.AddConstraint(
            model_name="actorassertionreceipt",
            constraint=models.UniqueConstraint(fields=("principal", "nonce"), name="orgmem_principal_assertion_nonce_uniq"),
        ),
        migrations.AddIndex(
            model_name="actorassertionreceipt",
            index=models.Index(fields=["expires_at"], name="orgmem_assertion_expiry_idx"),
        ),
    ]
