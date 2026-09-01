import uuid

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import integrations.fields


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("integrations", "0042_linear_meeting_action_batches"),
    ]

    operations = [
        migrations.CreateModel(
            name="SlackDmMirrorGrant",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("slack_workspace_id", models.CharField(db_index=True, max_length=100)),
                ("slack_user_id", models.CharField(db_index=True, max_length=100)),
                ("status", models.CharField(choices=[("active", "Active"), ("paused", "Paused"), ("error", "Error"), ("revoked", "Revoked")], db_index=True, default="active", max_length=24)),
                ("consent_version", models.CharField(default="slack-dm-mirror-v2-owner", max_length=64)),
                ("history_days", models.PositiveSmallIntegerField(default=30)),
                ("consented_at", models.DateTimeField()),
                ("paused_at", models.DateTimeField(blank=True, null=True)),
                ("revoked_at", models.DateTimeField(blank=True, null=True)),
                ("last_discovery_at", models.DateTimeField(blank=True, null=True)),
                ("last_synced_at", models.DateTimeField(blank=True, null=True)),
                ("last_error", models.TextField(blank=True, default="")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("connection", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="slack_dm_mirror_grant", to="integrations.externalserviceconnection")),
                ("user", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="slack_dm_mirror_grants", to=settings.AUTH_USER_MODEL)),
            ],
            options={"db_table": "slack_dm_mirror_grant"},
        ),
        migrations.CreateModel(
            name="SlackDmMirrorConversation",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("slack_workspace_id", models.CharField(db_index=True, max_length=100)),
                ("slack_conversation_id", models.CharField(max_length=100)),
                ("participant_slack_ids", models.JSONField(default=list)),
                ("participant_buzz_pubkeys", models.JSONField(default=list)),
                ("participant_identity_map", models.JSONField(default=dict)),
                ("participant_profiles", models.JSONField(default=dict)),
                ("participant_hash", models.CharField(blank=True, db_index=True, default="", max_length=64)),
                ("mlai_channel_id", models.UUIDField(blank=True, null=True, unique=True)),
                ("status", models.CharField(choices=[("awaiting_setup", "Awaiting owner setup"), ("provisioning", "Provisioning"), ("live", "Live"), ("paused", "Paused"), ("error", "Error")], db_index=True, default="awaiting_setup", max_length=24)),
                ("oldest_synced_ts", models.CharField(blank=True, default="", max_length=32)),
                ("latest_synced_ts", models.CharField(blank=True, default="", max_length=32)),
                ("last_synced_at", models.DateTimeField(blank=True, null=True)),
                ("last_error", models.TextField(blank=True, default="")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("grant", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="conversations", to="integrations.slackdmmirrorgrant")),
            ],
            options={"db_table": "slack_dm_mirror_conversation"},
        ),
        migrations.CreateModel(
            name="SlackDmMirrorDelivery",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("source_platform", models.CharField(choices=[("slack", "Slack"), ("discord", "Discord"), ("buzz", "MLAI Chat")], max_length=20)),
                ("source_message_id", models.CharField(max_length=100)),
                ("source_author_id", models.CharField(max_length=100)),
                ("operation", models.CharField(choices=[("create", "Create"), ("edit", "Edit"), ("delete", "Delete"), ("reaction_add", "Reaction add"), ("reaction_remove", "Reaction remove")], max_length=20)),
                ("encrypted_text", integrations.fields.EncryptedTextField(blank=True, default="")),
                ("metadata", models.JSONField(blank=True, default=dict)),
                ("status", models.CharField(choices=[("pending", "Pending"), ("processing", "Processing"), ("completed", "Completed"), ("failed", "Failed"), ("dead", "Dead")], db_index=True, default="pending", max_length=20)),
                ("attempts", models.PositiveSmallIntegerField(default=0)),
                ("available_at", models.DateTimeField(db_index=True)),
                ("completed_at", models.DateTimeField(blank=True, null=True)),
                ("last_error", models.TextField(blank=True, default="")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("conversation", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="deliveries", to="integrations.slackdmmirrorconversation")),
            ],
            options={"db_table": "slack_dm_mirror_delivery"},
        ),
        migrations.AddConstraint(
            model_name="slackdmmirrorgrant",
            constraint=models.UniqueConstraint(fields=("slack_workspace_id", "slack_user_id"), name="slack_dm_grant_workspace_user_unique"),
        ),
        migrations.AddIndex(
            model_name="slackdmmirrorgrant",
            index=models.Index(fields=["slack_workspace_id", "status"], name="slack_dm_grant_ws_status_idx"),
        ),
        migrations.AddIndex(
            model_name="slackdmmirrorconversation",
            index=models.Index(fields=["slack_workspace_id", "status"], name="slack_dm_conv_ws_status_idx"),
        ),
        migrations.AddConstraint(
            model_name="slackdmmirrorconversation",
            constraint=models.UniqueConstraint(fields=("grant", "slack_conversation_id"), name="slack_dm_conv_grant_chan_uniq"),
        ),
        migrations.AddConstraint(
            model_name="slackdmmirrordelivery",
            constraint=models.UniqueConstraint(fields=("conversation", "source_platform", "source_message_id", "operation"), name="slack_dm_delivery_source_operation_unique"),
        ),
        migrations.AddIndex(
            model_name="slackdmmirrordelivery",
            index=models.Index(fields=["status", "available_at"], name="slack_dm_delivery_ready_idx"),
        ),
    ]
