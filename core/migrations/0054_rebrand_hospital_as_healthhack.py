from django.db import migrations


def rebrand_hospital(apps, schema_editor):
    Hackathon = apps.get_model('core', 'Hackathon')
    Hackathon.objects.filter(slug='hospital').update(
        name='HealthHack',
        description='Build practical solutions to real healthcare challenges.',
    )


def restore_legacy_name(apps, schema_editor):
    Hackathon = apps.get_model('core', 'Hackathon')
    Hackathon.objects.filter(slug='hospital', name='HealthHack').update(
        name='Medhack: Frontiers',
        description='Revolutionizing healthcare with AI.',
    )


class Migration(migrations.Migration):
    dependencies = [
        ('core', '0053_user_updated_at'),
    ]

    operations = [
        migrations.RunPython(rebrand_hospital, restore_legacy_name),
    ]
