from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("organizations", "0002_organization_company_linkedin_url"),
        ("startup_updates", "0008_alter_gmailmessageartifact_relevance_label_and_more"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="StartupDataDeletionRequest",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("request_id", models.CharField(db_index=True, max_length=120, unique=True)),
                ("provider", models.CharField(db_index=True, default="gmail", max_length=32)),
                ("status", models.CharField(choices=[("deleting", "Deleting"), ("deleted", "Deleted"), ("failed", "Failed")], db_index=True, default="deleting", max_length=20)),
                ("delete_derived_data", models.BooleanField(default=False)),
                ("google_account", models.EmailField(blank=True, default="", max_length=254)),
                ("reason", models.TextField(blank=True, default="")),
                ("deleted_counts", models.JSONField(blank=True, default=dict)),
                ("warnings", models.JSONField(blank=True, default=list)),
                ("metadata", models.JSONField(blank=True, default=dict)),
                ("started_at", models.DateTimeField(blank=True, null=True)),
                ("completed_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("organization", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="startup_data_deletion_requests", to="organizations.organization")),
                ("user", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="startup_data_deletion_requests", to=settings.AUTH_USER_MODEL)),
            ],
            options={
                "db_table": "integrations_startupdatadeletionrequest",
                "ordering": ["-updated_at", "-id"],
            },
        ),
        migrations.AddIndex(
            model_name="startupdatadeletionrequest",
            index=models.Index(fields=["organization", "status", "-updated_at"], name="startup_delete_org_status_idx"),
        ),
        migrations.AddIndex(
            model_name="startupdatadeletionrequest",
            index=models.Index(fields=["provider", "status"], name="startup_delete_provider_idx"),
        ),
    ]
