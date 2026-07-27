from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("startup_updates", "0016_userstartupbinding_monthly_updates_enabled"),
    ]

    operations = [
        migrations.CreateModel(
            name="LinearProjectMemberArtifact",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("linear_user_id", models.CharField(max_length=100)),
                ("name", models.CharField(blank=True, default="", max_length=255)),
                ("email", models.EmailField(blank=True, default="", max_length=255)),
                ("membership_source", models.CharField(choices=[("direct", "Direct project member"), ("team_fallback", "Team fallback")], default="direct", max_length=24)),
                ("active", models.BooleanField(db_index=True, default=True)),
                ("raw_payload", models.JSONField(blank=True, default=dict)),
                ("synced_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("connection", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="linear_project_member_artifacts", to="integrations.externalserviceconnection")),
                ("organization", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="linear_project_member_artifacts", to="organizations.organization")),
                ("project", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="members", to="startup_updates.linearprojectartifact")),
            ],
            options={
                "db_table": "integrations_linearprojectmemberartifact",
                "ordering": ["name", "linear_user_id"],
                "indexes": [
                    models.Index(fields=["organization", "active", "name"], name="linear_member_org_active_idx"),
                    models.Index(fields=["project", "active"], name="linear_member_proj_active_idx"),
                ],
                "constraints": [
                    models.UniqueConstraint(fields=("organization", "connection", "project", "linear_user_id"), name="linear_member_org_proj_user_uniq"),
                ],
            },
        ),
    ]
