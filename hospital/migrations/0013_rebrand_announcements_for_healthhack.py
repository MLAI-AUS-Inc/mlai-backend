from django.db import migrations


LEGACY_KAGGLE_URL = 'https://www.kaggle.com/competitions/medhack-frontiers'

REPLACEMENTS = (
    (LEGACY_KAGGLE_URL, f'[HealthHack Kaggle competition]({LEGACY_KAGGLE_URL})'),
    ('MEDHACK: FRONTIERS', 'HEALTHHACK'),
    ('MedHack: Frontiers', 'HealthHack'),
    ('Medhack: Frontiers', 'HealthHack'),
    ('MEDHACK', 'HEALTHHACK'),
    ('MedHack', 'HealthHack'),
    ('Medhack', 'HealthHack'),
    ('MYMI', 'UNSW No Code Society'),
)


def rebrand_text(value):
    updated = value
    for old, new in REPLACEMENTS:
        updated = updated.replace(old, new)
    return updated


def rebrand_announcements(apps, schema_editor):
    Announcement = apps.get_model('hospital', 'Announcement')

    for announcement in Announcement.objects.all().iterator():
        title = rebrand_text(announcement.title)
        body = rebrand_text(announcement.body)
        if title == announcement.title and body == announcement.body:
            continue
        announcement.title = title
        announcement.body = body
        announcement.save(update_fields=['title', 'body', 'updated_at'])


class Migration(migrations.Migration):
    dependencies = [
        ('hospital', '0012_sim_turn_idempotency'),
    ]

    operations = [
        migrations.RunPython(rebrand_announcements, migrations.RunPython.noop),
    ]
