from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0044_alter_contentfactoryjob_status_blocked"),
    ]

    operations = [
        migrations.AddField(
            model_name="organizationcontentconfig",
            name="repo_execution_contract",
            field=models.JSONField(
                blank=True,
                default=dict,
                help_text="Runtime family and command contract used to verify and publish this repository.",
            ),
        ),
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
                    ("blocked", "Blocked"),
                    ("pr_opened", "PR Opened"),
                    ("needs_review", "Needs Review"),
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
