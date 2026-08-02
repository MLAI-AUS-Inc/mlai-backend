from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('community_chat', '0003_device_installation_metadata'),
    ]

    operations = [
        migrations.AddField(
            model_name='communitychatbootstraptoken',
            name='origin',
            field=models.CharField(default='legacy', max_length=255),
        ),
        migrations.AddField(
            model_name='communitychatbootstraptoken',
            name='platform',
            field=models.CharField(blank=True, max_length=32),
        ),
        migrations.AddField(
            model_name='communitychatbootstraptoken',
            name='name',
            field=models.CharField(blank=True, max_length=120),
        ),
    ]
