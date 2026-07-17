import django.db.models.deletion
from django.db import migrations, models
from django.utils import timezone


LEGACY_ROUND_SLUG = 'medhack-frontiers-legacy'
CURRENT_ROUND_SLUG = 'healthhack-2026'


def archive_legacy_data_and_open_healthhack(apps, schema_editor):
    HospitalCompetitionRound = apps.get_model(
        'hospital',
        'HospitalCompetitionRound',
    )
    Announcement = apps.get_model('hospital', 'Announcement')

    legacy_round = HospitalCompetitionRound.objects.get(
        slug=LEGACY_ROUND_SLUG,
    )
    Announcement.objects.filter(round__isnull=True).update(round=legacy_round)

    if legacy_round.status == 'active':
        legacy_round.status = 'archived'
        legacy_round.archived_at = timezone.now()
        legacy_round.notes = (
            'Automatically archived before the HealthHack 2026 competition round.'
        )
        legacy_round.save(
            update_fields=['status', 'archived_at', 'notes'],
        )

    active_round = HospitalCompetitionRound.objects.filter(status='active').first()
    if active_round is None:
        current_round, _ = HospitalCompetitionRound.objects.get_or_create(
            slug=CURRENT_ROUND_SLUG,
            defaults={
                'name': 'HealthHack 2026',
                'status': 'active',
                'notes': 'Opened automatically after archiving legacy event data.',
            },
        )
        if current_round.status != 'active':
            current_round.status = 'active'
            current_round.archived_at = None
            current_round.archived_by = None
            current_round.save(
                update_fields=['status', 'archived_at', 'archived_by'],
            )


def restore_legacy_round(apps, schema_editor):
    HospitalCompetitionRound = apps.get_model(
        'hospital',
        'HospitalCompetitionRound',
    )

    HospitalCompetitionRound.objects.filter(
        status='active',
    ).exclude(slug=LEGACY_ROUND_SLUG).update(
        status='archived',
        archived_at=timezone.now(),
    )
    HospitalCompetitionRound.objects.filter(
        slug=LEGACY_ROUND_SLUG,
    ).update(
        status='active',
        archived_at=None,
        archived_by=None,
    )


class Migration(migrations.Migration):
    # PostgreSQL must commit the round/announcement data updates before Django
    # alters their foreign-key constraint, otherwise pending trigger events
    # prevent the schema change.
    atomic = False

    dependencies = [
        ('hospital', '0015_hospital_competition_round'),
    ]

    operations = [
        migrations.AddField(
            model_name='announcement',
            name='round',
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name='announcements',
                to='hospital.hospitalcompetitionround',
            ),
        ),
        migrations.RunPython(
            archive_legacy_data_and_open_healthhack,
            restore_legacy_round,
        ),
        migrations.AlterField(
            model_name='announcement',
            name='round',
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name='announcements',
                to='hospital.hospitalcompetitionround',
            ),
        ),
        migrations.RemoveConstraint(
            model_name='announcement',
            name='uniq_hospital_announcement_slack_source',
        ),
        migrations.AddConstraint(
            model_name='announcement',
            constraint=models.UniqueConstraint(
                condition=(
                    models.Q(source_channel_id__isnull=False)
                    & models.Q(source_message_ts__isnull=False)
                ),
                fields=('round', 'source_channel_id', 'source_message_ts'),
                name='uniq_hospital_announcement_slack_source_per_round',
            ),
        ),
    ]
