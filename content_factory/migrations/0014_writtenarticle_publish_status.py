from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('content_factory', '0013_writtenarticle_wa_org_created_idx'),
    ]

    operations = [
        migrations.AddField(
            model_name='writtenarticle',
            name='publish_status',
            field=models.CharField(
                choices=[
                    ('written', 'Written'),
                    ('pr_open', 'PR Open'),
                    ('pr_closed', 'PR Closed'),
                    ('merged', 'Merged'),
                    ('live', 'Live'),
                ],
                db_index=True,
                default='written',
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name='writtenarticle',
            name='pr_number',
            field=models.IntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='writtenarticle',
            name='pr_merged_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='writtenarticle',
            name='live_url',
            field=models.URLField(
                blank=True,
                help_text="Production URL confirmed against the customer site's sitemap",
                null=True,
            ),
        ),
        migrations.AddField(
            model_name='writtenarticle',
            name='live_checked_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='writtenarticle',
            name='live_verified_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
