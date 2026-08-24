from django.db import migrations


ROOMS = (
    ('small-meeting-room', 'Small Meeting Room'),
    ('big-meeting-room', 'Big Meeting Room'),
)


def configure_two_meeting_rooms(apps, schema_editor):
    MeetingRoom = apps.get_model('roo', 'MeetingRoom')
    for slug, name in ROOMS:
        MeetingRoom.objects.update_or_create(
            slug=slug,
            defaults={'name': name, 'is_active': True},
        )
    MeetingRoom.objects.exclude(
        slug__in=[slug for slug, _ in ROOMS]
    ).update(is_active=False)


def restore_generic_meeting_room(apps, schema_editor):
    MeetingRoom = apps.get_model('roo', 'MeetingRoom')
    MeetingRoom.objects.update_or_create(
        slug='meeting-room',
        defaults={'name': 'Meeting Room', 'is_active': True},
    )
    MeetingRoom.objects.filter(
        slug__in=[slug for slug, _ in ROOMS]
    ).update(is_active=False)


class Migration(migrations.Migration):

    dependencies = [
        ('roo', '0031_meeting_room_booking'),
    ]

    operations = [
        migrations.RunPython(
            configure_two_meeting_rooms,
            restore_generic_meeting_room,
        ),
    ]
