# Generated manually for organisation identity and capability authorization.

import django.db.models.deletion
import django.utils.timezone
from django.conf import settings
from django.db import migrations, models


INITIAL_CAPABILITIES = (
    ("view_general_memory", "View general memory"),
    ("view_email_memory", "View email memory"),
    ("view_finance_memory", "View finance memory"),
    ("view_people_sensitive_memory", "View people-sensitive memory"),
    ("view_executive_memory", "View executive memory"),
    ("review_claims", "Review claims"),
    ("manage_sources", "Manage sources"),
    ("publish_knowledge", "Publish knowledge"),
    ("approve_actions", "Approve actions"),
)


def seed_capabilities_and_slack_identities(apps, schema_editor):
    Capability = apps.get_model("org_memory", "OrganizationCapability")
    Identity = apps.get_model("org_memory", "OrganizationIdentity")
    SlackIdentity = apps.get_model("org_memory", "OrganizationSlackIdentity")

    for key, name in INITIAL_CAPABILITIES:
        Capability.objects.get_or_create(key=key, defaults={"name": name})

    for slack_identity in SlackIdentity.objects.select_related("workspace", "user"):
        workspace = slack_identity.workspace
        linked_user_id = slack_identity.user_id
        verified_at = slack_identity.updated_at
        if linked_user_id and Identity.objects.filter(
            organization_id=workspace.organization_id,
            provider="slack",
            external_tenant_id=workspace.slack_team_id,
            user_id=linked_user_id,
            is_active=True,
        ).exists():
            # Preserve the external record but fail it closed for human review.
            linked_user_id = None
            verified_at = None
        Identity.objects.get_or_create(
            organization_id=workspace.organization_id,
            provider="slack",
            external_tenant_id=workspace.slack_team_id,
            external_user_id=slack_identity.slack_user_id,
            defaults={
                "user_id": linked_user_id,
                "email_at_link_time": (
                    slack_identity.user.email if slack_identity.user_id else ""
                ),
                "verified_at": verified_at,
                "is_active": slack_identity.is_active and workspace.is_active,
            },
        )


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("organizations", "0002_organization_company_linkedin_url"),
        ("org_memory", "0001_service_identity"),
    ]

    operations = [
        migrations.CreateModel(
            name="OrganizationCapability",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("key", models.SlugField(max_length=64, unique=True)),
                ("name", models.CharField(max_length=128)),
                ("description", models.TextField(blank=True, default="")),
                ("is_active", models.BooleanField(db_index=True, default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={"ordering": ("key",)},
        ),
        migrations.CreateModel(
            name="OrganizationMembership",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("is_active", models.BooleanField(db_index=True, default=True)),
                ("joined_at", models.DateTimeField(db_index=True, default=django.utils.timezone.now)),
                ("ended_at", models.DateTimeField(blank=True, db_index=True, null=True)),
                ("source", models.CharField(choices=[("manual", "Manual"), ("reviewed_backfill", "Reviewed backfill")], default="manual", max_length=32)),
                ("reviewed_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("organization", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="memory_memberships", to="organizations.organization")),
                ("reviewed_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="reviewed_org_memory_memberships", to=settings.AUTH_USER_MODEL)),
                ("user", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="organization_memory_memberships", to=settings.AUTH_USER_MODEL)),
            ],
            options={"ordering": ("organization", "user")},
        ),
        migrations.CreateModel(
            name="OrganizationRole",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("slug", models.SlugField(max_length=64)),
                ("name", models.CharField(max_length=128)),
                ("description", models.TextField(blank=True, default="")),
                ("is_active", models.BooleanField(db_index=True, default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("organization", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="memory_roles", to="organizations.organization")),
            ],
            options={"ordering": ("organization", "slug")},
        ),
        migrations.CreateModel(
            name="OrganizationIdentity",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("provider", models.CharField(choices=[("slack", "Slack"), ("google", "Google"), ("microsoft", "Microsoft"), ("notion", "Notion"), ("linear", "Linear"), ("xero", "Xero"), ("stripe", "Stripe"), ("luma", "Luma"), ("other", "Other")], db_index=True, max_length=32)),
                ("external_tenant_id", models.CharField(max_length=255)),
                ("external_user_id", models.CharField(max_length=255)),
                ("email_at_link_time", models.EmailField(blank=True, default="", max_length=254)),
                ("verified_at", models.DateTimeField(blank=True, db_index=True, null=True)),
                ("is_active", models.BooleanField(db_index=True, default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("organization", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="external_identities", to="organizations.organization")),
                ("user", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="organization_identities", to=settings.AUTH_USER_MODEL)),
            ],
            options={"ordering": ("organization", "provider", "external_tenant_id", "external_user_id")},
        ),
        migrations.CreateModel(
            name="OrganizationCapabilityGrant",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("effect", models.CharField(choices=[("allow", "Allow"), ("deny", "Deny")], default="allow", max_length=8)),
                ("valid_from", models.DateTimeField(db_index=True, default=django.utils.timezone.now)),
                ("valid_until", models.DateTimeField(blank=True, db_index=True, null=True)),
                ("reason", models.TextField(blank=True, default="")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("capability", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="grants", to="org_memory.organizationcapability")),
                ("granted_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="granted_org_memory_capabilities", to=settings.AUTH_USER_MODEL)),
                ("membership", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name="capability_grants", to="org_memory.organizationmembership")),
                ("role", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name="capability_grants", to="org_memory.organizationrole")),
            ],
            options={"ordering": ("capability", "effect", "-valid_from")},
        ),
        migrations.CreateModel(
            name="OrganizationRoleAssignment",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("valid_from", models.DateTimeField(db_index=True, default=django.utils.timezone.now)),
                ("valid_until", models.DateTimeField(blank=True, db_index=True, null=True)),
                ("reason", models.TextField(blank=True, default="")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("assigned_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="assigned_org_memory_roles", to=settings.AUTH_USER_MODEL)),
                ("membership", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="role_assignments", to="org_memory.organizationmembership")),
                ("role", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="assignments", to="org_memory.organizationrole")),
            ],
            options={
                "ordering": ("membership", "role", "-valid_from"),
                "indexes": [models.Index(fields=["membership", "valid_from", "valid_until"], name="orgmem_role_assignment_time")],
            },
        ),
        migrations.AddConstraint(
            model_name="organizationroleassignment",
            constraint=models.CheckConstraint(check=models.Q(("valid_until__isnull", True), ("valid_until__gt", models.F("valid_from")), _connector="OR"), name="orgmem_role_assignment_dates"),
        ),
        migrations.AddConstraint(
            model_name="organizationrole",
            constraint=models.UniqueConstraint(fields=("organization", "slug"), name="orgmem_organization_role_uniq"),
        ),
        migrations.AddConstraint(
            model_name="organizationmembership",
            constraint=models.UniqueConstraint(fields=("organization", "user"), name="orgmem_organization_member_uniq"),
        ),
        migrations.AddConstraint(
            model_name="organizationmembership",
            constraint=models.CheckConstraint(check=models.Q(("ended_at__isnull", True), ("ended_at__gt", models.F("joined_at")), _connector="OR"), name="orgmem_membership_dates_valid"),
        ),
        migrations.AddIndex(
            model_name="organizationidentity",
            index=models.Index(fields=["provider", "external_tenant_id", "external_user_id"], name="orgmem_ext_identity_lookup"),
        ),
        migrations.AddConstraint(
            model_name="organizationidentity",
            constraint=models.UniqueConstraint(fields=("organization", "provider", "external_tenant_id", "external_user_id"), name="orgmem_external_identity_uniq"),
        ),
        migrations.AddConstraint(
            model_name="organizationidentity",
            constraint=models.UniqueConstraint(condition=models.Q(("is_active", True), ("user__isnull", False)), fields=("organization", "provider", "external_tenant_id", "user"), name="orgmem_active_tenant_user_uniq"),
        ),
        migrations.AddIndex(
            model_name="organizationcapabilitygrant",
            index=models.Index(fields=["membership", "valid_from", "valid_until"], name="orgmem_member_grant_time"),
        ),
        migrations.AddIndex(
            model_name="organizationcapabilitygrant",
            index=models.Index(fields=["role", "valid_from", "valid_until"], name="orgmem_role_grant_time"),
        ),
        migrations.AddConstraint(
            model_name="organizationcapabilitygrant",
            constraint=models.CheckConstraint(check=models.Q(models.Q(("membership__isnull", False), ("role__isnull", True)), models.Q(("membership__isnull", True), ("role__isnull", False)), _connector="OR"), name="orgmem_grant_one_subject"),
        ),
        migrations.AddConstraint(
            model_name="organizationcapabilitygrant",
            constraint=models.CheckConstraint(check=models.Q(("valid_until__isnull", True), ("valid_until__gt", models.F("valid_from")), _connector="OR"), name="orgmem_capability_grant_dates"),
        ),
        migrations.RunPython(seed_capabilities_and_slack_identities, migrations.RunPython.noop),
    ]
