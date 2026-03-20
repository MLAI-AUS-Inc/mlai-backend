from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0032_update_content_factory_status_choices'),
    ]

    operations = [
        migrations.AddField(
            model_name='contentfactoryjob',
            name='last_progress_milestone_index',
            field=models.IntegerField(default=0),
        ),
        migrations.AddField(
            model_name='contentfactoryjob',
            name='posted_progress_ids',
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.AddField(
            model_name='contentfactoryjob',
            name='slack_root_message_ts',
            field=models.CharField(blank=True, default='', max_length=50),
        ),
    ]
