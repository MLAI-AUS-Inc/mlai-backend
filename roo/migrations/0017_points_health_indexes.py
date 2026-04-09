from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("roo", "0016_rename_roo_pointsr_status_8f1eab_idx_roo_pointsr_status_1880e1_idx_and_more"),
    ]

    operations = [
        migrations.AddIndex(
            model_name="ledger",
            index=models.Index(
                fields=["created_by_slack_id", "created_at"],
                name="roo_ledger_admin_week_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="coworkingbooking",
            index=models.Index(
                fields=["date", "status"],
                name="roo_cowork_date_status_idx",
            ),
        ),
    ]
