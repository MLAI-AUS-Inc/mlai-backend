from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0049_viberaisingpendingsignup'),
    ]

    operations = [
        migrations.DeleteModel(
            name='VibeRaisingPendingSignup',
        ),
    ]
