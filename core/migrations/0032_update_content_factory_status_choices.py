from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0031_add_article_system_to_org_config"),
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
                    ("completed", "Completed"),
                    ("error", "Error"),
                    ("auth_required", "Auth Required"),
                ],
                default="queued",
                max_length=30,
            ),
        ),
        migrations.AlterField(
            model_name="contentfactoryrun",
            name="status",
            field=models.CharField(
                choices=[
                    ("queued", "Queued"),
                    ("running", "Running"),
                    ("completed", "Completed"),
                    ("failed", "Failed"),
                    ("blocked", "Blocked"),
                    ("awaiting_delivery_mode", "Awaiting Delivery Mode"),
                    ("awaiting_confirmation", "Awaiting Confirmation"),
                    ("awaiting_approval", "Awaiting Approval"),
                    ("approval_required", "Approval Required"),
                    ("denied", "Denied"),
                ],
                db_index=True,
                default="queued",
                max_length=40,
            ),
        ),
    ]
