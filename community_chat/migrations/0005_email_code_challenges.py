import uuid

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("community_chat", "0004_bootstrap_token_origin"),
    ]

    operations = [
        migrations.CreateModel(
            name="CommunityChatEmailCodeChallenge",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("email_digest", models.CharField(max_length=64)),
                ("code_digest", models.CharField(max_length=64)),
                ("client_id", models.CharField(max_length=64)),
                ("installation_id", models.UUIDField()),
                ("origin", models.CharField(max_length=255)),
                ("platform", models.CharField(max_length=32)),
                ("device_name", models.CharField(blank=True, max_length=120)),
                ("public_key", models.CharField(max_length=64)),
                ("expires_at", models.DateTimeField()),
                ("attempt_count", models.PositiveSmallIntegerField(default=0)),
                ("max_attempts", models.PositiveSmallIntegerField(default=5)),
                ("consumed_at", models.DateTimeField(blank=True, null=True)),
                ("invalidated_at", models.DateTimeField(blank=True, null=True)),
                ("requested_ip_digest", models.CharField(blank=True, max_length=64)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "user",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="community_chat_email_code_challenges",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "ordering": ("-created_at",),
                "indexes": [
                    models.Index(fields=["email_digest", "client_id", "installation_id", "created_at"], name="chat_email_code_lookup_idx"),
                    models.Index(fields=["expires_at"], name="chat_email_code_expiry_idx"),
                    models.Index(fields=["user", "created_at"], name="chat_email_code_user_idx"),
                ],
            },
        ),
        migrations.CreateModel(
            name="CommunityChatEmailCodeDelivery",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("encrypted_code", models.TextField()),
                ("status", models.CharField(choices=[("pending", "Pending"), ("sending", "Sending"), ("sent", "Sent"), ("failed", "Failed"), ("cancelled", "Cancelled")], default="pending", max_length=16)),
                ("attempts", models.PositiveSmallIntegerField(default=0)),
                ("available_at", models.DateTimeField(auto_now_add=True)),
                ("claimed_at", models.DateTimeField(blank=True, null=True)),
                ("sent_at", models.DateTimeField(blank=True, null=True)),
                ("provider_delivery_id", models.CharField(blank=True, max_length=255)),
                ("last_error_code", models.CharField(blank=True, max_length=120)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "challenge",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="email_delivery",
                        to="community_chat.communitychatemailcodechallenge",
                    ),
                ),
            ],
            options={
                "ordering": ("created_at",),
                "indexes": [
                    models.Index(fields=["status", "available_at", "created_at"], name="chat_email_delivery_idx"),
                ],
            },
        ),
    ]
