"""
Take ownership of MedHack models (moved from roo app).
Tables already exist as roo_medhack* — we use db_table meta to point at them.
Also adds solved, hint_level fields and changes case_id to IntegerField.
"""
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('hospital', '0004_announcement'),
        ('roo', '0013_move_medhack_to_hospital'),  # Must run after roo releases the models
    ]

    operations = [
        # Step 1: Tell Django these models now live in hospital (without creating tables)
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.CreateModel(
                    name='MedHackCase',
                    fields=[
                        ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                        ('case_id', models.CharField(help_text="YAML case ID reference (e.g. 'patient_1')", max_length=100)),
                        ('is_active', models.BooleanField(default=True)),
                        ('started_by_slack_id', models.CharField(help_text='Admin Slack ID who started this case', max_length=50)),
                        ('started_at', models.DateTimeField(auto_now_add=True)),
                        ('closed_at', models.DateTimeField(blank=True, null=True)),
                    ],
                    options={
                        'verbose_name': 'MedHack Case',
                        'verbose_name_plural': 'MedHack Cases',
                        'ordering': ['-started_at'],
                        'db_table': 'roo_medhackcase',
                    },
                ),
                migrations.CreateModel(
                    name='MedHackWinner',
                    fields=[
                        ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                        ('slack_user_id', models.CharField(max_length=50)),
                        ('is_first_solver', models.BooleanField(default=False)),
                        ('won_at', models.DateTimeField(auto_now_add=True)),
                        ('case', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='winners', to='hospital.medhackcase')),
                    ],
                    options={
                        'verbose_name': 'MedHack Winner',
                        'verbose_name_plural': 'MedHack Winners',
                        'ordering': ['-won_at'],
                        'unique_together': {('case', 'slack_user_id')},
                        'db_table': 'roo_medhackwinner',
                    },
                ),
                migrations.CreateModel(
                    name='MedHackGuess',
                    fields=[
                        ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                        ('slack_user_id', models.CharField(max_length=50)),
                        ('guess', models.TextField()),
                        ('correct', models.BooleanField(blank=True, help_text='Null while pending, True/False after confirmation', null=True)),
                        ('is_pending', models.BooleanField(default=True, help_text='True if awaiting user confirmation')),
                        ('created_at', models.DateTimeField(auto_now_add=True)),
                        ('confirmed_at', models.DateTimeField(blank=True, null=True)),
                        ('case', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='guesses', to='hospital.medhackcase')),
                    ],
                    options={
                        'verbose_name': 'MedHack Guess',
                        'verbose_name_plural': 'MedHack Guesses',
                        'ordering': ['-created_at'],
                        'db_table': 'roo_medhackguess',
                        'indexes': [models.Index(fields=['case', 'slack_user_id'], name='roo_medhack_case_id_bbe3ce_idx')],
                    },
                ),
            ],
            database_operations=[],  # Tables already exist
        ),

        # Step 2: Add new fields and alter case_id type (these DO touch the database)
        migrations.AddField(
            model_name='medhackcase',
            name='solved',
            field=models.BooleanField(default=False, help_text='Whether the case has been correctly diagnosed'),
        ),
        migrations.AddField(
            model_name='medhackcase',
            name='hint_level',
            field=models.IntegerField(default=0, help_text='Current hint level for progressive hints'),
        ),
        migrations.AlterField(
            model_name='medhackcase',
            name='case_id',
            field=models.IntegerField(help_text="Case ID from cases.yaml (not unique — same case can be replayed)"),
        ),
    ]
