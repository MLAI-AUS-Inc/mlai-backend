from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("integrations", "0033_xero_statement_reconciliation_outcomes"),
    ]

    operations = [
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
                    ("learning_rule_promoted", "Learning candidate promoted"),
                    ("learning_rule_rejected", "Learning candidate rejected"),
                    ("duplicate_recovered", "Existing Xero object recovered"),
                    ("executed", "Xero object created"),
                ],
                db_index=True,
                max_length=32,
            ),
        ),
    ]
