"""B3 item 3E: owner approved creation for PR review; application is separate."""
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("startup_updates", "0024_startupprofile_progress_configuration"),
        ("organizations", "0003_organization_billing_user"),
        ("workflow_runs", "0005_contentfactoryrun_reconciled_at"),
    ]

    operations = [
        migrations.CreateModel(
            name="SourceExtractionReceipt",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("provider", models.CharField(max_length=64)),
                ("source_id", models.TextField()),
                ("fingerprint", models.CharField(db_index=True, max_length=64)),
                ("output", models.JSONField(null=True)),
                ("extraction_version", models.CharField(max_length=128)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("organization", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,
                    related_name="source_extraction_receipts", to="organizations.organization")),
                ("run", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,
                    related_name="source_extraction_receipts", to="workflow_runs.contentfactoryrun")),
            ],
            options={"constraints": [models.UniqueConstraint(
                fields=("organization", "provider", "source_id", "fingerprint"), name="source_receipt_identity_uniq")]},
        ),
    ]
