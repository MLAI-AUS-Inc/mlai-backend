from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('roo', '0021_alter_taskactivity_event_type'),
    ]

    operations = [
        migrations.AlterField(
            model_name='pointsadmin',
            name='role',
            field=models.CharField(
                choices=[
                    ('committee', 'Committee'),
                    ('portfolio_lead', 'Portfolio Lead'),
                    ('admin', 'Admin'),
                    ('partner', 'Partner'),
                ],
                default='committee',
                max_length=50,
            ),
        ),
    ]
