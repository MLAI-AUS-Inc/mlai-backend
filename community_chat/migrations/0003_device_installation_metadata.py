import uuid

from django.db import migrations, models


def backfill_installation_ids(apps, schema_editor):
    CommunityChatDevice = apps.get_model('community_chat', 'CommunityChatDevice')
    for device in CommunityChatDevice.objects.filter(installation_id__isnull=True).iterator():
        device.installation_id = uuid.uuid4()
        device.save(update_fields=('installation_id',))


class Migration(migrations.Migration):
    dependencies = [
        ('community_chat', '0002_device_auth'),
    ]

    operations = [
        migrations.AddField(
            model_name='communitychatdevice',
            name='client_id',
            field=models.CharField(default='legacy', max_length=64),
        ),
        migrations.AddField(
            model_name='communitychatdevice',
            name='installation_id',
            field=models.UUIDField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='communitychatdevice',
            name='last_seen_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='communitychatdevice',
            name='name',
            field=models.CharField(blank=True, max_length=120),
        ),
        migrations.AddField(
            model_name='communitychatdevice',
            name='platform',
            field=models.CharField(blank=True, max_length=32),
        ),
        migrations.AddField(
            model_name='communitychatchallenge',
            name='client_id',
            field=models.CharField(default='legacy', max_length=64),
        ),
        migrations.AddField(
            model_name='communitychatchallenge',
            name='installation_id',
            field=models.UUIDField(default=uuid.uuid4),
        ),
        migrations.AddField(
            model_name='communitychatbootstraptoken',
            name='client_id',
            field=models.CharField(default='legacy', max_length=64),
        ),
        migrations.AddField(
            model_name='communitychatbootstraptoken',
            name='installation_id',
            field=models.UUIDField(default=uuid.uuid4),
        ),
        migrations.RunPython(backfill_installation_ids, migrations.RunPython.noop),
        migrations.AlterField(
            model_name='communitychatdevice',
            name='installation_id',
            field=models.UUIDField(default=uuid.uuid4, unique=True),
        ),
    ]
