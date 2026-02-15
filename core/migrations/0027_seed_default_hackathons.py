from datetime import date

from django.db import migrations


def seed_default_hackathons(apps, schema_editor):
    Hackathon = apps.get_model('core', 'Hackathon')

    Hackathon.objects.get_or_create(
        slug='esafety',
        defaults={
            'name': 'eSafety Hackathon',
            'description': 'Develop AI solutions for online safety.',
            'start_date': date(2026, 1, 1),
            'end_date': date(2026, 12, 31),
        },
    )

    Hackathon.objects.get_or_create(
        slug='hospital',
        defaults={
            'name': 'MedHack - AI Hospital Hackathon',
            'description': 'Revolutionizing healthcare with AI.',
            'start_date': date(2026, 1, 1),
            'end_date': date(2026, 12, 31),
        },
    )


class Migration(migrations.Migration):
    dependencies = [
        ('core', '0026_add_scaffold_preview_url'),
    ]

    operations = [
        migrations.RunPython(seed_default_hackathons, migrations.RunPython.noop),
    ]
