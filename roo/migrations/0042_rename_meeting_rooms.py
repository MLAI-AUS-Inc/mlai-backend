from django.db import migrations


ROOM_NAMES = (
    ('small-meeting-room', 'Small Meeting Room', 'Lovelace'),
    ('big-meeting-room', 'Big Meeting Room', 'Turing'),
)


def rename_meeting_rooms(apps, schema_editor):
    MeetingRoom = apps.get_model('roo', 'MeetingRoom')
    for slug, _old_name, new_name in ROOM_NAMES:
        MeetingRoom.objects.using(schema_editor.connection.alias).filter(
            slug=slug,
        ).update(name=new_name)


def restore_meeting_room_names(apps, schema_editor):
    MeetingRoom = apps.get_model('roo', 'MeetingRoom')
    for slug, old_name, _new_name in ROOM_NAMES:
        MeetingRoom.objects.using(schema_editor.connection.alias).filter(
            slug=slug,
        ).update(name=old_name)


class Migration(migrations.Migration):
    dependencies = [
        ('roo', '0041_apple_iap_digital_credits'),
    ]

    operations = [
        migrations.RunPython(rename_meeting_rooms, restore_meeting_room_names),
    ]
