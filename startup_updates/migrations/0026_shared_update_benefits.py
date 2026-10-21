from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("startup_updates", "0025_sourceextractionreceipt"),
    ]

    operations = [
        # Existing approvals retain their already-earned, fixed benefit window.
        migrations.AddField(
            model_name="monthlyupdateapproval",
            name="requires_shared_update",
            field=models.BooleanField(default=False),
        ),
        # Every approval created after rollout requires community/public sharing.
        migrations.AlterField(
            model_name="monthlyupdateapproval",
            name="requires_shared_update",
            field=models.BooleanField(default=True),
        ),
    ]
