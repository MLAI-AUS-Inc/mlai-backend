from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0030_add_content_factory_runs'),
    ]

    operations = [
        migrations.AddField(
            model_name='organizationcontentconfig',
            name='article_system',
            field=models.JSONField(blank=True, default=dict),
        ),
    ]

