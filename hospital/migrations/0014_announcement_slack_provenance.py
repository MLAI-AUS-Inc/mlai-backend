import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('hospital', '0013_rebrand_announcements_for_healthhack'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name='announcement',
            name='requester',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='requested_hospital_announcements',
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name='announcement',
            name='source_channel_id',
            field=models.CharField(blank=True, max_length=32, null=True),
        ),
        migrations.AddField(
            model_name='announcement',
            name='source_message_ts',
            field=models.CharField(blank=True, max_length=32, null=True),
        ),
        migrations.AddConstraint(
            model_name='announcement',
            constraint=models.UniqueConstraint(
                condition=(
                    models.Q(source_channel_id__isnull=False)
                    & models.Q(source_message_ts__isnull=False)
                ),
                fields=('source_channel_id', 'source_message_ts'),
                name='uniq_hospital_announcement_slack_source',
            ),
        ),
    ]
