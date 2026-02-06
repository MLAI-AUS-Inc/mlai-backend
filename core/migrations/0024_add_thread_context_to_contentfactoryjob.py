from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0023_add_domain_to_paa_and_aisaturation'),
    ]

    operations = [
        migrations.AddField(
            model_name='contentfactoryjob',
            name='slack_channel_id',
            field=models.CharField(blank=True, default='', max_length=100),
        ),
        migrations.AddField(
            model_name='contentfactoryjob',
            name='slack_thread_ts',
            field=models.CharField(blank=True, default='', max_length=50),
        ),
    ]
