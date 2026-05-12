from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("content_factory", "0007_backfill_topic_coverage_memory"),
    ]

    operations = [
        migrations.AddField(
            model_name="researchedkeyword",
            name="difficulty_source",
            field=models.CharField(
                default="legacy_default",
                help_text="Source for difficulty: dataforseo_labs, dataforseo_bulk, missing, or legacy_default",
                max_length=30,
            ),
        ),
    ]
