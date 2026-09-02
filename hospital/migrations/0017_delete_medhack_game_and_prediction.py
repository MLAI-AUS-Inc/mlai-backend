from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('hospital', '0016_announcement_round_and_activate_healthhack'),
    ]

    operations = [
        migrations.DeleteModel(
            name='MedHackGuess',
        ),
        migrations.DeleteModel(
            name='MedHackWinner',
        ),
        migrations.DeleteModel(
            name='MedHackCase',
        ),
        migrations.DeleteModel(
            name='Prediction',
        ),
    ]
