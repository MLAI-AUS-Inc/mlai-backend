# Generated manually

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0019_contentfactoryjob_request_meta_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='organizationcontentconfig',
            name='installed_packages',
            field=models.JSONField(
                blank=True,
                default=dict,
                help_text='Full list of installed packages from package.json {name: version}'
            ),
        ),
        migrations.AddField(
            model_name='organizationcontentconfig',
            name='pillar_strategy',
            field=models.JSONField(
                blank=True,
                default=dict,
                help_text='SEO content pillars with slugs and topics derived from company context'
            ),
        ),
    ]
