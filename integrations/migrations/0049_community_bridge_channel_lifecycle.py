from django.db import migrations, models


MLAI_SLACK_WORKSPACE_ID = "T05N9C1QSJC"

# Reviewed hold/no decisions from mlai-chat deploy/mlai/bridge-channels.conf.
# Recording them as rows means lifecycle mirroring can never pick these
# channels up automatically; an admin changes a decision on the row instead.
MANIFEST_DECISIONS = (
    ("C09LVV0FUQK", "ethan_test", "public", "exclude"),
    ("C0936Q64WTD", "test-me", "private", "hold"),
    ("C0654NWALGH", "underbelly", "private", "hold"),
    ("C0A0ZNH01N3", "vibe-marketing", "private", "hold"),
    ("C09C6GJ7H8A", "vibe-raising", "private", "hold"),
    ("C0B74JJ7BQS", "how-to-raise-money-for-my-startup", "private", "hold"),
    ("C0AR6K070T0", "how-to-raise-your-first-million", "private", "hold"),
    ("C0A1QQQTGSE", "fortnightly-show-and-tell", "private", "hold"),
    ("C0BA566P86M", "healthhack-sydney-organisers", "private", "hold"),
    ("C0A37L4S36D", "watt-the-hack-organisers", "private", "hold"),
    ("C0ARMJG3Q0Z", "monash-build-day", "private", "hold"),
    ("C0B16PJ4GBS", "stone-and-chalk", "private", "hold"),
    ("C08PLKELVNE", "marketing", "private", "hold"),
    ("C08JL94RV6F", "newsletters", "private", "hold"),
    ("C098A6DV70A", "tech-team", "private", "hold"),
    ("C0BAWU2MZDE", "aaron-ai", "private", "hold"),
    ("C0BL1RUKGTT", "aie-sydney", "private", "hold"),
    ("C0BDPD8JUAW", "exp-victor-ai", "private", "hold"),
    ("C0B1FJSEP7W", "mlai-studio", "private", "hold"),
    ("C096VPQ6YL8", "pesto-dev", "private", "hold"),
    ("C0B6H1SK61Z", "present-studio", "private", "hold"),
    ("C0BA24TPGNL", "receipts", "private", "exclude"),
    ("C0BMFDWTYBW", "roo-bookkeeping", "private", "exclude"),
    ("C09077RJ2T0", "tax_and_accounting", "private", "exclude"),
    ("C0A81UJPQLQ", "launchvic-grant", "private", "exclude"),
    ("C0ASWPBAL4U", "mlai-committee-2026", "private", "exclude"),
)


def seed_manifest_decisions(apps, schema_editor):
    channels = apps.get_model("integrations", "CommunityBridgeChannel")
    existing = set(
        channels.objects.filter(
            slack_channel_id__in=[row[0] for row in MANIFEST_DECISIONS]
        ).values_list("slack_channel_id", flat=True)
    )
    channels.objects.bulk_create(
        [
            channels(
                slack_workspace_id=MLAI_SLACK_WORKSPACE_ID,
                slack_channel_id=slack_channel_id,
                slack_channel_name=name,
                destination_platform="buzz",
                enabled=False,
                mirror_decision=decision,
                hold_reason="manifest",
                slack_visibility=visibility,
            )
            for slack_channel_id, name, visibility, decision in MANIFEST_DECISIONS
            if slack_channel_id not in existing
        ]
    )


def remove_manifest_decisions(apps, schema_editor):
    channels = apps.get_model("integrations", "CommunityBridgeChannel")
    channels.objects.filter(
        slack_channel_id__in=[row[0] for row in MANIFEST_DECISIONS],
        hold_reason="manifest",
        enabled=False,
        destination_channel_id="",
    ).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("integrations", "0048_owner_slack_conversation_inventory"),
    ]

    operations = [
        migrations.AddField(
            model_name="communitybridgechannel",
            name="mirror_decision",
            field=models.CharField(
                choices=[("mirror", "Mirror"), ("hold", "Hold for review"), ("exclude", "Never mirror")],
                db_index=True,
                default="mirror",
                max_length=10,
            ),
        ),
        migrations.AddField(
            model_name="communitybridgechannel",
            name="hold_reason",
            field=models.CharField(blank=True, default="", max_length=40),
        ),
        migrations.AddField(
            model_name="communitybridgechannel",
            name="lifecycle_source",
            field=models.CharField(
                blank=True,
                choices=[("", "Manual or manifest"), ("event", "Slack channel event"), ("reconcile", "Slack channel reconciliation")],
                default="",
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="communitybridgechannel",
            name="slack_visibility",
            field=models.CharField(default="public", max_length=10),
        ),
        migrations.AddField(
            model_name="communitybridgechannel",
            name="slack_archived",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="communitybridgechannel",
            name="slack_deleted",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="communitybridgechannel",
            name="slack_bot_member",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="communitybridgechannel",
            name="slack_topic",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="communitybridgechannel",
            name="slack_purpose",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="communitybridgechannel",
            name="slack_created_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="communitybridgechannel",
            name="slack_seen_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="communitybridgechannel",
            name="room_state",
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name="communitybridgechannel",
            name="room_sync_pending",
            field=models.BooleanField(db_index=True, default=False),
        ),
        migrations.AddField(
            model_name="communitybridgechannel",
            name="room_sync_error",
            field=models.CharField(blank=True, default="", max_length=100),
        ),
        migrations.AddField(
            model_name="communitybridgechannel",
            name="room_synced_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.CreateModel(
            name="CommunityBridgeChannelRefresh",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("slack_workspace_id", models.CharField(max_length=100)),
                ("slack_channel_id", models.CharField(max_length=100, unique=True)),
                ("reason", models.CharField(max_length=40)),
                ("attempts", models.PositiveIntegerField(default=0)),
                ("available_at", models.DateTimeField(db_index=True)),
                ("last_error_code", models.CharField(blank=True, default="", max_length=100)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "db_table": "community_bridge_channel_refresh",
            },
        ),
        migrations.CreateModel(
            name="CommunityBridgeLifecycleState",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("slack_workspace_id", models.CharField(max_length=100, unique=True)),
                ("auto_mirror_since", models.DateTimeField()),
                ("reconcile_cursor", models.CharField(blank=True, default="", max_length=255)),
                ("pass_started_at", models.DateTimeField(blank=True, null=True)),
                ("last_full_pass_at", models.DateTimeField(blank=True, null=True)),
                ("lease_expires_at", models.DateTimeField(blank=True, null=True)),
                ("last_error_code", models.CharField(blank=True, default="", max_length=100)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "db_table": "community_bridge_lifecycle_state",
            },
        ),
        migrations.RunPython(seed_manifest_decisions, remove_manifest_decisions),
    ]
