from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0034_add_content_factory_billing_fields"),
    ]

    operations = [
        migrations.AddField(
            model_name="contentfactoryjob",
            name="progress_message_ts",
            field=models.CharField(blank=True, default="", max_length=50),
        ),
        migrations.AddField(
            model_name="contentfactoryjob",
            name="last_progress_milestone_key",
            field=models.CharField(blank=True, default="", max_length=100),
        ),
        migrations.AddField(
            model_name="contentfactoryjob",
            name="last_progress_updated_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="contentfactoryjob",
            name="still_working_pinged_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
