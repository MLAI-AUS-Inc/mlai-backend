from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0041_organizationcontentconfig_connected_slack_user_id"),
    ]

    operations = [
        migrations.AddField(
            model_name="organizationcontentconfig",
            name="article_delivery_mode",
            field=models.CharField(blank=True, max_length=32, null=True),
        ),
    ]
