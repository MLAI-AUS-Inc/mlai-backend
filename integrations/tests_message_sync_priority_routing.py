"""Database-free regressions for priority at actual worker dispatch boundaries."""
from contextlib import ExitStack, nullcontext
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase

from integrations.services import slack_chat_read_state as reads, slack_dm_mirror as dm, slack_open_requests
from integrations.services.message_sync import discovery, read_priority, read_state, runner
from integrations.services.message_sync.request_priority import current_priority, request_priority


class DiscoveryPriorityRoutingTests(SimpleTestCase):
    now = 1_800_000_000

    def open_request(self, **changes):
        return {"id": "synthetic-intent", "source_id": "G1", "public_key": "synthetic-key",
                "epoch": "synthetic-owner-device-epoch", "requested_at": self.now - 30,
                "until": self.now + 60, "due": self.now, "state": "pending", **changes}

    def test_pending_owner_open_is_foreground_without_a_live_callback(self):
        self._dispatch({slack_open_requests.KEY: {"device:G1": self.open_request()}}, "foreground")
        # Another owner's queued open must not elevate this owner's sweep.
        self._dispatch({}, "background")

    def test_expired_completed_and_malformed_open_requests_do_not_promote_discovery(self):
        for value in (None, [], "invalid", {"device:G1": None},
                      {"device:G1": {"state": "pending", "until": self.now + 60}},
                      {"device:G1": self.open_request(until=self.now - 1)},
                      {"device:G1": self.open_request(state="importing")},
                      {"device:G1": self.open_request(state="error")},
                      {"device:G1": self.open_request(until="invalid")},
                      {"device:G1": self.open_request(until=float("inf"))},
                      {"device:G1": self.open_request(requested_at=float("nan"))},
                      {"device:G1": self.open_request(source_id="")}):
            with self.subTest(value=value):
                self._dispatch({slack_open_requests.KEY: value}, "background")

    def test_malformed_open_does_not_hide_another_valid_owner_open(self):
        self._dispatch({slack_open_requests.KEY: {
            "invalid": {"until": "invalid"}, "device:G1": self.open_request(),
        }}, "foreground")

    def test_staged_private_callbacks_keep_owner_discovery_foreground(self):
        for channel_id in ("D1", "G1", "Cprivate"):
            with self.subTest(channel_id=channel_id):
                self._dispatch({dm.PENDING_EVENT_CHECKPOINT_KEY: [{
                    "channel_id": channel_id, "ciphertext": "synthetic-encrypted-envelope",
                }]}, "foreground")

    def test_bulk_discovery_and_invalid_envelopes_stay_background(self):
        for pending in (None, [], {}, [None], [{"channel_id": "G1"}],
                        [{"channel_id": "", "ciphertext": "opaque"}],
                        [{"channel_id": "G1", "ciphertext": ""}]):
            with self.subTest(pending=pending):
                self._dispatch({dm.PENDING_EVENT_CHECKPOINT_KEY: pending}, "background")

    def test_other_owner_pending_callbacks_do_not_promote_bulk_owner(self):
        other_owner = SimpleNamespace(sync_cursor={dm.PENDING_EVENT_CHECKPOINT_KEY: [{
            "channel_id": "Gother", "ciphertext": "synthetic-encrypted-envelope",
        }]})
        self.assertEqual(discovery.discovery_request_priority(other_owner), "foreground")
        self._dispatch({}, "background")

    def _dispatch(self, cursor, expected):
        lease = discovery.DiscoveryLease(1, 2, 3, "synthetic-lease")
        grant = SimpleNamespace(connection=SimpleNamespace(sync_cursor=cursor))
        seen = []
        with patch.object(discovery, "claim_discovery", return_value=lease), patch.object(
            discovery, "finish_discovery"
        ) as finish, patch.object(discovery.SlackDmMirrorGrant.objects, "select_related") as query, patch.object(
            dm, "discover_conversations", side_effect=lambda actual: seen.append((actual, current_priority()))
        ), patch.object(dm, "decrypt_credential_value", side_effect=AssertionError("Priority must not decrypt")), patch.object(
            slack_open_requests.time, "time", return_value=self.now
        ):
            query.return_value.get.return_value = grant
            self.assertTrue(discovery.discover_once(60))
        self.assertEqual(seen, [(grant, expected)])
        finish.assert_called_once_with(lease, delay_seconds=1.0, error_code="", return_turn=False)
        self.assertEqual(current_priority(), "foreground")


class ReadPriorityRoutingTests(SimpleTestCase):
    now = 1_800_000_000

    def test_new_public_activity_without_hint_is_foreground_at_provider_call(self):
        target = reads.ReadTarget("room", "C1", "public_channel", source_activity_ts=str(self.now - 10))
        self._dispatch(target, {"available": True, "is_unread": False,
                                "fetched_at": self.now - 90, "latest_ts": str(self.now - 120)},
                       expected="foreground")

    def test_ordinary_known_unread_and_quiet_refreshes_remain_background(self):
        target = reads.ReadTarget("room", "C1", "public_channel", source_activity_ts=str(self.now - 100))
        for unread in (True, False):
            with self.subTest(unread=unread):
                self._dispatch(target, {"available": True, "is_unread": unread,
                                        "fetched_at": self.now - 90, "latest_ts": str(self.now - 100)},
                               expected="background")

    def test_visible_and_durable_activity_hints_remain_foreground(self):
        target = reads.ReadTarget("room", "D1", "im")
        for hint in ({"until": self.now + 30}, {"until": self.now - 30, "reason": "activity"}):
            with self.subTest(hint=hint):
                self._dispatch(target, {"fetched_at": self.now - 90},
                               hint=hint, expected="foreground")

    def test_source_requested_reobservation_remains_foreground(self):
        self._dispatch(reads.ReadTarget("room", "D1", "im"),
                       {"fetched_at": self.now - 2, "refresh_required": True}, expected="foreground")

    def test_activity_must_be_valid_newer_and_not_already_observed_or_excluded(self):
        base = {"fetched_at": self.now - 90, "latest_ts": str(self.now - 120)}
        cases = [
            ("nan", base), ("inf", base), (str(self.now + 301), base),
            (str(self.now - 10), {**base, "excluded": True}),
            (str(self.now - 10), {**base, "fetched_at": self.now - 5}),
            (str(self.now - 100), base),
            (str(self.now - 10), {**base, "latest_ts": str(self.now - 1)}),
            (str(self.now - 10), {}),
        ]
        for source, snapshot in cases:
            with self.subTest(source=source, snapshot=snapshot):
                target = reads.ReadTarget("room", "C1", "public_channel", source_activity_ts=source)
                self.assertEqual(read_state.refresh_request_priority(target, snapshot, None, now=self.now),
                                 "background")

    def test_explicit_read_receipt_is_foreground_even_inside_background_context(self):
        with request_priority("background"):
            self._dispatch(reads.ReadTarget("room", "D1", "im"), {},
                           expected=None, receipt_result=1)
            self.assertEqual(current_priority(), "background")

    def _dispatch(self, target, snapshot, *, hint=None, expected, receipt_result=None):
        lease = read_state.ReadStateLease(1, 2, 3, "synthetic-lease", None, "")
        authority = SimpleNamespace(scopes={target.read_scope})
        grant = SimpleNamespace(user_id=3)
        connection = SimpleNamespace(sync_cursor={read_priority.KEY: {target.slack_id: hint}} if hint else {})
        observed, receipts = [], []
        def refresh(*_args):
            observed.append(current_priority())
            return {"available": True, "fetched_at": self.now}
        def flush(*_args):
            receipts.append(current_priority())
            return receipt_result
        with ExitStack() as stack:
            def replace(obj, name, **kwargs):
                return stack.enter_context(patch.object(obj, name, **kwargs))
            replace(read_state, "enabled", return_value=True)
            replace(read_state, "claim_read_state", return_value=lease)
            finish = replace(read_state, "finish_read_state")
            replace(read_state.timezone, "now", return_value=datetime.fromtimestamp(self.now, tz=timezone.utc))
            query = replace(read_state.SlackDmMirrorGrant.objects, "select_related")
            query.return_value.get.return_value = grant
            devices = replace(read_state.CommunityChatDevice.objects, "filter")
            devices.return_value.values_list.return_value = ["synthetic-key"]
            replace(reads, "_assert_grant_connection_authorized")
            replace(reads, "_capture_slack_grant_api_authority", return_value=authority)
            targets = replace(reads, "_targets_for_keys", return_value=[target])
            replace(reads, "_lock_slack_grant_api_authority", return_value=(grant, connection))
            replace(reads, "_cache_key", side_effect=lambda _authority, row: row.slack_id)
            replace(reads, "refresh_target", side_effect=refresh)
            replace(read_state.transaction, "atomic", side_effect=nullcontext)
            replace(read_state.cache, "get_many", return_value={target.slack_id: snapshot})
            replace(read_priority, "prune_unroutable_hints")
            replace(read_priority, "satisfy_refresh")
            stack.enter_context(patch("integrations.services.slack_owner_inventory.source_read_targets", return_value=[]))
            stack.enter_context(patch("integrations.services.message_sync.read_snapshots.flush_notification"))
            stack.enter_context(patch("integrations.services.message_sync.receipts.flush_read_once", side_effect=flush))
            self.assertEqual(read_state.refresh_read_state_once(), 1)
            if receipt_result is not None:
                targets.assert_not_called()
        self.assertEqual(receipts, ["foreground"])
        self.assertEqual(observed, [] if expected is None else [expected])
        self.assertEqual(finish.call_args.kwargs["error"], "")


class HistoryPriorityRoutingTests(SimpleTestCase):
    def test_recent_open_promotes_head_only_and_quiet_head_stays_background(self):
        for kind, opened, expected in (("head", True, "foreground"), ("archive", True, "background"),
                                       ("thread", True, "background"), ("head", False, "background")):
            with self.subTest(kind=kind, opened=opened):
                state = SimpleNamespace(private_conversation_id=1, foreground_refresh=opened)
                lease = SimpleNamespace(state_id=1, kind=kind)
                rows = Mock()
                rows.select_related.return_value.get.return_value = state
                seen = []
                with patch.object(runner, "enabled", return_value=True), patch.object(
                    runner, "claim_job", return_value=lease
                ), patch.object(runner.BridgeSyncState.objects, "all", return_value=rows), patch(
                    "integrations.services.slack_chat_refresh.prioritize_open_conversations", return_value=rows
                ), patch.object(runner, "private_page", side_effect=lambda *_args: seen.append(current_priority())), patch.object(
                    runner, "fail_job"
                ) as failure:
                    self.assertEqual(runner.process_history_once(seed=False), 1)
                self.assertEqual(seen, [expected])
                failure.assert_not_called()
