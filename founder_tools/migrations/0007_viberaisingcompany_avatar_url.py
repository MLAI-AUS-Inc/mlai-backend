from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("founder_tools", "0006_repair_company_organization_column"),
    ]

    operations = [
        migrations.AddField(
            model_name="viberaisingcompany",
            name="avatar_url",
            field=models.URLField(blank=True, null=True),
        ),
    ]
