import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("integrations", "0045_communitybridgedelivery_parent_dependency"),
    ]

    operations = [
        migrations.CreateModel(
            name="CommunityBridgeDeletionRequest",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("idempotency_key", models.UUIDField(default=uuid.uuid4)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("processing", "Processing"),
                            ("succeeded", "Succeeded"),
                            ("already_deleted", "Already deleted"),
                            ("failed", "Failed"),
                        ],
                        db_index=True,
                        default="processing",
                        max_length=24,
                    ),
                ),
                ("slack_workspace_id", models.CharField(max_length=100)),
                ("slack_channel_id", models.CharField(max_length=100)),
                ("slack_message_id", models.CharField(max_length=100)),
                ("buzz_event_id", models.CharField(max_length=100)),
                (
                    "error_code",
                    models.CharField(blank=True, default="", max_length=100),
                ),
                ("provider_response", models.JSONField(blank=True, default=dict)),
                ("completed_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "message_link",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="deletion_requests",
                        to="integrations.communitybridgemessagelink",
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="community_bridge_deletion_requests",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "db_table": "community_bridge_deletion_request",
                "ordering": ["-created_at"],
                "indexes": [
                    models.Index(
                        fields=["slack_workspace_id", "slack_message_id"],
                        name="bridge_delete_slack_msg_idx",
                    ),
                    models.Index(
                        fields=["buzz_event_id", "status"],
                        name="bridge_delete_buzz_status_idx",
                    ),
                ],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("user", "idempotency_key"),
                        name="bridge_delete_user_idem_unique",
                    ),
                ],
            },
        ),
    ]
