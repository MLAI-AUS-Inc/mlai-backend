"""Owner inventory consent, pagination, source unread and open-action regressions."""

from datetime import timedelta
import time
import uuid
from unittest.mock import patch

from django.core.cache import cache
from django.test import TransactionTestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIRequestFactory, force_authenticate

from community_chat.slack_views import SlackDmMirrorView
from community_chat.tests.test_slack_dm_io_authority import SlackDmIoAuthorityFixture
from integrations.models import (
    CommunityBridgeChannel, CommunityBridgePlatform,
    SlackOwnerConversationInventory,
)
from integrations.services import slack_dm_mirror as dm
from integrations.services.slack_chat_read_state import ReadTarget, _cache_key
from integrations.services.message_sync.read_snapshots import publish_snapshot
from integrations.services.slack_owner_inventory import (
    collect_public_page, complete_private_sweep, grant_metadata_consent, record_private_page,
    source_read_targets,
)
from integrations.services.slack_owner_inventory_api import (
    InventoryError, conversation_page, request_open,
)


@override_settings(MESSAGE_SYNC_ENABLED=True, SLACK_OWNER_INVENTORY_ENABLED=True)
class SlackOwnerInventoryTests(SlackDmIoAuthorityFixture, TransactionTestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.connection.scopes = list(self.connection.scopes) + [
            "groups:read", "groups:history", "channels:read", "channels:history",
        ]
        self.connection.save(update_fields=("scopes", "updated_at"))
        self.grant.refresh_from_db()
        self.grant.connection = self.connection
        self.authority = dm._capture_slack_grant_api_authority(self.grant, refresh_token=False)

    def row(self, source_id, *, kind="im", age=60, **flags):
        raw = {
            "id": source_id,
            "latest": {"ts": str(time.time() - age)},
            "is_im": kind == "im",
            "is_mpim": kind == "mpim",
            "is_private": kind == "private_channel",
            "is_channel": kind == "public_channel",
            "user": f"U{source_id}",
            "name": source_id.lower(),
            **flags,
        }
        return raw

    def consent(self):
        grant_metadata_consent(self.grant)
        self.connection.refresh_from_db()
        self.grant.connection = self.connection
        self.authority = dm._capture_slack_grant_api_authority(self.grant, refresh_token=False)

    def test_explicit_consent_then_source_directory_before_history_gate(self):
        with self.assertRaises(InventoryError) as error:
            conversation_page(self.user, public_key=self.owner_key)
        self.assertEqual((error.exception.code, error.exception.status_code),
                         ("inventory_consent_required", 403))
        self.consent()
        started = timezone.now()
        old = self.row("DOLD", age=90 * 86400, is_archived=None, is_open=False)
        group = self.row("GTEAM", kind="mpim", is_archived=False)
        record_private_page(self.authority, [old, group], started_at=started,
                            kinds={"im", "mpim", "private_channel"})
        complete_private_sweep(self.authority, started_at=started,
                               kinds={"im", "mpim", "private_channel"})
        page = conversation_page(self.user, public_key=self.owner_key)
        by_id = {row["slack_conversation_id"]: row for row in page["items"]}
        self.assertEqual(by_id["DOLD"]["state"], "out_of_window")
        self.assertIsNone(by_id["DOLD"]["source_archived"])
        self.assertIs(by_id["DOLD"]["source_is_open"], False)
        self.assertEqual(by_id["GTEAM"]["state"], "source_only")
        self.assertIsNone(by_id["GTEAM"]["mlai_channel_id"])
        self.assertEqual(page["total"], 2)
        self.assertFalse(page["discovery_complete"])
        self.assertEqual(page["coverage"]["public_channel"], "pending")
        self.assertEqual(page["read_state_coverage"]["unknown_count"], 2)

    def test_metadata_only_post_never_defaults_or_changes_history_consent(self):
        self.grant.status = "paused"
        self.grant.save(update_fields=("status", "updated_at"))
        request = APIRequestFactory().post(
            "/community-chat/slack/", {"include_inventory_metadata": True}, format="json",
        )
        force_authenticate(request, user=self.user)
        with patch("community_chat.slack_views.activate_connection") as activate:
            response = SlackDmMirrorView.as_view(throttle_classes=[])(request)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.data, {"error": "slack_inventory_unavailable"})
        activate.assert_not_called()
        self.grant.refresh_from_db()
        self.assertEqual(self.grant.history_days, 30)

    def test_full_sweep_deletes_only_after_final_page_and_rejects_malformed_page(self):
        self.consent()
        started = timezone.now()
        kinds = {"im", "mpim"}
        record_private_page(self.authority, [self.row("DOLDER")], started_at=started, kinds=kinds)
        complete_private_sweep(self.authority, started_at=started, kinds=kinds)
        new_cycle = started + timedelta(minutes=1)
        record_private_page(self.authority, [self.row("DNEW")], started_at=new_cycle, kinds=kinds)
        self.assertTrue(SlackOwnerConversationInventory.objects.filter(slack_conversation_id="DOLDER").exists())
        with self.assertRaises(ValueError):
            record_private_page(self.authority, [{"id": "bad"}], started_at=new_cycle, kinds=kinds)
        self.assertTrue(SlackOwnerConversationInventory.objects.filter(slack_conversation_id="DOLDER").exists())
        complete_private_sweep(self.authority, started_at=new_cycle, kinds=kinds)
        self.assertFalse(SlackOwnerConversationInventory.objects.filter(slack_conversation_id="DOLDER").exists())

    def test_consent_mid_sweep_waits_for_a_new_complete_source_cycle(self):
        old_cycle = timezone.now() - timedelta(minutes=1)
        self.consent()
        record_private_page(self.authority, [self.row("DLATE")],
                            started_at=old_cycle, kinds={"im"})
        complete_private_sweep(self.authority, started_at=old_cycle, kinds={"im"})
        page = conversation_page(self.user, public_key=self.owner_key)
        self.assertEqual(page["items"], [])
        self.assertEqual(page["coverage"]["im"], "pending")

    def test_unread_only_includes_stale_positive_and_bound_cursor_revision(self):
        self.consent()
        started = timezone.now()
        record_private_page(self.authority, [self.row("DAAA"), self.row("DBBB"), self.row("DCCC")],
                            started_at=started, kinds={"im"})
        cache.set(_cache_key(self.authority, ReadTarget("DAAA", "DAAA", "im")),
                  {"available": True, "is_unread": True, "unread_count": 2,
                   "has_personal_mention": False, "fetched_at": time.time()}, timeout=86400)
        cache.set(_cache_key(self.authority, ReadTarget("DBBB", "DBBB", "im")),
                  {"available": True, "is_unread": True, "unread_count": 1,
                   "has_personal_mention": False, "fetched_at": time.time() - 300}, timeout=86400)
        first = conversation_page(self.user, public_key=self.owner_key, unread_only=True, limit=1)
        self.assertEqual([r["slack_conversation_id"] for r in first["items"]], ["DAAA"])
        self.assertEqual(first["read_state_coverage"]["fresh_unread_count"], 1)
        self.assertEqual(first["read_state_coverage"]["provisional_unread_count"], 1)
        self.assertEqual(first["read_state_coverage"]["unknown_count"], 1)
        second = conversation_page(self.user, public_key=self.owner_key, unread_only=True,
                                   cursor=first["next_cursor"], limit=1)
        self.assertEqual(second["items"][0]["read_state"]["availability"], "stale")
        with self.assertRaises(InventoryError) as invalid:
            conversation_page(self.user, public_key=self.owner_key, cursor="tampered")
        self.assertEqual((invalid.exception.code, invalid.exception.status_code),
                         ("inventory_cursor_invalid", 403))
        record_private_page(self.authority, [self.row("DDDD")], started_at=started, kinds={"im"})
        with self.assertRaises(InventoryError) as stale:
            conversation_page(self.user, public_key=self.owner_key, unread_only=True,
                              cursor=first["next_cursor"], limit=1)
        self.assertEqual(stale.exception.code, "inventory_cursor_stale")

    def test_quiet_read_refresh_does_not_invalidate_unread_cursor(self):
        self.consent()
        started = timezone.now()
        record_private_page(self.authority, [self.row("DAAA"), self.row("DBBB")],
                            started_at=started, kinds={"im"})
        key = _cache_key(self.authority, ReadTarget("DAAA", "DAAA", "im"))
        self.connection.refresh_from_db()
        initial = {"available": True, "is_unread": True, "unread_count": 1,
                   "has_personal_mention": False, "fetched_at": time.time()}
        publish_snapshot(self.connection, key, initial)
        first = conversation_page(self.user, public_key=self.owner_key,
                                  unread_only=True, limit=1)
        original_revision = first["read_state_coverage"]["read_revision"]
        publish_snapshot(self.connection, key, {**initial, "fetched_at": time.time() + 1})
        after = conversation_page(self.user, public_key=self.owner_key,
                                  unread_only=True, limit=1)
        self.assertEqual(after["read_state_coverage"]["read_revision"], original_revision)

    def test_archived_positive_snapshot_is_excluded_from_unread_only(self):
        self.consent()
        record_private_page(self.authority, [self.row("DARCHIVED", is_archived=True)],
                            started_at=timezone.now(), kinds={"im"})
        key = _cache_key(self.authority, ReadTarget("DARCHIVED", "DARCHIVED", "im"))
        cache.set(key, {"available": True, "is_unread": True, "unread_count": 3,
                        "has_personal_mention": False, "fetched_at": time.time()}, timeout=86400)
        page = conversation_page(self.user, public_key=self.owner_key, unread_only=True)
        self.assertEqual(page["items"], [])
        self.assertEqual(page["read_state_coverage"]["eligible_count"], 0)

    def test_source_read_targets_cover_unprovisioned_and_dedupe_routed(self):
        self.consent()
        record_private_page(self.authority, [self.row("DIOAUTH"), self.row("DNEW")],
                            started_at=timezone.now(), kinds={"im"})
        routed = [ReadTarget("room", "DIOAUTH", "im", conversation=self.conversation)]
        source = source_read_targets(self.grant, self.authority, routed)
        self.assertEqual([(t.channel_id, t.slack_id) for t in source], [("DNEW", "DNEW")])

    def test_new_oauth_authority_hides_old_names_until_new_consent(self):
        self.consent()
        record_private_page(self.authority, [self.row("DOLD")],
                            started_at=timezone.now(), kinds={"im"})
        old_epoch = conversation_page(self.user, public_key=self.owner_key)["inventory_epoch"]
        self.connection.provider_metadata = {
            **(self.connection.provider_metadata or {}),
            "mlai_slack_oauth_generation": 1,
        }
        self.connection.save(update_fields=("provider_metadata", "updated_at"))
        self.grant.connection = self.connection
        with self.assertRaises(InventoryError) as denied:
            conversation_page(self.user, public_key=self.owner_key)
        self.assertEqual(denied.exception.code, "inventory_consent_required")
        self.consent()
        page = conversation_page(self.user, public_key=self.owner_key)
        self.assertEqual(page["items"], [])
        self.assertNotEqual(page["inventory_epoch"], old_epoch)

    def test_public_mapping_is_metadata_only_and_open_never_expands_history(self):
        self.consent()
        with patch("integrations.services.slack_dm_mirror._call_slack_with_grant_authority", return_value={
            "channels": [self.row("CJOINED", kind="public_channel", is_member=True)],
            "response_metadata": {"next_cursor": ""},
        }):
            collect_public_page(self.authority)
        unmapped = conversation_page(self.user, public_key=self.owner_key)["items"][0]
        self.assertEqual((unmapped["kind"], unmapped["state"]), ("public_channel", "unmapped"))
        with self.assertRaises(InventoryError) as error:
            request_open(self.user, public_key=self.owner_key, slack_conversation_id="CJOINED")
        self.assertEqual(error.exception.code, "public_mapping_required")
        channel_id = uuid.uuid4()
        CommunityBridgeChannel.objects.create(
            slack_workspace_id="TIOAUTH", slack_channel_id="CJOINED",
            destination_platform=CommunityBridgePlatform.BUZZ,
            destination_channel_id=str(channel_id), enabled=True,
        )
        mapped = conversation_page(self.user, public_key=self.owner_key)["items"][0]
        self.assertEqual((mapped["state"], mapped["mlai_channel_id"]), ("mapped", str(channel_id)))

    def test_permission_limited_public_coverage_cannot_claim_unread_completeness(self):
        self.connection.scopes = [
            scope for scope in self.connection.scopes if scope not in {"channels:read", "channels:history"}
        ]
        self.connection.save(update_fields=("scopes", "updated_at"))
        self.grant.connection = self.connection
        self.authority = dm._capture_slack_grant_api_authority(self.grant, refresh_token=False)
        self.consent()
        started = timezone.now()
        record_private_page(self.authority, [], started_at=started,
                            kinds={"im", "mpim", "private_channel"})
        complete_private_sweep(self.authority, started_at=started,
                               kinds={"im", "mpim", "private_channel"})
        collect_public_page(self.authority)
        page = conversation_page(self.user, public_key=self.owner_key)
        self.assertTrue(page["discovery_terminal"])
        self.assertFalse(page["discovery_complete"])
        self.assertEqual(page["coverage"]["public_channel"], "permission_required")
        self.assertFalse(page["read_state_coverage"]["complete"])

    def test_open_rechecks_source_and_schedules_only_in_window(self):
        self.consent()
        record_private_page(self.authority, [self.row("DRECENT"), self.row("DOLD", age=90 * 86400)],
                            started_at=timezone.now(), kinds={"im"})
        self.grant.last_discovery_at = timezone.now()
        self.grant.save(update_fields=("last_discovery_at", "updated_at"))
        def source(_authority, method, **kwargs):
            self.assertEqual(method, "conversations_info")
            age = 90 * 86400 if kwargs["channel"] == "DOLD" else 60
            return {"channel": {
                "id": kwargs["channel"], "is_im": True, "is_member": True,
                "latest": {"ts": str(time.time() - age)},
            }}

        with patch("integrations.services.slack_owner_inventory_api._call_slack_with_grant_authority", side_effect=source):
            with self.assertRaises(InventoryError) as old:
                request_open(self.user, public_key=self.owner_key, slack_conversation_id="DOLD")
        self.assertEqual(old.exception.code, "inventory_history_consent_required")
        with patch("integrations.services.slack_owner_inventory_api._call_slack_with_grant_authority", side_effect=source) as source_call:
            status, payload = request_open(self.user, public_key=self.owner_key,
                                           slack_conversation_id="DRECENT")
        self.assertEqual((status, payload["state"]), (202, "importing"))
        source_call.assert_called_once()
        self.grant.refresh_from_db()
        self.assertIsNone(self.grant.last_discovery_at)
        self.assertEqual(self.grant.history_days, 30)

    def test_unknown_activity_open_finishes_with_explicit_no_activity_result(self):
        self.consent()
        record_private_page(self.authority, [{
            "id": "DEMPTY", "is_im": True, "user": "UEMPTY",
        }], started_at=timezone.now(), kinds={"im"})
        calls = []

        def source(_authority, method, **kwargs):
            calls.append(method)
            if method == "conversations_info":
                return {"channel": {"id": "DEMPTY", "is_im": True, "is_member": True}}
            return {"messages": []}

        with patch("integrations.services.slack_owner_inventory_api._call_slack_with_grant_authority", side_effect=source):
            with self.assertRaises(InventoryError) as empty:
                request_open(self.user, public_key=self.owner_key, slack_conversation_id="DEMPTY")
        self.assertEqual(empty.exception.code, "inventory_no_in_window_activity")
        self.assertEqual(calls, ["conversations_info", "conversations_history"])
