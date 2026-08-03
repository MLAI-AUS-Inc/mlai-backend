import uuid

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("community_chat", "0005_email_code_challenges"),
    ]

    operations = [
        migrations.CreateModel(
            name="CommunityChatAccountSession",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("public_key", models.CharField(max_length=64)),
                ("installation_id", models.UUIDField()),
                ("client_id", models.CharField(max_length=64)),
                ("origin", models.CharField(max_length=255)),
                ("platform", models.CharField(max_length=32)),
                ("name", models.CharField(blank=True, max_length=120)),
                ("access_token_hash", models.CharField(max_length=64, unique=True)),
                ("refresh_token_hash", models.CharField(max_length=64, unique=True)),
                ("auth_version", models.PositiveIntegerField()),
                ("access_expires_at", models.DateTimeField()),
                ("expires_at", models.DateTimeField()),
                ("revoked_at", models.DateTimeField(blank=True, null=True)),
                ("last_used_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="community_chat_account_sessions",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "ordering": ("-created_at",),
                "indexes": [
                    models.Index(fields=["user", "revoked_at", "expires_at"], name="chat_session_user_active_idx"),
                    models.Index(fields=["client_id", "installation_id"], name="chat_session_install_idx"),
                ],
            },
        ),
    ]
