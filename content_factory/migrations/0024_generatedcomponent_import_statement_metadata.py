from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('content_factory', '0023_design_snapshot'),
    ]

    operations = [
        migrations.AddField(
            model_name='generatedcomponent',
            name='import_statement',
            field=models.CharField(blank=True, default='', max_length=500),
        ),
        migrations.AddField(
            model_name='generatedcomponent',
            name='metadata',
            field=models.JSONField(blank=True, default=dict),
        ),
    ]
