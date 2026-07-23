from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("startup_updates", "0019_merge_20260723_0335"),
    ]

    operations = [
        migrations.AddField(
            model_name="lumaeventselection",
            name="registration_count",
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="lumaeventselection",
            name="checked_in_count",
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
    ]
