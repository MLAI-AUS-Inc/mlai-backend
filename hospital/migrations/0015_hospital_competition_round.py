import django.db.models.deletion
import django.db.models.functions.text
from django.conf import settings
from django.db import migrations, models
import django.utils.timezone


LEGACY_ROUND_SLUG = 'medhack-frontiers-legacy'


def assign_existing_records_to_legacy_round(apps, schema_editor):
    HospitalCompetitionRound = apps.get_model(
        'hospital',
        'HospitalCompetitionRound',
    )
    Team = apps.get_model('hospital', 'Team')
    Submission = apps.get_model('hospital', 'Submission')

    legacy_round, _ = HospitalCompetitionRound.objects.get_or_create(
        slug=LEGACY_ROUND_SLUG,
        defaults={
            'name': 'MedHack: Frontiers (Legacy)',
            'status': 'active',
            'notes': 'Automatically created for records predating HealthHack rounds.',
        },
    )
    Team.objects.filter(round__isnull=True).update(round=legacy_round)
    Submission.objects.filter(round__isnull=True).update(round=legacy_round)


def remove_legacy_round_assignment(apps, schema_editor):
    HospitalCompetitionRound = apps.get_model(
        'hospital',
        'HospitalCompetitionRound',
    )
    Team = apps.get_model('hospital', 'Team')
    Submission = apps.get_model('hospital', 'Submission')

    Team.objects.update(round=None)
    Submission.objects.update(round=None)
    HospitalCompetitionRound.objects.filter(slug=LEGACY_ROUND_SLUG).delete()


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('hospital', '0014_announcement_slack_provenance'),
    ]

    operations = [
        migrations.CreateModel(
            name='HospitalCompetitionRound',
            fields=[
                (
                    'id',
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name='ID',
                    ),
                ),
                ('slug', models.SlugField(unique=True)),
                ('name', models.CharField(max_length=120)),
                (
                    'status',
                    models.CharField(
                        choices=[('active', 'Active'), ('archived', 'Archived')],
                        db_index=True,
                        default='active',
                        max_length=16,
                    ),
                ),
                ('opened_at', models.DateTimeField(default=django.utils.timezone.now)),
                ('archived_at', models.DateTimeField(blank=True, null=True)),
                ('notes', models.TextField(blank=True, default='')),
                (
                    'archived_by',
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name='archived_hospital_competition_rounds',
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                'ordering': ['-opened_at'],
            },
        ),
        migrations.AddField(
            model_name='team',
            name='round',
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name='teams',
                to='hospital.hospitalcompetitionround',
            ),
        ),
        migrations.AddField(
            model_name='submission',
            name='round',
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name='submissions',
                to='hospital.hospitalcompetitionround',
            ),
        ),
        migrations.AlterField(
            model_name='team',
            name='team_id',
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
        migrations.RunPython(
            assign_existing_records_to_legacy_round,
            remove_legacy_round_assignment,
        ),
        migrations.AlterField(
            model_name='team',
            name='round',
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name='teams',
                to='hospital.hospitalcompetitionround',
            ),
        ),
        migrations.AlterField(
            model_name='submission',
            name='round',
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name='submissions',
                to='hospital.hospitalcompetitionround',
            ),
        ),
        migrations.AddConstraint(
            model_name='hospitalcompetitionround',
            constraint=models.UniqueConstraint(
                condition=models.Q(('status', 'active')),
                fields=('status',),
                name='unique_active_hospital_competition_round',
            ),
        ),
        migrations.AddConstraint(
            model_name='team',
            constraint=models.UniqueConstraint(
                fields=('round', 'team_id'),
                name='unique_hospital_team_id_per_round',
            ),
        ),
        migrations.AddConstraint(
            model_name='team',
            constraint=models.UniqueConstraint(
                django.db.models.functions.text.Lower('team_name'),
                models.F('round'),
                name='unique_hospital_team_name_per_round_ci',
            ),
        ),
    ]
