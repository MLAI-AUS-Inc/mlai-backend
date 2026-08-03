import uuid

from django.db import migrations, models


def backfill_profile_ids(apps, schema_editor):
    User = apps.get_model('core', 'User')
    for user in User.objects.filter(community_chat_profile_id__isnull=True).iterator():
        user.community_chat_profile_id = uuid.uuid4()
        user.save(update_fields=('community_chat_profile_id',))


class Migration(migrations.Migration):
    dependencies = [
        ('core', '0055_passwordresetchallenge_user_auth_version_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='user',
            name='community_chat_profile_id',
            field=models.UUIDField(blank=True, null=True),
        ),
        migrations.RunPython(backfill_profile_ids, migrations.RunPython.noop),
        migrations.AlterField(
            model_name='user',
            name='community_chat_profile_id',
            field=models.UUIDField(default=uuid.uuid4, editable=False, unique=True),
        ),
    ]
