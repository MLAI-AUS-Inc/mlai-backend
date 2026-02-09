"""
Remove MedHack models from roo app state (moved to hospital app).
Tables are NOT dropped — hospital app takes ownership via db_table meta.
"""
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('roo', '0012_medhack_game_models'),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.DeleteModel(name='MedHackGuess'),
                migrations.DeleteModel(name='MedHackWinner'),
                migrations.DeleteModel(name='MedHackCase'),
            ],
            database_operations=[],  # Don't touch the database
        ),
    ]
