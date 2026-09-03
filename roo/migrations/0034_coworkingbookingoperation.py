import uuid

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
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
            ],
            options={
                'verbose_name': 'Coworking booking operation',
                'verbose_name_plural': 'Coworking booking operations',
                'ordering': ['-created_at'],
            },
        ),
    ]
