from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0039_scheduled_discovery_dispatch"),
    ]

    operations = [
        migrations.AlterField(
            model_name="contentfactoryjob",
            name="status",
            field=models.CharField(
                choices=[
                    ("queued", "Queued"),
                    ("researching", "Researching"),
                    ("awaiting_confirmation", "Awaiting Confirmation"),
                    ("awaiting_delivery_mode", "Awaiting Delivery Mode"),
                    ("awaiting_approval", "Awaiting Approval"),
                    ("generating", "Generating"),
                    ("confirmed", "Confirmed"),
                    ("cancelled", "Cancelled"),
                    ("completed", "Completed"),
                    ("error", "Error"),
                    ("auth_required", "Auth Required"),
                ],
                default="queued",
                max_length=30,
            ),
        ),
    ]
