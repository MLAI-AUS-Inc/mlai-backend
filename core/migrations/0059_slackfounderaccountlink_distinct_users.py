from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0058_slackfounderaccountlink_slackfounderlinkrequest"),
    ]

    operations = [
        migrations.AddConstraint(
            model_name="slackfounderaccountlink",
            constraint=models.CheckConstraint(
                check=~models.Q(slack_user=models.F("founder_user")),
                name="core_sfal_distinct_users",
            ),
        ),
    ]
