import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("integrations", "0032_reconciliation_decision_approval_types"),
    ]

    operations = [
        migrations.AddField(
            model_name="xerostatementposting",
            name="reconciled_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="xerostatementposting",
            name="reconciled_scan",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="confirmed_postings",
                to="integrations.xerostatementscan",
            ),
        ),
        migrations.AlterField(
            model_name="reconciliationdecision",
            name="decision_type",
            field=models.CharField(
                choices=[
                    ("rule_applied", "Verified rule applied"),
                    ("rule_conflict", "Verified rule conflict"),
                    ("suggestion_saved", "Suggestion saved"),
                    ("admin_approved", "Admin approved"),
                    ("admin_rejected", "Admin rejected"),
                    ("preview_ready", "Posting preview ready"),
                    ("preview_blocked", "Posting preview blocked"),
                    ("execution_blocked", "Approved execution blocked"),
                    ("reconciled_confirmed", "Human reconciliation confirmed"),
                    ("duplicate_recovered", "Existing Xero object recovered"),
                    ("executed", "Xero object created"),
                ],
                db_index=True,
                max_length=32,
            ),
        ),
        migrations.AlterField(
            model_name="xerostatementposting",
            name="status",
            field=models.CharField(
                choices=[
                    ("previewed", "Previewed"),
                    ("ready", "Ready"),
                    ("posting", "Posting"),
                    ("match_ready", "Ready to Match"),
                    ("reconciled", "Reconciled"),
                    ("failed", "Failed"),
                ],
                db_index=True,
                default="previewed",
                max_length=20,
            ),
        ),
    ]
