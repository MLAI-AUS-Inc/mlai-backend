from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('roo', '0014_add_content_factory_ledger_source'),
    ]

    operations = [
        migrations.CreateModel(
            name='PointsRequest',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('requester_slack_id', models.CharField(max_length=50)),
                ('target_slack_id', models.CharField(max_length=50)),
                ('points', models.IntegerField()),
                ('reason', models.TextField()),
                ('status', models.CharField(choices=[('pending', 'Pending'), ('approved', 'Approved'), ('rejected', 'Rejected'), ('cancelled', 'Cancelled')], default='pending', max_length=20)),
                ('approved_by_slack_id', models.CharField(blank=True, max_length=50, null=True)),
                ('approved_at', models.DateTimeField(blank=True, null=True)),
                ('slack_channel_id', models.CharField(blank=True, max_length=50, null=True)),
                ('slack_thread_ts', models.CharField(blank=True, max_length=50, null=True)),
                ('slack_summary_message_ts', models.CharField(blank=True, max_length=50, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('ledger_entry', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='points_requests', to='roo.ledger')),
            ],
            options={
                'verbose_name': 'Points Request',
                'verbose_name_plural': 'Points Requests',
                'ordering': ['-created_at'],
            },
        ),
        migrations.AddIndex(
            model_name='pointsrequest',
            index=models.Index(fields=['status', 'created_at'], name='roo_pointsr_status_8f1eab_idx'),
        ),
        migrations.AddIndex(
            model_name='pointsrequest',
            index=models.Index(fields=['slack_channel_id', 'slack_summary_message_ts'], name='roo_pointsr_slack_c7c99b_idx'),
        ),
    ]
