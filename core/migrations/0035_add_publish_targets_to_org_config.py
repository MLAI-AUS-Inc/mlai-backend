from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0034_add_content_factory_billing_fields"),
    ]

    operations = [
        migrations.AddField(
            model_name="organizationcontentconfig",
            name="publish_targets",
            field=models.JSONField(
                blank=True,
                default=list,
                help_text="Cached publish target metadata derived from repository scans or live repo hints",
            ),
        ),
        migrations.AddField(
            model_name="organizationcontentconfig",
            name="default_publish_target_id",
            field=models.CharField(
                blank=True,
                help_text="Preferred publish target identifier for direct preview runs",
                max_length=255,
                null=True,
            ),
        ),
    ]
