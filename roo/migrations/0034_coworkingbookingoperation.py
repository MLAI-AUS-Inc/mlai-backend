import uuid

from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('roo', '0033_meeting_room_purchased_cost_microroo'),
    ]

    operations = [
        migrations.CreateModel(
            name='CoworkingBookingOperation',
            fields=[
                ('id', models.UUIDField(editable=False, primary_key=True, serialize=False)),
                ('kind', models.CharField(choices=[('single', 'Single'), ('batch', 'Batch')], max_length=10)),
                ('request_fingerprint', models.CharField(max_length=64)),
                ('response_payload', models.JSONField()),
                ('http_status', models.PositiveSmallIntegerField(default=200)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('subjects', models.ManyToManyField(blank=True, related_name='coworking_booking_operations', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'verbose_name': 'Coworking booking operation',
                'verbose_name_plural': 'Coworking booking operations',
                'ordering': ['-created_at'],
            },
        ),
    ]
