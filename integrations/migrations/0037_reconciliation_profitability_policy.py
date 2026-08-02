from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("integrations", "0036_xero_statement_scan_capture_metadata"),
    ]

    operations = [
        migrations.AddField(
            model_name="reconciliationprofile",
            name="humanitix_profitability_included",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="reconciliationprofile",
            name="profitability_policy_verified_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="reconciliationprofile",
            name="profitability_policy_verified_by_slack_id",
            field=models.CharField(blank=True, default="", max_length=100),
        ),
    ]
