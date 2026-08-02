import uuid

from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


class Migration(migrations.Migration):
    dependencies = [
        ('core', '0056_user_community_chat_profile_id'),
    ]

    operations = [
        migrations.CreateModel(
            name='PasswordResetEmailDelivery',
            fields=[
                (
                    'id',
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ('encrypted_reset_link', models.TextField()),
                (
                    'status',
                    models.CharField(
                        choices=[
                            ('pending', 'Pending'),
                            ('sending', 'Sending'),
                            ('sent', 'Sent'),
                            ('failed', 'Failed'),
                            ('cancelled', 'Cancelled'),
                        ],
                        default='pending',
                        max_length=16,
                    ),
                ),
                ('attempts', models.PositiveSmallIntegerField(default=0)),
                ('available_at', models.DateTimeField(default=django.utils.timezone.now)),
                ('claimed_at', models.DateTimeField(blank=True, null=True)),
                ('sent_at', models.DateTimeField(blank=True, null=True)),
                ('last_error_code', models.CharField(blank=True, max_length=120)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                (
                    'challenge',
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='email_delivery',
                        to='core.passwordresetchallenge',
                    ),
                ),
            ],
            options={'ordering': ('created_at',)},
        ),
        migrations.AddIndex(
            model_name='passwordresetemaildelivery',
            index=models.Index(
                fields=['status', 'available_at', 'created_at'],
                name='password_delivery_pending_idx',
            ),
        ),
    ]
