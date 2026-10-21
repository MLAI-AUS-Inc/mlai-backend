"""Exercise the approved 0049/0050 transition against real database schemas."""

from importlib import import_module
from types import SimpleNamespace

from django.contrib.auth import get_user_model
from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase
from django.utils import timezone

from integrations.models import (
    CommunityBridgeChannel, ExternalServiceConnection, SlackChannelMembership,
    SlackDmMirrorGrant,
)


OLD = ("integrations", "0049_community_bridge_channel_lifecycle")
NEW = ("integrations", "0050_slack_member_channels")
SEEDS = import_module("integrations.migrations.0049_community_bridge_channel_lifecycle")
PRIVATE_IDS = [item[0] for item in SEEDS.MANIFEST_DECISIONS if item[2] == "private"]
PUBLIC_EXCLUSION = next(item[0] for item in SEEDS.MANIFEST_DECISIONS if item[2] == "public")


class MemberChannelMigrationTests(TransactionTestCase):
    def setUp(self):
        # Always restore the current schema, including when fixture setup fails.
        self.addCleanup(self.migrate, NEW)
        self.old_apps = self.migrate(OLD)
        SEEDS.seed_manifest_decisions(self.old_apps, SimpleNamespace(connection=connection))
        self.Channel = self.old_apps.get_model("integrations", "CommunityBridgeChannel")

    def migrate(self, target):
        executor = MigrationExecutor(connection)
        executor.migrate([target])
        return executor.loader.project_state([target]).apps

    def test_exact_private_seeds_are_removed_public_exclusion_survives_and_reverse_is_safe(self):
        self.assertEqual(len(PRIVATE_IDS), 25)
        self.assertEqual(self.Channel.objects.filter(slack_channel_id__in=PRIVATE_IDS).count(), 25)
        apps = self.migrate(NEW)
        Channel = apps.get_model("integrations", "CommunityBridgeChannel")
        self.assertFalse(Channel.objects.filter(slack_channel_id__in=PRIVATE_IDS).exists())
        public = Channel.objects.get(slack_channel_id=PUBLIC_EXCLUSION)
        self.assertEqual((public.mirror_decision, public.enabled, public.audience), ("exclude", False, "members"))
        # An operator-created replacement must survive reversal unchanged.
        Channel.objects.create(slack_channel_id=PRIVATE_IDS[0], slack_channel_name="operator-choice", enabled=False)
        old = self.migrate(OLD).get_model("integrations", "CommunityBridgeChannel")
        self.assertEqual(old.objects.filter(slack_channel_id__in=PRIVATE_IDS).count(), 25)
        self.assertEqual(old.objects.get(slack_channel_id=PRIVATE_IDS[0]).slack_channel_name, "operator-choice")
        self.assertFalse(old.objects.filter(slack_channel_id__in=PRIVATE_IDS, enabled=True).exists())
        self.assertTrue(old.objects.filter(slack_channel_id=PUBLIC_EXCLUSION).exists())

    def test_operator_changes_to_each_seed_are_preserved(self):
        now = timezone.now()
        modifications = [
            {"slack_workspace_id": "TOTHER"}, {"slack_channel_name": "renamed"},
            {"destination_platform": "discord"}, {"destination_workspace_id": "workspace"},
            {"destination_channel_id": "existing-room"}, {"destination_channel_name": "room"},
            {"discord_guild_id": "legacy"}, {"discord_channel_id": "legacy-room"},
            {"discord_channel_name": "legacy-name"}, {"enabled": True},
            {"sync_edits": False}, {"sync_deletes": False}, {"sync_replies": False},
            {"pilot_settings": {"fixture": True}}, {"mirror_decision": "mirror"},
            {"hold_reason": "admin"}, {"lifecycle_source": "event"},
            {"slack_visibility": "public"}, {"slack_archived": True},
            {"slack_deleted": True}, {"slack_bot_member": True},
            {"slack_topic": "operator"}, {"slack_purpose": "operator"},
            {"slack_created_at": now}, {"room_state": {"fixture": True}},
        ]
        original_pks = {}
        for channel_id, changes in zip(PRIVATE_IDS, modifications, strict=True):
            row = self.Channel.objects.get(slack_channel_id=channel_id)
            original_pks[channel_id] = row.pk
            self.Channel.objects.filter(pk=row.pk).update(**changes)
        Channel = self.migrate(NEW).get_model("integrations", "CommunityBridgeChannel")
        self.assertEqual(dict(Channel.objects.filter(slack_channel_id__in=PRIVATE_IDS).values_list("slack_channel_id", "pk")), original_pks)
        for channel_id, changes in zip(PRIVATE_IDS, modifications, strict=True):
            row = Channel.objects.get(slack_channel_id=channel_id)
            for key, value in changes.items():
                if key != "slack_bot_member":  # This obsolete field is intentionally removed.
                    self.assertEqual(getattr(row, key), value)

    def test_retained_delivery_and_pending_refresh_prevent_cascade_cleanup(self):
        Delivery = self.old_apps.get_model("integrations", "CommunityBridgeDelivery")
        Refresh = self.old_apps.get_model("integrations", "CommunityBridgeChannelRefresh")
        delivery = Delivery.objects.create(
            channel=self.Channel.objects.get(slack_channel_id=PRIVATE_IDS[0]),
            source_platform="slack", target_platform="buzz", delivery_type="create",
            available_at=timezone.now(), payload={"synthetic": True},
        )
        Refresh.objects.create(
            slack_workspace_id=SEEDS.MLAI_SLACK_WORKSPACE_ID,
            slack_channel_id=PRIVATE_IDS[1], reason="fixture", available_at=timezone.now(),
        )
        Channel = self.migrate(NEW).get_model("integrations", "CommunityBridgeChannel")
        self.assertEqual(set(Channel.objects.filter(slack_channel_id__in=PRIVATE_IDS).values_list("slack_channel_id", flat=True)), set(PRIVATE_IDS[:2]))
        self.assertTrue(Delivery.objects.filter(pk=delivery.pk, payload={"synthetic": True}).exists())
        self.assertTrue(Refresh.objects.filter(slack_channel_id=PRIVATE_IDS[1]).exists())

    def test_only_enabled_mapped_buzz_rooms_become_community_and_keep_room_identity(self):
        cases = [
            ("CENABLED", "buzz", "mapped-room", True, "community"),
            ("CDISABLED", "buzz", "disabled-room", False, "members"),
            ("CUNMAPPED", "buzz", "", True, "members"),
            ("CDISCORD", "discord", "discord-room", True, "members"),
        ]
        for channel_id, platform, room_id, enabled, _ in cases:
            self.Channel.objects.create(
                slack_workspace_id="TTEST", slack_channel_id=channel_id,
                destination_platform=platform, destination_channel_id=room_id, enabled=enabled,
            )
        Channel = self.migrate(NEW).get_model("integrations", "CommunityBridgeChannel")
        for channel_id, platform, room_id, enabled, audience in cases:
            row = Channel.objects.get(slack_channel_id=channel_id)
            self.assertEqual((row.destination_platform, row.destination_channel_id, row.enabled, row.audience), (platform, room_id, enabled, audience))
            self.assertIsNone(row.reader_grant_id)
            self.assertIsNone(row.audience_generation)
            self.assertEqual(row.audience_hash, "")
            self.assertFalse(row.audience_sync_pending)


class MemberChannelModelTests(TestCase):
    def grant(self, suffix):
        user = get_user_model().objects.create_user(email=f"member-model-{suffix}@example.invalid")
        oauth = ExternalServiceConnection.objects.create(user=user, provider="slack", external_account_id=f"TTEST-{suffix}")
        return SlackDmMirrorGrant.objects.create(
            user=user, connection=oauth, slack_workspace_id="TTEST", slack_user_id=f"UTEST{suffix}",
            consented_at=timezone.now(),
        )

    def membership(self, grant, channel="CTEST"):
        return SlackChannelMembership.objects.create(
            grant=grant, slack_workspace_id="TTEST", slack_channel_id=channel,
            state="joined", source="event", observed_at=timezone.now(),
        )

    def test_membership_uniqueness_is_per_grant_and_channel(self):
        first, second = self.grant("a"), self.grant("b")
        self.membership(first)
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.membership(first)
        self.membership(second)
        self.membership(first, "COTHER")
        self.assertEqual(SlackChannelMembership.objects.count(), 3)

    def test_deleting_reader_removes_membership_but_preserves_paused_room(self):
        grant = self.grant("reader")
        self.membership(grant)
        room = CommunityBridgeChannel.objects.create(
            slack_channel_id="CTEST", destination_platform="buzz", destination_channel_id="retained-room",
            reader_grant=grant, sync_paused_reason="no_reader",
        )
        self.assertEqual(room.audience, "members")
        grant.delete()
        self.assertFalse(SlackChannelMembership.objects.exists())
        room.refresh_from_db()
        self.assertEqual((room.destination_channel_id, room.sync_paused_reason, room.reader_grant_id), ("retained-room", "no_reader", None))
