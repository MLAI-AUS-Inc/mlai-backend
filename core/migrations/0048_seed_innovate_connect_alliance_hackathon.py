from datetime import date

from django.db import migrations


def seed_innovate_connect_alliance_hackathon(apps, schema_editor):
    Hackathon = apps.get_model("core", "Hackathon")

    Hackathon.objects.get_or_create(
        slug="innovate-connect-alliance",
        defaults={
            "name": "Innovate Connect Alliance",
            "description": "Collaborate on bold ideas, ship compelling demos, and submit team videos.",
            "start_date": date(2026, 1, 1),
            "end_date": date(2026, 12, 31),
        },
    )


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0047_organizationcontentconfig_daily_discovery_fields"),
    ]

    operations = [
        migrations.RunPython(
            seed_innovate_connect_alliance_hackathon,
            migrations.RunPython.noop,
        ),
    ]
