from datetime import date

from django.db import migrations


def update_hospital_hackathon_metadata(apps, schema_editor):
    Hackathon = apps.get_model('core', 'Hackathon')

    Hackathon.objects.update_or_create(
        slug='hospital',
        defaults={
            'name': 'Medhack: Frontiers',
            'description': 'Revolutionizing healthcare with AI.',
            'start_date': date(2026, 1, 1),
            'end_date': date(2026, 12, 31),
        },
    )


class Migration(migrations.Migration):
    dependencies = [
        ('core', '0027_seed_default_hackathons'),
    ]

    operations = [
        migrations.RunPython(update_hospital_hackathon_metadata, migrations.RunPython.noop),
    ]
