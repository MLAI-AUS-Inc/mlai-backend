from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("content_factory", "0033_activate_account_email_notification_channels"),
    ]

    operations = [
        migrations.AddField(
            model_name="keywordvelocity",
            name="basis",
            field=models.CharField(default="unknown", max_length=32),
        ),
        migrations.AddField(
            model_name="keywordvelocity",
            name="is_estimated",
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name="keywordvelocity",
            name="period_label",
            field=models.CharField(blank=True, default="", max_length=64),
        ),
        migrations.AddField(
            model_name="keywordvelocity",
            name="source",
            field=models.CharField(default="unknown", max_length=32),
        ),
    ]
