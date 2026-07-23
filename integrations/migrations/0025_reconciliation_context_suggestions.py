from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("integrations", "0024_stripe_xero_reconciliation"),
    ]

    operations = [
        migrations.AddField(
            model_name="reconciliationmapping",
            name="project_source_id",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
        migrations.AddField(
            model_name="reconciliationmapping",
            name="project_source_type",
            field=models.CharField(blank=True, default="", max_length=32),
        ),
        migrations.AddField(
            model_name="reconciliationmapping",
            name="reconciliation_note",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.CreateModel(
            name="ReconciliationSuggestion",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("run_id", models.CharField(blank=True, db_index=True, default="", max_length=255)),
                ("source_type", models.CharField(max_length=32)),
                ("source_id", models.CharField(max_length=255)),
                ("source_label", models.CharField(blank=True, default="", max_length=500)),
                ("event_source_type", models.CharField(blank=True, default="", max_length=32)),
                ("event_source_id", models.CharField(blank=True, default="", max_length=255)),
                ("event_tracking_option_name", models.CharField(blank=True, default="", max_length=255)),
                ("project_source_type", models.CharField(blank=True, default="", max_length=32)),
                ("project_source_id", models.CharField(blank=True, default="", max_length=255)),
                ("project_tracking_option_name", models.CharField(blank=True, default="", max_length=255)),
                ("confidence", models.FloatField(default=0.0)),
                ("rationale", models.TextField(blank=True, default="")),
                ("review_note", models.TextField(blank=True, default="")),
                ("evidence", models.JSONField(blank=True, default=list)),
                ("source_hash", models.CharField(blank=True, default="", max_length=64)),
                ("model_name", models.CharField(blank=True, default="", max_length=255)),
                ("status", models.CharField(choices=[("proposed", "Proposed"), ("approved", "Approved"), ("rejected", "Rejected"), ("superseded", "Superseded")], db_index=True, default="proposed", max_length=20)),
                ("reviewed_by_slack_id", models.CharField(blank=True, default="", max_length=100)),
                ("reviewed_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("organization", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="reconciliation_suggestions", to="organizations.organization")),
                ("payout", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="suggestions", to="integrations.stripepayoutreconciliation")),
            ],
            options={"db_table": "stripe_xero_reconciliation_suggestion"},
        ),
        migrations.AddConstraint(
            model_name="reconciliationsuggestion",
            constraint=models.UniqueConstraint(fields=("organization", "payout", "run_id", "source_type", "source_id"), name="recon_suggest_org_payout_run_source_uniq"),
        ),
        migrations.AddIndex(
            model_name="reconciliationsuggestion",
            index=models.Index(fields=["organization", "status"], name="recon_suggest_org_status_idx"),
        ),
        migrations.AddIndex(
            model_name="reconciliationsuggestion",
            index=models.Index(fields=["organization", "source_type", "source_id"], name="recon_suggest_org_source_idx"),
        ),
    ]
