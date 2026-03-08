from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0028_update_hospital_hackathon_metadata'),
    ]

    operations = [
        migrations.AddField(
            model_name='researchedkeyword',
            name='cluster_fingerprint',
            field=models.CharField(blank=True, db_index=True, default='', max_length=255),
        ),
        migrations.AddField(
            model_name='researchedkeyword',
            name='cooldown_until',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='researchedkeyword',
            name='last_rejected_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='researchedkeyword',
            name='last_selected_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='researchedkeyword',
            name='last_shown_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='researchedkeyword',
            name='times_rejected',
            field=models.IntegerField(default=0),
        ),
        migrations.AddField(
            model_name='researchedkeyword',
            name='times_selected',
            field=models.IntegerField(default=0),
        ),
        migrations.AddField(
            model_name='researchedkeyword',
            name='times_shown',
            field=models.IntegerField(default=0),
        ),
        migrations.AddIndex(
            model_name='researchedkeyword',
            index=models.Index(fields=['organization', 'cooldown_until'], name='seo_kw_org_cooldown_idx'),
        ),
    ]
