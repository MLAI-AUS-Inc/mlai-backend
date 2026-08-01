from django.db import migrations, models


def copy_discord_destinations(apps, schema_editor):
    channel_model = apps.get_model("integrations", "CommunityBridgeChannel")
    channel_model.objects.all().update(destination_platform="discord")
    for channel in channel_model.objects.all().iterator():
        channel.destination_workspace_id = channel.discord_guild_id
        channel.destination_channel_id = channel.discord_channel_id
        channel.destination_channel_name = channel.discord_channel_name
        channel.save(
            update_fields=[
                "destination_workspace_id",
                "destination_channel_id",
                "destination_channel_name",
            ]
        )


class Migration(migrations.Migration):
    dependencies = [("integrations", "0034_reconciliation_learning_decisions")]

    operations = [
        migrations.AddField(
            model_name="communitybridgechannel",
            name="destination_platform",
            field=models.CharField(
                choices=[("slack", "Slack"), ("discord", "Discord"), ("buzz", "MLAI Chat")],
                db_index=True,
                default="discord",
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="communitybridgechannel",
            name="destination_workspace_id",
            field=models.CharField(blank=True, default="", max_length=100),
        ),
        migrations.AddField(
            model_name="communitybridgechannel",
            name="destination_channel_id",
            field=models.CharField(blank=True, db_index=True, default="", max_length=100),
        ),
        migrations.AddField(
            model_name="communitybridgechannel",
            name="destination_channel_name",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
        migrations.AlterField(
            model_name="communitybridgechannel",
            name="discord_channel_id",
            field=models.CharField(blank=True, db_index=True, default="", max_length=100),
        ),
        migrations.AlterField(
            model_name="communitybridgedelivery",
            name="source_platform",
            field=models.CharField(
                choices=[("slack", "Slack"), ("discord", "Discord"), ("buzz", "MLAI Chat")],
                db_index=True,
                max_length=20,
            ),
        ),
        migrations.AlterField(
            model_name="communitybridgedelivery",
            name="target_platform",
            field=models.CharField(
                choices=[("slack", "Slack"), ("discord", "Discord"), ("buzz", "MLAI Chat")],
                db_index=True,
                max_length=20,
            ),
        ),
        migrations.AlterField(
            model_name="communitybridgemessagelink",
            name="destination_platform",
            field=models.CharField(
                choices=[("slack", "Slack"), ("discord", "Discord"), ("buzz", "MLAI Chat")],
                db_index=True,
                max_length=20,
            ),
        ),
        migrations.AlterField(
            model_name="communitybridgemessagelink",
            name="source_platform",
            field=models.CharField(
                choices=[("slack", "Slack"), ("discord", "Discord"), ("buzz", "MLAI Chat")],
                db_index=True,
                max_length=20,
            ),
        ),
        migrations.AlterField(
            model_name="communitybridgereceipt",
            name="platform",
            field=models.CharField(
                choices=[("slack", "Slack"), ("discord", "Discord"), ("buzz", "MLAI Chat")],
                db_index=True,
                max_length=20,
            ),
        ),
        migrations.RunPython(copy_discord_destinations, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="communitybridgechannel",
            constraint=models.UniqueConstraint(
                condition=~models.Q(destination_channel_id=""),
                fields=("destination_platform", "destination_channel_id"),
                name="bridge_destination_platform_channel_unique",
            ),
        ),
    ]
