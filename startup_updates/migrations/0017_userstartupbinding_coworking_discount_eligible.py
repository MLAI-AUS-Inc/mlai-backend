from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("startup_updates", "0016_userstartupbinding_monthly_updates_enabled"),
    ]

    operations = [
        migrations.AddField(
            model_name="userstartupbinding",
            name="coworking_discount_eligible",
            field=models.BooleanField(default=True),
        ),
    ]
