from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("organizations", "0001_split_content_factory_apps"),
    ]

    operations = [
        migrations.AddField(
            model_name="organization",
            name="company_linkedin_url",
            field=models.URLField(blank=True, default="", max_length=512),
        ),
    ]
