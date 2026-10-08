from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("founder_tools", "0010_company_default_audience_visibility"),
    ]

    operations = [
        migrations.AlterField(
            model_name="viberaisingcompany",
            name="avatar_url",
            field=models.URLField(max_length=2048, blank=True, null=True),
        ),
    ]
