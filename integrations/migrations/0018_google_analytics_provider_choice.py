# Adds the Google Analytics provider choice for ExternalServiceConnection and
# financial provider fields.

from django.db import migrations, models


PROVIDER_CHOICES = [
    ("stripe", "Stripe"),
    ("xero", "Xero"),
    ("bank_feed", "Bank Feed"),
    ("notion", "Notion"),
    ("google_drive", "Google Drive"),
    ("slack", "Slack"),
    ("linear", "Linear"),
    ("google_analytics", "Google Analytics"),
]


class Migration(migrations.Migration):

    dependencies = [
        ("integrations", "0017_alter_external_service_provider_choices"),
    ]

    operations = [
        migrations.AlterField(
            model_name="externalfinancialrecord",
            name="provider",
            field=models.CharField(choices=PROVIDER_CHOICES, db_index=True, max_length=32),
        ),
        migrations.AlterField(
            model_name="externalserviceconnection",
            name="provider",
            field=models.CharField(choices=PROVIDER_CHOICES, db_index=True, max_length=32),
        ),
        migrations.AlterField(
            model_name="financialaccount",
            name="provider",
            field=models.CharField(choices=PROVIDER_CHOICES, db_index=True, max_length=32),
        ),
    ]
