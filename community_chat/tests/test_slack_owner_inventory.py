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
from integrations.services import slack_chat_read_state as reads
from integrations.services.slack_chat_read_state import ReadTarget, _cache_key
from integrations.services.message_sync.read_priority import KEY as READ_PRIORITY_KEY
from integrations.services.message_sync.read_snapshots import publish_snapshot
from integrations.services.slack_owner_inventory import (
    collect_private_page, collect_public_page, complete_private_sweep,
    grant_metadata_consent, record_private_page,
    source_read_targets,
)
from integrations.services.slack_owner_inventory_api import (
    InventoryError, _validate_open_source, conversation_page, mark_inventory_read,
    mark_inventory_unread, request_open,
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

    def test_metadata_consent_restarts_older_partial_discovery_cursor(self):
        old_started = timezone.now() - timedelta(hours=1)
        dm._save_discovery_checkpoint(
            self.authority,
            cursor="old-partial-page",
            seen_channel_ids={"DOLDER"},
            failures=[],
            started_at=old_started,
        )
        self.consent()
        cursor, seen, failures, started_at = dm._load_discovery_checkpoint(self.authority)
        self.assertEqual(cursor, "")
        self.assertEqual(seen, set())
        self.assertEqual(failures, [])
        self.assertGreater(started_at, old_started)

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

    def test_private_metadata_pages_advance_without_history_import(self):
        self.consent()
        old_group = self.row("GOLD", kind="mpim", age=90 * 86400)
        new_dm = self.row("DNEW", kind="im")
        pages = [
            {"channels": [old_group], "response_metadata": {"next_cursor": "next"}},
            {"channels": [new_dm], "response_metadata": {"next_cursor": ""}},
        ]
        with patch("integrations.services.slack_dm_mirror._call_slack_with_grant_authority",
                   side_effect=pages) as slack:
            collect_private_page(self.authority)
            self.assertEqual(slack.call_args.kwargs["limit"], 50)
            self.assertEqual(slack.call_args.kwargs["cursor"], "")
            self.connection.refresh_from_db()
            state = self.connection.sync_cursor["slack_owner_inventory_v1"]
            self.assertEqual(state["directory_private_cursor"], "next")
            self.assertEqual(state["coverage"]["im"], "pending")
            # A history-import page cannot replace the metadata scan marker.
            record_private_page(self.authority, [self.row("DIMPORT")],
                                started_at=timezone.now(), kinds={"im"})
            self.assertFalse(SlackOwnerConversationInventory.objects.filter(
                slack_conversation_id="DIMPORT").exists())
            collect_private_page(self.authority)
            self.assertEqual(slack.call_args.kwargs["cursor"], "next")
        page = conversation_page(self.user, public_key=self.owner_key)
        self.assertEqual({row["slack_conversation_id"] for row in page["items"]},
                         {"GOLD", "DNEW"})
        self.assertEqual(page["coverage"]["im"], "complete")
        self.assertEqual(page["coverage"]["mpim"], "complete")
        self.assertEqual(page["coverage"]["private_channel"], "permission_required")
        self.assertEqual(slack.call_count, 2)
        self.connection.refresh_from_db()
        cursor = dict(self.connection.sync_cursor)
        state = dict(cursor["slack_owner_inventory_v1"])
        state["last_private_at"] = (timezone.now() - timedelta(minutes=6)).isoformat()
        cursor["slack_owner_inventory_v1"] = state
        self.connection.sync_cursor = cursor
        self.connection.save(update_fields=("sync_cursor", "updated_at"))
        with patch("integrations.services.slack_dm_mirror._call_slack_with_grant_authority",
                   return_value={"channels": [new_dm], "response_metadata": {"next_cursor": ""}}):
            collect_private_page(self.authority)
        self.assertEqual(set(SlackOwnerConversationInventory.objects.filter(
            grant=self.grant,
        ).values_list("slack_conversation_id", flat=True)), {"DNEW"})

    def test_consent_mid_sweep_waits_for_a_new_complete_source_cycle(self):
        old_cycle = timezone.now() - timedelta(minutes=1)
        self.consent()
        record_private_page(self.authority, [self.row("DLATE")],
                            started_at=old_cycle, kinds={"im"})
        complete_private_sweep(self.authority, started_at=old_cycle, kinds={"im"})
        page = conversation_page(self.user, public_key=self.owner_key)
        self.assertEqual(page["items"], [])
        self.assertEqual(page["coverage"]["im"], "pending")

    def test_pause_resume_requires_fresh_source_rows_before_showing_old_names(self):
        self.consent()
        record_private_page(
            self.authority, [self.row("GOLDPRIVATE", kind="private_channel")],
            started_at=timezone.now(), kinds={"private_channel"},
        )
        self.assertEqual(
            [item["slack_conversation_id"] for item in conversation_page(
                self.user, public_key=self.owner_key,
            )["items"]],
            ["GOLDPRIVATE"],
        )
        dm.pause_grant(self.grant)
        self.assertFalse(
            SlackOwnerConversationInventory.objects.filter(grant=self.grant).exists()
        )
        with (
            patch("integrations.services.slack_dm_mirror._complete_registration_cleanup_before_activation"),
            patch("integrations.services.slack_dm_mirror._prepare_generation_transition_locked"),
            patch("integrations.services.slack_dm_mirror._registration_cleanup_pending_locked", return_value=False),
            patch("integrations.services.slack_dm_mirror._normalize_grant_history_window_locked"),
        ):
            dm.resume_grant(self.grant)
        page = conversation_page(self.user, public_key=self.owner_key)
        self.assertEqual(page["items"], [])
        self.assertEqual(page["coverage"]["private_channel"], "pending")

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

    def test_first_page_hints_at_most_four_visible_source_reads_without_provider_calls(self):
        self.consent()
        rows = [self.row(f"D{i:03d}") for i in range(1, 9)]
        record_private_page(self.authority, rows, started_at=timezone.now(), kinds={"im"})
        for source_id, unread in (("D001", True), ("D002", True), ("D003", True),
                                  ("D007", False), ("D008", False)):
            cache.set(_cache_key(self.authority, ReadTarget(source_id, source_id, "im")),
                      {"available": True, "is_unread": unread, "unread_count": int(unread),
                       "has_personal_mention": False, "fetched_at": time.time() - 300},
                      timeout=86400)
        with patch("integrations.services.slack_owner_inventory_api._call_slack_with_grant_authority") as slack:
            first = conversation_page(self.user, public_key=self.owner_key, limit=8)
        slack.assert_not_called()
        self.assertEqual(len(first["items"]), 8)
        self.connection.refresh_from_db()
        hints = self.connection.sync_cursor[READ_PRIORITY_KEY]
        self.assertEqual(set(hints), {"D001", "D002", "D004", "D005"})
        self.assertEqual({hint["reason"] for hint in hints.values()}, {"visible"})
        original_hints = dict(hints)

        # An active hint is retained, rather than renewed on every UI poll.
        conversation_page(self.user, public_key=self.owner_key, limit=8)
        self.connection.refresh_from_db()
        hints = self.connection.sync_cursor[READ_PRIORITY_KEY]
        self.assertEqual(set(hints), {f"D{i:03d}" for i in range(1, 9)})
        self.assertEqual(hints["D001"], original_hints["D001"])

    def test_cursor_pages_and_mapped_rooms_do_not_enqueue_source_hints(self):
        self.consent()
        record_private_page(self.authority, [self.row("DAAA"), self.row("DBBB")],
                            started_at=timezone.now(), kinds={"im"})
        channel_id = uuid.uuid4()
        CommunityBridgeChannel.objects.create(
            slack_workspace_id="TIOAUTH", slack_channel_id="CJOINED",
            destination_platform=CommunityBridgePlatform.BUZZ,
            destination_channel_id=str(channel_id), enabled=True,
        )
        with patch("integrations.services.slack_dm_mirror._call_slack_with_grant_authority", return_value={
            "channels": [self.row("CJOINED", kind="public_channel", is_member=True)],
            "response_metadata": {"next_cursor": ""},
        }):
            collect_public_page(self.authority)
        first = conversation_page(self.user, public_key=self.owner_key, limit=1)
        self.connection.refresh_from_db()
        self.assertEqual(set(self.connection.sync_cursor[READ_PRIORITY_KEY]), {"DAAA"})
        second = conversation_page(self.user, public_key=self.owner_key,
                                   cursor=first["next_cursor"], limit=2)
        self.assertEqual(len(second["items"]), 2)
        self.connection.refresh_from_db()
        self.assertEqual(set(self.connection.sync_cursor[READ_PRIORITY_KEY]), {"DAAA"})
        conversation_page(self.user, public_key=self.owner_key, limit=3)
        self.connection.refresh_from_db()
        self.assertEqual(set(self.connection.sync_cursor[READ_PRIORITY_KEY]), {"DAAA", "DBBB"})

    def test_external_shared_source_never_receives_visible_read_hint(self):
        self.consent()
        record_private_page(self.authority, [
            self.row("DAAA"), self.row("DEXTERNAL", is_ext_shared=True),
        ], started_at=timezone.now(), kinds={"im"})
        page = conversation_page(self.user, public_key=self.owner_key)
        self.assertEqual(
            {item["slack_conversation_id"]: item["eligibility"] for item in page["items"]},
            {"DAAA": "eligible", "DEXTERNAL": "unsupported_external"},
        )
        self.connection.refresh_from_db()
        self.assertEqual(set(self.connection.sync_cursor[READ_PRIORITY_KEY]), {"DAAA"})

    def test_observed_coverage_does_not_claim_fresh_or_all_caught_up(self):
        self.consent()
        started = timezone.now()
        record_private_page(self.authority, [self.row("DSTALE")],
                            started_at=started, kinds={"im", "mpim", "private_channel"})
        complete_private_sweep(self.authority, started_at=started,
                               kinds={"im", "mpim", "private_channel"})
        with patch("integrations.services.slack_dm_mirror._call_slack_with_grant_authority", return_value={
            "channels": [], "response_metadata": {"next_cursor": ""},
        }):
            collect_public_page(self.authority)
        cache.set(_cache_key(self.authority, ReadTarget("DSTALE", "DSTALE", "im")),
                  {"available": True, "is_unread": False, "unread_count": 0,
                   "has_personal_mention": False, "fetched_at": time.time() - 300},
                  timeout=86400)
        page = conversation_page(self.user, public_key=self.owner_key)
        coverage = page["read_state_coverage"]
        self.assertTrue(page["discovery_complete"])
        self.assertTrue(coverage["observed_complete"])
        self.assertFalse(coverage["fresh_complete"])
        self.assertFalse(coverage["complete"])
        self.assertEqual(coverage["stale_count"], 1)

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

    def test_archived_positive_snapshot_is_not_an_actionable_unread(self):
        self.consent()
        record_private_page(self.authority, [self.row("DARCHIVED", is_archived=True)],
                            started_at=timezone.now(), kinds={"im"})
        key = _cache_key(self.authority, ReadTarget("DARCHIVED", "DARCHIVED", "im"))
        cache.set(key, {"available": True, "is_unread": True, "unread_count": 3,
                        "has_personal_mention": False, "fetched_at": time.time()}, timeout=86400)
        page = conversation_page(self.user, public_key=self.owner_key, unread_only=True)
        self.assertEqual(page["items"], [])
        self.assertEqual(page["read_state_coverage"]["eligible_count"], 1)
        self.assertEqual(page["read_state_coverage"]["fresh_unread_count"], 0)
        self.assertEqual(
            [target.slack_id for target in source_read_targets(self.grant, self.authority, [])],
            ["DARCHIVED"],
        )

    def test_unopenable_unreads_are_filtered_before_paging_and_counting(self):
        self.consent()
        record_private_page(self.authority, [
            self.row("DOLD", age=90 * 86400), self.row("DCLOSED", is_open=False),
            self.row("DFIRST"), self.row("DNEXT"),
        ], started_at=timezone.now(), kinds={"im"})
        for source in ("DOLD", "DCLOSED", "DFIRST", "DNEXT"):
            cache.set(_cache_key(self.authority, ReadTarget(source, source, "im")), {
                "available": True, "is_unread": True, "unread_count": 1,
                "has_personal_mention": False, "fetched_at": time.time(),
            }, timeout=86400)
        first = conversation_page(self.user, public_key=self.owner_key, unread_only=True, limit=1)
        self.assertEqual([item["slack_conversation_id"] for item in first["items"]], ["DFIRST"])
        self.assertTrue(first["items"][0]["openable"])
        self.assertEqual(first["read_state_coverage"]["fresh_unread_count"], 2)
        second = conversation_page(self.user, public_key=self.owner_key, unread_only=True,
                                   limit=1, cursor=first["next_cursor"])
        self.assertEqual([item["slack_conversation_id"] for item in second["items"]], ["DNEXT"])
        self.assertIsNone(second["next_cursor"])

    def test_source_read_targets_cover_unprovisioned_and_dedupe_routed(self):
        self.consent()
        record_private_page(self.authority, [self.row("DIOAUTH"), self.row("DNEW")],
                            started_at=timezone.now(), kinds={"im"})
        routed = [ReadTarget("room", "DIOAUTH", "im", conversation=self.conversation)]
        source = source_read_targets(self.grant, self.authority, routed)
        self.assertEqual([(t.channel_id, t.slack_id) for t in source], [("DNEW", "DNEW")])
        aliases = source_read_targets(self.grant, self.authority, routed, include_routed=True)
        self.assertEqual(
            [(t.channel_id, t.slack_id) for t in aliases],
            [("DIOAUTH", "DIOAUTH"), ("DNEW", "DNEW")],
        )

    def test_source_only_mark_read_uses_server_observed_frontier(self):
        self.consent()
        record_private_page(self.authority, [self.row("DNEW")],
                            started_at=timezone.now(), kinds={"im"})
        key = _cache_key(self.authority, ReadTarget("DNEW", "DNEW", "im"))
        with self.assertRaises(InventoryError) as missing:
            mark_inventory_read(self.user, public_key=self.owner_key,
                                slack_conversation_id="DNEW")
        self.assertEqual(missing.exception.code, "inventory_read_state_unavailable")
        cache.set(key, {"available": True, "is_unread": True,
                        "last_read": "100.000001", "latest_ts": "101.000001"})
        with patch.object(reads, "mark_read", return_value={"synced": True}) as mark:
            result = mark_inventory_read(self.user, public_key=self.owner_key,
                                         slack_conversation_id="DNEW")
        self.assertEqual(result, {"synced": True})
        self.assertEqual(mark.call_args.kwargs["channel_id"], "DNEW")
        self.assertEqual(mark.call_args.kwargs["source_ts"], "101.000001")

    def test_source_only_mark_unread_moves_slack_cursor_and_publishes_snapshot(self):
        self.consent()
        record_private_page(self.authority, [self.row("DNEW")],
                            started_at=timezone.now(), kinds={"im"})
        responses = [
            {"channel": {"id": "DNEW", "is_im": True, "is_member": True,
                         "last_read": "101.000001", "latest": {"ts": "101.000001"}}},
            {"messages": [
                {"ts": "101.000001", "user": "UOTHER", "text": "latest"},
                {"ts": "100.000001", "user": self.grant.slack_user_id, "text": "older"},
            ], "has_more": False},
            {"ok": True},
        ]
        with patch.object(reads, "_call_slack_with_grant_authority", side_effect=responses) as source:
            result = mark_inventory_unread(self.user, public_key=self.owner_key,
                                           slack_conversation_id="DNEW")
        self.assertTrue(result["synced"])
        self.assertTrue(result["channels"]["DNEW"]["is_unread"])
        self.assertEqual(result["last_read"], "100.000001")
        self.assertEqual([c.args[1] for c in source.call_args_list],
                         ["conversations_info", "conversations_history", "conversations_mark"])
        self.assertEqual(source.call_args.kwargs["ts"], "100.000001")

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
        self.assertFalse(page["read_state_coverage"]["observed_complete"])
        self.assertFalse(page["read_state_coverage"]["fresh_complete"])

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
                _validate_open_source(self.grant, self.authority, self.grant.owner_conversation_inventory.get(slack_conversation_id="DOLD"))
        self.assertEqual(old.exception.code, "inventory_history_consent_required")
        with patch("integrations.services.slack_owner_inventory_api._call_slack_with_grant_authority", side_effect=source) as source_call:
            status, payload = request_open(self.user, public_key=self.owner_key,
                                           slack_conversation_id="DRECENT")
        self.assertEqual((status, payload["state"]), (202, "importing"))
        source_call.assert_not_called()
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
                _validate_open_source(self.grant, self.authority, self.grant.owner_conversation_inventory.get(slack_conversation_id="DEMPTY"))
        self.assertEqual(empty.exception.code, "inventory_no_in_window_activity")
        self.assertEqual(calls, ["conversations_info", "conversations_history"])
