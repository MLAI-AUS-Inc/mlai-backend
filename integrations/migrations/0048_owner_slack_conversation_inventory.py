from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("integrations", "0047_message_sync_reliability"),
    ]

    operations = [
        migrations.CreateModel(
            name="SlackOwnerConversationInventory",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("slack_conversation_id", models.CharField(max_length=100)),
                ("kind", models.CharField(max_length=24)),
                ("source_name", models.CharField(blank=True, default="", max_length=255)),
                ("counterpart_slack_user_id", models.CharField(blank=True, default="", max_length=100)),
                ("display_name", models.CharField(blank=True, default="", max_length=255)),
                ("source_activity_ts", models.CharField(blank=True, default="", max_length=32)),
                ("source_archived", models.BooleanField(null=True)),
                ("source_is_open", models.BooleanField(null=True)),
                ("eligibility", models.CharField(default="eligible", max_length=24)),
                ("last_seen_sweep_id", models.UUIDField(null=True)),
                ("first_seen_at", models.DateTimeField(auto_now_add=True)),
                ("last_seen_at", models.DateTimeField(auto_now=True)),
                ("grant", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="owner_conversation_inventory", to="integrations.slackdmmirrorgrant")),
            ],
            options={
                "db_table": "slack_owner_conversation_inventory",
            },
        ),
        migrations.AddConstraint(
            model_name="slackownerconversationinventory",
            constraint=models.UniqueConstraint(fields=("grant", "slack_conversation_id"), name="slack_owner_inv_grant_source_uniq"),
        ),
        migrations.AddIndex(
            model_name="slackownerconversationinventory",
            index=models.Index(fields=("grant", "kind", "id"), name="slack_owner_inv_page_idx"),
        ),
        migrations.AddIndex(
            model_name="slackownerconversationinventory",
            index=models.Index(fields=("grant", "last_seen_sweep_id"), name="slack_owner_inv_sweep_idx"),
        ),
    ]
