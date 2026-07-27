from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("integrations", "0029_humanitix_reconciliation_foundation"),
    ]

    operations = [
        migrations.CreateModel(
            name="XeroStatementScan",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("bank_account_id", models.CharField(max_length=255)),
                ("status", models.CharField(choices=[("started", "Started"), ("complete", "Complete"), ("incomplete", "Incomplete"), ("failed", "Failed")], db_index=True, default="started", max_length=20)),
                ("source", models.CharField(blank=True, default="browser", max_length=32)),
                ("requested_by", models.CharField(blank=True, default="", max_length=100)),
                ("expected_count", models.PositiveIntegerField(blank=True, null=True)),
                ("observed_count", models.PositiveIntegerField(default=0)),
                ("payload_hash", models.CharField(blank=True, default="", max_length=64)),
                ("error", models.TextField(blank=True, default="")),
                ("started_at", models.DateTimeField(auto_now_add=True)),
                ("completed_at", models.DateTimeField(blank=True, null=True)),
                ("organization", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="xero_statement_scans", to="organizations.organization")),
            ],
            options={
                "db_table": "xero_statement_scan",
                "indexes": [
                    models.Index(fields=["organization", "bank_account_id", "-started_at"], name="xero_scan_org_bank_time_idx"),
                    models.Index(fields=["organization", "status"], name="xero_scan_org_status_idx"),
                ],
            },
        ),
        migrations.CreateModel(
            name="ReconciliationPartyIdentity",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("bank_narration_key", models.CharField(max_length=255)),
                ("direction", models.CharField(blank=True, default="", max_length=16)),
                ("canonical_name", models.CharField(max_length=255)),
                ("xero_contact_id", models.CharField(blank=True, default="", max_length=255)),
                ("xero_contact_name", models.CharField(blank=True, default="", max_length=255)),
                ("linear_user_id", models.CharField(blank=True, default="", max_length=100)),
                ("linear_name", models.CharField(blank=True, default="", max_length=255)),
                ("linear_email", models.EmailField(blank=True, default="", max_length=255)),
                ("status", models.CharField(choices=[("proposed", "Proposed"), ("verified", "Verified"), ("revoked", "Revoked")], db_index=True, default="proposed", max_length=20)),
                ("confidence", models.FloatField(default=0.0)),
                ("verified_by_slack_id", models.CharField(blank=True, default="", max_length=100)),
                ("verified_at", models.DateTimeField(blank=True, null=True)),
                ("active", models.BooleanField(db_index=True, default=True)),
                ("notes", models.TextField(blank=True, default="")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("organization", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="reconciliation_party_identities", to="organizations.organization")),
            ],
            options={
                "db_table": "reconciliation_party_identity",
                "indexes": [
                    models.Index(fields=["organization", "status", "active"], name="recon_identity_org_status_idx"),
                    models.Index(fields=["organization", "linear_user_id"], name="recon_identity_org_linear_idx"),
                ],
                "constraints": [
                    models.UniqueConstraint(fields=("organization", "bank_narration_key", "direction"), name="recon_identity_org_key_dir_uniq"),
                ],
            },
        ),
        migrations.AddField(
            model_name="xerostatementlinesnapshot",
            name="create_prefill_complete",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="xerostatementlinesnapshot",
            name="last_scan",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="statement_lines", to="integrations.xerostatementscan"),
        ),
        migrations.AddField(
            model_name="xerostatementlinesnapshot",
            name="matched_xero_transaction_id",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
        migrations.AddField(
            model_name="xerostatementlinesnapshot",
            name="queue_state",
            field=models.CharField(choices=[("active", "Active"), ("reconciled", "Reconciled or removed"), ("inactive", "Inactive"), ("unknown", "Unknown")], db_index=True, default="unknown", max_length=20),
        ),
        migrations.AddField(
            model_name="xerostatementlinesnapshot",
            name="ui_mode",
            field=models.CharField(choices=[("blank_create", "Blank Create"), ("create_prefilled", "Create Prefilled"), ("green_match", "Green Match"), ("discuss", "Discuss"), ("unknown", "Unknown")], db_index=True, default="unknown", max_length=24),
        ),
        migrations.AddField(
            model_name="xerostatementsuggestion",
            name="accounting_confidence",
            field=models.FloatField(default=0.0),
        ),
        migrations.AddField(
            model_name="xerostatementsuggestion",
            name="allocation_confidence",
            field=models.FloatField(default=0.0),
        ),
        migrations.AddField(
            model_name="xerostatementsuggestion",
            name="blocking_reasons",
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.AddField(
            model_name="xerostatementsuggestion",
            name="document_confidence",
            field=models.FloatField(default=0.0),
        ),
        migrations.AddField(
            model_name="xerostatementsuggestion",
            name="execution_ready",
            field=models.BooleanField(db_index=True, default=False),
        ),
        migrations.AddField(
            model_name="xerostatementsuggestion",
            name="identity_confidence",
            field=models.FloatField(default=0.0),
        ),
        migrations.AddIndex(
            model_name="xerostatementlinesnapshot",
            index=models.Index(fields=["organization", "queue_state", "ui_mode"], name="xero_stmt_org_ui_queue_idx"),
        ),
    ]
