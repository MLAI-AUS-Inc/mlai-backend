from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('content_factory', '0020_organizationcontentconfig_article_system_setup_cache_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='organizationcontentconfig',
            name='auto_publish',
            field=models.BooleanField(default=False, help_text='When true, generated article PRs auto-merge once automated build/preview verification passes (no human review).'),
        ),
        migrations.AddField(
            model_name='organizationcontentconfig',
            name='requires_review',
            field=models.BooleanField(default=False, help_text='Force human review: open the publish PR but never auto-merge, even when auto_publish is true. Overrides auto_publish.'),
        ),
    ]
