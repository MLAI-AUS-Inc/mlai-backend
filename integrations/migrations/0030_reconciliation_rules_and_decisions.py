from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("integrations", "0029_xero_statement_scan_and_confidence"),
    ]

    operations = [
        migrations.CreateModel(
            name="ReconciliationRule",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=255)),
                ("scope", models.CharField(choices=[("merchant", "Merchant and date range"), ("statement_line", "One statement line")], db_index=True, default="merchant", max_length=24)),
                ("bank_narration_key", models.CharField(blank=True, default="", max_length=255)),
                ("direction", models.CharField(blank=True, default="", max_length=16)),
                ("effective_from", models.DateField(blank=True, null=True)),
                ("effective_to", models.DateField(blank=True, null=True)),
                ("proposed_action", models.CharField(choices=[("create_bank_transaction", "Create bank transaction")], default="create_bank_transaction", max_length=32)),
                ("contact_name", models.CharField(max_length=255)),
                ("account_code", models.CharField(max_length=64)),
                ("account_name", models.CharField(max_length=255)),
                ("tax_type", models.CharField(max_length=255)),
                ("description_template", models.TextField()),
                ("event_source_id", models.CharField(blank=True, default="", max_length=255)),
                ("event_tracking_option_name", models.CharField(blank=True, default="", max_length=255)),
                ("project_source_id", models.CharField(blank=True, default="", max_length=255)),
                ("project_tracking_option_name", models.CharField(blank=True, default="", max_length=255)),
                ("priority", models.IntegerField(default=100)),
                ("status", models.CharField(choices=[("proposed", "Proposed"), ("verified", "Verified"), ("revoked", "Revoked")], db_index=True, default="proposed", max_length=20)),
                ("active", models.BooleanField(db_index=True, default=False)),
                ("evidence", models.JSONField(blank=True, default=list)),
                ("notes", models.TextField(blank=True, default="")),
                ("verified_by_slack_id", models.CharField(blank=True, default="", max_length=100)),
                ("verified_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("organization", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="reconciliation_rules", to="organizations.organization")),
                ("statement_line", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name="reconciliation_rules", to="integrations.xerostatementlinesnapshot")),
            ],
            options={
                "db_table": "reconciliation_rule",
                "indexes": [
                    models.Index(fields=["organization", "status", "active", "scope"], name="recon_rule_org_status_idx"),
                    models.Index(fields=["organization", "bank_narration_key", "direction"], name="recon_rule_org_merchant_idx"),
                ],
            },
        ),
        migrations.CreateModel(
            name="ReconciliationDecision",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("decision_key", models.CharField(max_length=64, unique=True)),
                ("run_id", models.CharField(blank=True, db_index=True, default="", max_length=255)),
                ("decision_type", models.CharField(choices=[("rule_applied", "Verified rule applied"), ("rule_conflict", "Verified rule conflict"), ("suggestion_saved", "Suggestion saved"), ("preview_ready", "Posting preview ready"), ("preview_blocked", "Posting preview blocked"), ("duplicate_recovered", "Existing Xero object recovered"), ("executed", "Xero object created")], db_index=True, max_length=32)),
                ("actor_type", models.CharField(choices=[("system", "System"), ("agent", "Agent"), ("admin", "Admin")], default="system", max_length=16)),
                ("actor_id", models.CharField(blank=True, default="", max_length=100)),
                ("outcome", models.JSONField(blank=True, default=dict)),
                ("evidence", models.JSONField(blank=True, default=list)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("organization", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="reconciliation_decisions", to="organizations.organization")),
                ("rule", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="decisions", to="integrations.reconciliationrule")),
                ("statement_line", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="reconciliation_decisions", to="integrations.xerostatementlinesnapshot")),
                ("suggestion", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="reconciliation_decisions", to="integrations.xerostatementsuggestion")),
            ],
            options={
                "db_table": "reconciliation_decision",
                "indexes": [
                    models.Index(fields=["organization", "statement_line", "-created_at"], name="recon_decision_org_line_idx"),
                    models.Index(fields=["organization", "decision_type", "-created_at"], name="recon_decision_org_type_idx"),
                ],
            },
        ),
    ]
