from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0036_merge_content_factory_progress_and_publish_targets"),
    ]

    operations = [
        migrations.AlterField(
            model_name="organizationcontentconfig",
            name="article_system",
            field=models.JSONField(
                blank=True,
                default=dict,
                help_text="Canonical article/blog system readiness state for this organization",
            ),
        ),
    ]
