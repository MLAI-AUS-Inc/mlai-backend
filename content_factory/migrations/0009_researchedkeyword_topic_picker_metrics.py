from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("content_factory", "0008_researchedkeyword_difficulty_source"),
    ]

    operations = [
        migrations.AddField(
            model_name="researchedkeyword",
            name="monthly_searches",
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.AddField(
            model_name="researchedkeyword",
            name="related_keywords",
            field=models.JSONField(blank=True, default=list),
        ),
    ]
