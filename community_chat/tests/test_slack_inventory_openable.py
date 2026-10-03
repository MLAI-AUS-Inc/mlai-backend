"""Content-free inventory availability rules, without database/provider I/O."""

from datetime import datetime, timezone
from contextlib import ExitStack, nullcontext
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase

from integrations.services.slack_owner_inventory_api import _item
from integrations.services import slack_owner_inventory_api as api


class SlackInventoryOpenableTests(SimpleTestCase):
    def item(self, *, ready=False, mirror=False, mapped=False, open_error=None, mirror_status="live", **changes):
        row = SimpleNamespace(
            slack_conversation_id="DTEST", kind="im", eligibility="eligible",
            source_activity_ts="1900000000", source_name="", display_name="Person",
            counterpart_slack_user_id="UTEST", source_archived=False,
            source_is_open=True, last_seen_at=datetime.now(timezone.utc),
        )
        for field, value in changes.items():
            setattr(row, field, value)
        conversation = SimpleNamespace(
            participant_profiles={}, participant_buzz_pubkeys=["owner"],
            mlai_channel_id="room", status=mirror_status,
        ) if mirror else None
        with patch("integrations.services.slack_owner_inventory_api.time.time", return_value=1900000060), patch("integrations.services.slack_owner_inventory_api.ready_for_display", return_value=ready), patch(
            "integrations.services.slack_owner_inventory_api.conversation_activity_at", return_value=None,
        ):
            return _item(
                row, mirror=conversation, public_map={"DTEST": "room"} if mapped else {},
                public_key="owner", state={}, read_state={"is_unread": True},
                oldest=1899000000, open_error=open_error,
            )

    def test_eligible_private_imports_and_ready_rooms_can_open(self):
        for kind in ("im", "mpim", "private_channel"):
            with self.subTest(kind=kind):
                self.assertTrue(self.item(kind=kind)["openable"])
                self.assertTrue(self.item(kind=kind, mirror=True)["openable"])
        self.assertTrue(self.item(kind="public_channel", mapped=True)["openable"])
        self.assertTrue(self.item(mirror=True, ready=True, source_is_open=False)["openable"])

    def test_known_history_membership_and_mapping_restrictions_cannot_open(self):
        for changes in (
            {"source_activity_ts": "1800000000"}, {"source_archived": True},
            {"source_is_open": False}, {"eligibility": "unsupported_external"},
            {"kind": "public_channel"},
            {"open_error": "inventory_conversation_unavailable"},
            {"open_error": "inventory_source_changed"},
        ):
            with self.subTest(changes=changes):
                self.assertFalse(self.item(**changes)["openable"])

    def test_fresh_worker_history_rejection_overrides_stale_inventory_activity(self):
        for error in ("inventory_history_consent_required", "inventory_no_in_window_activity"):
            with self.subTest(error=error):
                item = self.item(open_error=error)
                self.assertFalse(item["openable"])
                self.assertEqual(item["state"], "out_of_window")

    def test_confirmed_ready_room_supersedes_previous_open_failure(self):
        self.assertTrue(self.item(mirror=True, ready=True, open_error="inventory_source_changed")["openable"])

    def test_broken_import_waits_for_repair_before_it_can_open(self):
        self.assertFalse(self.item(mirror=True, mirror_status="error")["openable"])
        self.assertTrue(self.item(mirror=True, mirror_status="live")["openable"])

    def test_page_and_summary_retain_known_unreads_before_mirrors_can_open(self):
        rows = [SimpleNamespace(
            pk=index, slack_conversation_id=f"D{index}", kind="im", eligibility="eligible",
            source_activity_ts="1800000000" if index == 1 else "1900000000",
            source_name="", display_name="Person", counterpart_slack_user_id="UTEST",
            source_archived=False, source_is_open=index != 2,
            last_seen_at=datetime.now(timezone.utc),
        ) for index in range(1, 5)]
        connection = SimpleNamespace(sync_cursor={})
        grant = SimpleNamespace(pk=1, connection=connection, owner_conversation_inventory=MagicMock(), conversations=MagicMock())
        grant.owner_conversation_inventory.order_by.return_value = rows
        device = SimpleNamespace(pk=7, public_key="owner")
        state = {"revision": 1}
        snapshots = {row.slack_conversation_id: {
            "available": True, "is_unread": True, "unread_count": 1,
            "fetched_at": 1900000060,
        } for row in rows}
        with ExitStack() as stack:
            for name, kwargs in (
                ("_authorized", {"return_value": (grant, SimpleNamespace(workspace_id="T"), device, state)}),
                ("device_epoch", {"return_value": "epoch"}),
                ("_cache_key", {"side_effect": lambda authority, target: target.slack_id}),
                ("cache.get_many", {"return_value": snapshots}),
                ("time.time", {"return_value": 1900000060}),
                ("catalog_conversations", {"return_value": []}),
                ("CommunityBridgeChannel.objects.filter", {"return_value": []}),
                ("_grant_history_days", {"return_value": 30}),
                ("source_read_targets", {"return_value": []}),
                ("transaction.atomic", {"side_effect": lambda: nullcontext()}),
                ("_lock_slack_grant_api_authority", {"return_value": (grant, connection)}),
                ("CommunityChatDevice.objects", {}),
                ("has_metadata_consent", {"return_value": True}),
                ("state_for", {"return_value": state}),
            ):
                stack.enter_context(patch(f"{api.__name__}.{name}", **kwargs))
            first = api.conversation_page(object(), public_key="owner", unread_only=True, limit=1)
            self.assertEqual([item["slack_conversation_id"] for item in first["items"]], ["D1"])
            self.assertFalse(first["items"][0]["openable"])
            self.assertEqual(first["read_state_coverage"]["fresh_unread_count"], 4)
            self.assertEqual(first["read_revision"], 0)
            self.assertEqual(first["read_state_coverage"]["pending_refresh_count"], 0)
            self.assertIsNotNone(first["read_state_coverage"]["oldest_observed_at"])
            second = api.conversation_page(object(), public_key="owner", unread_only=True, limit=1, cursor=first["next_cursor"])
            self.assertEqual([item["slack_conversation_id"] for item in second["items"]], ["D2"])
            self.assertFalse(second["items"][0]["openable"])
            self.assertIsNotNone(second["next_cursor"])
            directory = api.conversation_page(object(), public_key="owner", limit=1)
            self.assertEqual(directory["items"][0]["slack_conversation_id"], "D1")
            self.assertFalse(directory["items"][0]["openable"])
            self.assertEqual(directory["read_state_coverage"]["fresh_unread_count"], 4)
            from integrations.services.slack_open_requests import KEY
            failure = {"epoch": "epoch", "until": 1900000120, "error": "inventory_history_consent_required"}
            connection.sync_cursor[KEY] = {"7:D3": failure}
            blocked = api.conversation_page(object(), public_key="owner", unread_only=True)
            self.assertEqual([item["slack_conversation_id"] for item in blocked["items"]], ["D1", "D2", "D3", "D4"])
            self.assertFalse(blocked["items"][2]["openable"])
            self.assertEqual(blocked["read_state_coverage"]["fresh_unread_count"], 4)
            for replacement in (
                {"7:D3": {**failure, "epoch": "previous-consent"}},
                {"8:D3": failure},
                {"7:D3": {**failure, "until": 1900000000}},
            ):
                connection.sync_cursor[KEY] = replacement
                restored = api.conversation_page(object(), public_key="owner", unread_only=True)
                self.assertEqual([item["slack_conversation_id"] for item in restored["items"]], ["D1", "D2", "D3", "D4"])

    def test_inventory_and_mapped_snapshot_share_revision_and_read_frontiers(self):
        value = api._read_state({
            "available": True, "is_unread": True, "unread_count": 2,
            "fetched_at": 1900000000, "revision": 123,
            "latest_ts": "1899999999.000002", "last_read": "1899999000.000001",
            "confirmed_at": 1899999500,
        }, 1900000060)
        self.assertEqual(value["revision"], 123)
        self.assertEqual(value["latest_ts"], "1899999999.000002")
        self.assertEqual(value["last_read"], "1899999000.000001")
        self.assertEqual(value["confirmed_at"], 1899999500)
        self.assertEqual(value["availability"], "available")

    def test_invalid_inventory_versions_never_become_a_confirmed_read(self):
        value = api._read_state({
            "available": True, "is_unread": True, "fetched_at": 1900000000,
            "revision": True, "confirmed_at": float("nan"),
            "latest_ts": "nan", "last_read": "-1",
        }, 1900000060)
        self.assertEqual(value["revision"], 0)
        self.assertEqual(value["confirmed_at"], 0)
        self.assertEqual(value["latest_ts"], "")
        self.assertEqual(value["last_read"], "")
