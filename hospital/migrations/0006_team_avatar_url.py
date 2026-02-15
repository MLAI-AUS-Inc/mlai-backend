from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('hospital', '0005_medhack_from_roo'),
    ]

    operations = [
        migrations.AddField(
            model_name='team',
            name='avatar_url',
            field=models.URLField(blank=True, null=True),
        ),
    ]
