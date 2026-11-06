"""Database-free targeted routing of parked group-DM/private-channel events."""
from contextlib import ExitStack, nullcontext
from datetime import datetime, timezone
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase, override_settings
from slack_sdk.errors import SlackApiError

from integrations.services import slack_dm_mirror as dm
from integrations.services.message_sync import staged_routing as routing
from integrations.services.message_sync.scheduler import BudgetDeferred, LeaseLost

PENDING = dm.PENDING_EVENT_CHECKPOINT_KEY
GROUP_SCOPES = sorted(dm.DIRECT_DM_SCOPES | dm.GROUP_DM_SCOPES)
ALL_SCOPES = sorted(dm.DIRECT_DM_SCOPES | dm.GROUP_DM_SCOPES | dm.PRIVATE_CHANNEL_SCOPES)


def parked(channel_id, at, *, kind=None):
    item = {"key": f"{channel_id}:{at}", "channel_id": channel_id, "ciphertext": "opaque-ciphertext",
            "staged_at": datetime.fromtimestamp(at, tz=timezone.utc).isoformat()}
    if kind:
        item["kind"] = kind
    return item


def source(channel_id, **fields):
    kind = fields.pop("kind", "mpim")
    flags = {"im": {"is_im": True}, "mpim": {"is_mpim": True, "is_private": True},
             "private_channel": {"is_private": True}}[kind]
    return {"id": channel_id, "name": "synthetic", "is_member": True, "is_open": True,
            "latest": {"ts": "1899999990.000100", "text": "private body must not be forwarded"},
            **flags, **fields}


def slack_error(code):
    return SlackApiError("synthetic", {"ok": False, "error": code})


@override_settings(MESSAGE_SYNC_ENABLED=True, MESSAGE_SYNC_TARGETED_STAGED_ROUTING=True)
class TargetedStagedRoutingTests(SimpleTestCase):
    def setUp(self):
        self.now = 1900000000.0
        self.connection = SimpleNamespace(sync_cursor={PENDING: []}, scopes=ALL_SCOPES,
                                          provider_metadata={}, save=MagicMock())
        self.grant = SimpleNamespace(pk=7, connection=self.connection, consent_version="synthetic-consent",
                                     conversations=MagicMock())
        self.grant.conversations.filter.return_value.exists.return_value = False
        self.authority = SimpleNamespace(grant_id=7)
        self.sources = {}
        self.provider_calls = []
        self.private_ok = True
        self.live = True
        self.rooms = {}
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(routing.time, "time", side_effect=lambda: self.now))
        stack.enter_context(patch.object(routing.transaction, "atomic", side_effect=lambda: nullcontext()))
        self.lock = stack.enter_context(patch.object(
            dm, "_lock_slack_grant_api_authority", side_effect=lambda *_, **__: (self.grant, self.connection)))
        stack.enter_context(patch.object(dm, "_call_slack_with_grant_authority", side_effect=self.call))
        stack.enter_context(patch.object(dm, "private_channels_enabled", side_effect=lambda _: self.private_ok))
        stack.enter_context(patch.object(dm, "_stored_profile_cache", return_value={}))
        self.discard = stack.enter_context(patch.object(
            dm, "_discard_staged_events_for_channel", side_effect=lambda _, channel_id: self.remove(channel_id)))
        self.retire = stack.enter_context(patch.object(dm, "_retire_ineligible_from_slack_response"))
        self.discover = stack.enter_context(patch.object(dm, "_discover_conversation", side_effect=self.provision))
        self.drain = stack.enter_context(patch.object(
            dm, "_drain_staged_events_for_conversation",
            side_effect=lambda _, pk, **__: self.remove(self.rooms[pk])))
        conversations = stack.enter_context(patch.object(dm.SlackDmMirrorConversation, "objects"))
        conversations.filter.side_effect = lambda **_: SimpleNamespace(exists=lambda: self.live)
        self.progress = stack.enter_context(patch(
            "integrations.services.slack_discovery_progress.conversation_progress",
            side_effect=lambda *_: nullcontext()))
        self.recent_first = stack.enter_context(patch(
            "integrations.services.message_sync.recent_history.enabled", return_value=False))

    def park(self, *items):
        self.connection.sync_cursor[PENDING].extend(items)

    def remove(self, channel_id):
        cursor = dict(self.connection.sync_cursor)
        cursor[PENDING] = [item for item in cursor.get(PENDING, []) if item["channel_id"] != channel_id]
        self.connection.sync_cursor = cursor

    def call(self, authority, method, *, required_scopes, **kwargs):
        self.assertEqual(method, "conversations_info")
        self.provider_calls.append((kwargs["channel"], frozenset(required_scopes)))
        value = self.sources[kwargs["channel"]]
        if isinstance(value, BaseException):
            raise value
        return {"ok": True, "channel": value}

    def provision(self, grant, authority, raw, **kwargs):
        pk = 100 + len(self.rooms)
        self.rooms[pk] = raw["id"]
        return SimpleNamespace(pk=pk)

    def route(self, **kwargs):
        return routing.route_staged_events(self.grant, self.authority, **kwargs)

    def retry(self):
        return (self.connection.sync_cursor.get(routing.KEY) or {}).get("channels", {})

    def event_route(self):
        from integrations.services.message_sync import discovery, event_routing

        self.connection.pk = 8
        self.connection.sync_cursor[discovery.KEY] = {
            "token": "event-lease", "expires": self.now + 120,
        }
        lease = discovery.DiscoveryLease(7, 8, 9, "event-lease",
            event_channel_id="GROOM", event_generation=self.connection.sync_cursor[PENDING][0]["key"])
        with override_settings(MESSAGE_SYNC_EVENT_ROUTING=True, MESSAGE_SYNC_TARGETED_STAGED_ROUTING=False), discovery.discovery_context(lease), patch.object(
            dm.transaction, "on_commit", side_effect=lambda callback, **_: callback(),
        ):
            event_routing.route_event_claim(self.grant, self.authority, lease)

    def test_event_lane_keeps_full_checker_and_event_path_without_old_targeted_flag(self):
        self.park(parked("GROOM", self.now - 5, kind="mpim"))
        self.sources["GROOM"] = source("GROOM")
        self.event_route()
        self.assertEqual(len(self.provider_calls), 1)
        self.discover.assert_called_once()
        self.assertNotIn("latest", self.discover.call_args.args[2])
        self.drain.assert_called_once()
        self.assertEqual(self.drain.call_args.kwargs, {"path": "event"})
        self.assertEqual(self.connection.sync_cursor[PENDING], [])

    def test_event_lane_cannot_bypass_scope_consent_connect_archive_or_membership(self):
        cases = [
            ("scope", "mpim", source("GROOM"), False, False),
            ("consent", "private_channel", source("GROOM", kind="private_channel"), False, False),
            ("connect", "mpim", source("GROOM", is_ext_shared=True), True, False),
            ("archive", "mpim", source("GROOM", is_archived=True), True, False),
            ("left", "mpim", source("GROOM", is_member=False), True, False),
            ("membership_unknown", "mpim", source("GROOM", is_member=None), True, False),
            ("provisioning_fence", "mpim", source("GROOM"), True, True),
        ]
        for reason, kind, details, requested, provisioned in cases:
            with self.subTest(reason=reason):
                self.connection.sync_cursor = {PENDING: [parked("GROOM", self.now - 5, kind=kind)]}
                self.connection.scopes = sorted(dm.DIRECT_DM_SCOPES) if reason == "scope" else ALL_SCOPES
                self.private_ok, self.live = reason != "consent", reason != "provisioning_fence"
                self.sources["GROOM"] = details
                self.provider_calls.clear()
                self.discover.reset_mock()
                self.drain.reset_mock()
                self.event_route()
                self.assertEqual(bool(self.provider_calls), requested)
                self.assertEqual(self.discover.called, provisioned)
                self.drain.assert_not_called()

    def test_flag_and_durable_sync_are_both_required_before_any_io(self):
        self.park(parked("GLIVE", self.now - 5, kind="mpim"))
        for enabled, flag in ((True, False), (False, True)):
            with self.subTest(enabled=enabled, flag=flag), override_settings(
                MESSAGE_SYNC_ENABLED=enabled, MESSAGE_SYNC_TARGETED_STAGED_ROUTING=flag,
            ):
                self.assertFalse(self.route())
        self.assertEqual(self.provider_calls, [])
        self.lock.assert_not_called()

    def test_live_group_dm_routes_after_full_source_and_membership_recheck(self):
        self.grant.conversations.filter.return_value.exists.return_value = True
        self.park(parked("GLIVE", self.now - 5, kind="mpim"), parked("GLIVE", self.now - 2, kind="mpim"))
        self.sources["GLIVE"] = source("GLIVE")
        self.assertTrue(self.route(reset_history=True))
        self.assertEqual(self.provider_calls, [("GLIVE", frozenset(dm.DIRECT_DM_SCOPES | dm.GROUP_DM_SCOPES))])
        raw = self.discover.call_args.args[2]
        self.assertNotIn("latest", raw)
        self.assertNotIn("private body", json.dumps(raw))
        self.assertTrue(raw["is_mpim"])
        kwargs = self.discover.call_args.kwargs
        self.assertEqual((kwargs["recent_activity"], kwargs["force_backfill"], kwargs["reset_history"]), (True, False, True))
        self.assertEqual(kwargs["activity_seconds"], 1899999990)
        self.assertNotIn("required_owner_public_key", kwargs)  # every verified owner device
        self.assertNotIn("check_recent_activity", kwargs)
        progress = self.progress.call_args.args
        self.assertEqual(progress[1:3], ("GLIVE", "mpim"))
        self.assertEqual(progress[3].timestamp(), self.now - 5)  # stable resume scope
        self.assertEqual(self.drain.call_args.kwargs, {"path": "targeted"})
        self.assertEqual(self.connection.sync_cursor[PENDING], [])
        self.assertNotIn(routing.KEY, self.connection.sync_cursor)

    def test_unknown_private_channel_becomes_room_on_first_message(self):
        self.park(parked("GNEW", self.now - 3, kind="private_channel"))
        self.sources["GNEW"] = source("GNEW", kind="private_channel")
        self.assertTrue(self.route())
        self.assertEqual(self.provider_calls[0][1], frozenset(dm.DIRECT_DM_SCOPES | dm.PRIVATE_CHANNEL_SCOPES))
        self.assertEqual(self.progress.call_args.args[2], "private_channel")
        self.discover.assert_called_once()
        self.drain.assert_called_once()

    def test_unknown_direct_message_uses_only_direct_scopes(self):
        self.park(parked("DNEW", self.now - 3))
        self.sources["DNEW"] = source("DNEW", kind="im", user="UCOUNTERPART")
        self.assertTrue(self.route())
        self.assertEqual(self.provider_calls, [("DNEW", frozenset(dm.DIRECT_DM_SCOPES))])
        self.assertEqual(self.discover.call_args.args[2]["user"], "UCOUNTERPART")

    def test_unreadable_type_is_discarded_without_a_revoking_provider_probe(self):
        self.private_ok = False
        self.park(parked("GPRIV", self.now - 3, kind="private_channel"))
        self.connection.scopes = list(dm.DIRECT_DM_SCOPES)
        self.park(parked("GMPIM", self.now - 2, kind="mpim"), parked("GANY", self.now - 1))
        self.assertFalse(self.route())  # no provider request: the turn continues
        self.assertEqual(self.provider_calls, [])
        self.assertEqual(self.connection.sync_cursor[PENDING], [])
        self.discover.assert_not_called()

    def test_untyped_source_with_partial_scopes_is_left_for_the_sweep(self):
        self.private_ok = False
        self.connection.scopes = GROUP_SCOPES
        self.park(parked("GANY", self.now - 3))
        self.assertFalse(self.route())
        self.assertEqual(self.provider_calls, [])
        self.assertEqual(len(self.connection.sync_cursor[PENDING]), 1)
        self.assertEqual(self.retry()["GANY"]["due"], self.now + routing.SWEEP_HANDOFF_SECONDS)
        # A catalogued group DM supplies the readable type instead.
        self.connection.sync_cursor.pop(routing.KEY)
        self.connection.provider_metadata = {"mlai_chat_conversations_v1": {"GANY": {"kind": "mpim"}}}
        self.sources["GANY"] = source("GANY")
        self.assertTrue(self.route())
        self.assertEqual(self.provider_calls, [("GANY", frozenset(dm.DIRECT_DM_SCOPES | dm.GROUP_DM_SCOPES))])

    def test_slack_connect_source_is_retired_and_parked_events_discarded(self):
        self.park(parked("GSHARED", self.now - 3, kind="mpim"))
        self.sources["GSHARED"] = source("GSHARED", is_ext_shared=True)
        self.assertTrue(self.route())
        self.assertEqual(self.retire.call_args.kwargs["reason"], dm.SLACK_CONNECT_INELIGIBLE_REASON)
        self.assertEqual(self.retire.call_args.args[1], "GSHARED")
        self.discover.assert_not_called()
        self.drain.assert_not_called()
        self.assertEqual(self.connection.sync_cursor[PENDING], [])

    def test_archived_source_respects_history_consent(self):
        self.park(parked("GOLD", self.now - 3, kind="mpim"))
        self.sources["GOLD"] = source("GOLD", is_archived=True)
        self.assertTrue(self.route())
        self.assertEqual(self.retire.call_args.kwargs["reason"], dm.SLACK_CONVERSATION_UNAVAILABLE_REASON)
        self.discover.assert_not_called()
        self.grant.consent_version = dm.ALL_HISTORY_CONSENT
        self.park(parked("GOLD", self.now - 1, kind="mpim"))
        self.assertTrue(self.route())
        self.discover.assert_called_once()

    def test_owner_outside_source_or_unavailable_source_is_never_routed(self):
        cases = {
            "GLEFT": source("GLEFT", is_member=False),
            "GPUBLIC": {"id": "GPUBLIC", "is_channel": True, "is_private": False, "is_member": True},
            "GGONE": slack_error("channel_not_found"),
            "GMISSING": source("GMISSING", is_member=None),
        }
        for index, (channel_id, value) in enumerate(cases.items()):
            self.park(parked(channel_id, self.now - 10 + index, kind="mpim"))
            self.sources[channel_id] = value
        self.assertTrue(self.route(limit=4))
        self.assertEqual([call[0] for call in self.provider_calls], list(cases))
        self.discover.assert_not_called()
        self.drain.assert_not_called()
        self.retire.assert_not_called()  # the complete sweep owns retirement
        # Unconfirmed membership delivers nothing but leaves the sweep its events.
        self.assertEqual([item["channel_id"] for item in self.connection.sync_cursor[PENDING]], ["GMISSING"])
        self.assertEqual(self.retry()["GMISSING"]["due"], self.now + routing.SWEEP_HANDOFF_SECONDS)

    def test_retired_conversation_is_revived_only_by_the_full_recheck(self):
        # A retired (paused) mirror is never drained directly. Its parked event
        # goes through the same membership/eligibility path as the sweep.
        self.grant.conversations.filter.return_value.exists.return_value = True
        self.park(parked("GRETIRED", self.now - 3, kind="mpim"))
        self.sources["GRETIRED"] = source("GRETIRED")
        self.discover.side_effect = lambda *_, **__: None  # e.g. owner left or >9 members
        self.assertTrue(self.route())
        self.drain.assert_not_called()
        self.discard.assert_called_once_with(self.authority, "GRETIRED")

    def test_fenced_room_keeps_events_parked_with_exponential_backoff(self):
        self.live = False
        self.park(parked("GFENCED", self.now - 3, kind="mpim"))
        self.sources["GFENCED"] = source("GFENCED")
        self.assertTrue(self.route())
        self.drain.assert_not_called()
        self.assertEqual(self.retry()["GFENCED"], {"due": self.now + 30, "attempts": 1})
        self.assertFalse(self.route())  # cooling down: no I/O, the sweep may run
        self.assertEqual(len(self.provider_calls), 1)
        self.now += 31
        self.assertTrue(self.route())
        self.assertEqual(self.retry()["GFENCED"], {"due": self.now + 60, "attempts": 2})
        self.live = True
        self.now += 61
        self.assertTrue(self.route())
        self.drain.assert_called_once()
        self.assertNotIn(routing.KEY, self.connection.sync_cursor)

    def test_closed_new_source_is_left_for_the_sweep_under_recent_first(self):
        self.recent_first.return_value = True
        self.park(parked("GCLOSED", self.now - 3, kind="mpim"))
        self.sources["GCLOSED"] = source("GCLOSED", is_open=False)
        self.assertTrue(self.route())
        self.discover.assert_not_called()
        self.assertEqual(self.retry()["GCLOSED"]["due"], self.now + routing.SWEEP_HANDOFF_SECONDS)

    def test_first_request_admission_deferral_returns_the_unused_turn(self):
        self.park(parked("GBUSY", self.now - 3, kind="mpim"))
        self.sources["GBUSY"] = BudgetDeferred(12, before_request_method="conversations.info")
        with self.assertRaises(BudgetDeferred) as caught:
            self.route()
        self.assertTrue(caught.exception.discovery_admission_deferred)
        self.assertEqual(self.retry()["GBUSY"]["due"], self.now + 12)
        self.assertEqual(len(self.connection.sync_cursor[PENDING]), 1)
        self.discover.assert_not_called()

    def test_nested_deferral_and_provider_429_consume_the_turn(self):
        for name, error, wait in (
            ("members", BudgetDeferred(5, before_request_method="conversations.members"), 5),
            ("provider", BudgetDeferred(60), 60),
            ("local cooldown", dm.SlackDmMirrorRateLimited("synthetic"), routing.RETRY_SECONDS),
        ):
            with self.subTest(name=name):
                self.connection.sync_cursor = {PENDING: [parked("GBUSY", self.now - 3, kind="mpim")]}
                self.sources["GBUSY"] = source("GBUSY")
                self.discover.side_effect = error
                with self.assertRaises(type(error)) as caught:
                    self.route()
                self.assertFalse(getattr(caught.exception, "discovery_admission_deferred", False))
                self.assertEqual(self.retry()["GBUSY"]["due"], self.now + wait)
                self.drain.assert_not_called()

    def test_admission_deferral_after_routed_work_is_not_an_unused_turn(self):
        self.park(parked("GFIRST", self.now - 5, kind="mpim"), parked("GSECOND", self.now - 4, kind="mpim"))
        self.sources["GFIRST"] = source("GFIRST")
        self.sources["GSECOND"] = BudgetDeferred(9, before_request_method="conversations.info")
        with self.assertRaises(BudgetDeferred) as caught:
            self.route()
        self.assertFalse(getattr(caught.exception, "discovery_admission_deferred", False))
        self.drain.assert_called_once()
        self.assertEqual(set(self.retry()), {"GSECOND"})

    def test_lease_loss_propagates_without_writing_retry_state(self):
        for stage in ("source", "provision"):
            with self.subTest(stage=stage):
                self.connection.sync_cursor = {PENDING: [parked("GLEASE", self.now - 3, kind="mpim")]}
                self.connection.save.reset_mock()
                self.sources["GLEASE"] = LeaseLost("discovery_lease_lost") if stage == "source" else source("GLEASE")
                self.discover.side_effect = LeaseLost("discovery_lease_lost") if stage == "provision" else self.provision
                with self.assertRaises(LeaseLost):
                    self.route()
                self.connection.save.assert_not_called()
                self.assertNotIn(routing.KEY, self.connection.sync_cursor)
                self.assertEqual(len(self.connection.sync_cursor[PENDING]), 1)

    def test_slack_auth_errors_propagate_for_grant_revocation(self):
        self.park(parked("GAUTH", self.now - 3, kind="mpim"))
        self.sources["GAUTH"] = slack_error("invalid_auth")
        with self.assertRaises(SlackApiError):
            self.route()
        self.discover.assert_not_called()

    def test_consent_change_stops_the_turn(self):
        self.park(parked("GONE", self.now - 5, kind="mpim"), parked("GTWO", self.now - 4, kind="mpim"))
        self.sources.update(GONE=source("GONE"), GTWO=source("GTWO"))
        self.discover.side_effect = dm.SlackDmMirrorAuthorizationError("synthetic")
        self.assertTrue(self.route())
        self.assertEqual([call[0] for call in self.provider_calls], ["GONE"])

    def test_transient_failure_backs_off_and_other_parked_channels_continue(self):
        self.park(parked("GFLAKY", self.now - 5, kind="mpim"), parked("GOK", self.now - 4, kind="mpim"))
        self.sources.update(GFLAKY=slack_error("internal_error"), GOK=source("GOK"))
        with self.assertLogs(routing.logger, "WARNING") as logs:
            self.assertTrue(self.route())
        self.assertNotIn("GFLAKY", "\n".join(logs.output))
        self.assertEqual(self.retry()["GFLAKY"]["attempts"], 1)
        self.assertEqual([item["channel_id"] for item in self.connection.sync_cursor[PENDING]], ["GFLAKY"])

    def test_turn_is_bounded_oldest_first_and_skips_cooling_channels(self):
        for index, channel_id in enumerate(("GE", "GD", "GC", "GB", "GA")):
            self.park(parked(channel_id, self.now - 10 + index, kind="mpim"))
            self.sources[channel_id] = source(channel_id)
        self.connection.sync_cursor[routing.KEY] = {"channels": {"GD": {"due": self.now + 50, "attempts": 1}}}
        self.assertTrue(self.route())
        self.assertEqual([call[0] for call in self.provider_calls], ["GE", "GC", "GB"])
        self.assertEqual({item["channel_id"] for item in self.connection.sync_cursor[PENDING]}, {"GD", "GA"})
        self.assertEqual(set(self.retry()), {"GD"})

    def test_retry_state_is_pruned_to_parked_channels_and_bounded(self):
        stale = {f"G{index}": {"due": self.now + index, "attempts": 1} for index in range(400)}
        self.connection.sync_cursor[routing.KEY] = {"channels": {**stale, "bad": {"due": "soon", "attempts": 1}}}
        self.park(*(parked(f"G{index}", self.now - 1) for index in range(300)), parked("GNEW", self.now - 1))
        routing._settle(self.authority, "GNEW", delay="backoff")
        state = self.retry()
        self.assertEqual(len(state), routing.MAX_TRACKED_CHANNELS)
        self.assertIn("GNEW", state)
        self.assertNotIn("bad", state)
        self.assertTrue(all(key in {f"G{index}" for index in range(300)} | {"GNEW"} for key in state))

    def test_malformed_parked_items_are_ignored(self):
        self.connection.sync_cursor[PENDING] = [
            "not-a-dict", {"channel_id": " GPAD", "ciphertext": "x"}, {"channel_id": "GNOBODY"},
            {"channel_id": "G" * 101, "ciphertext": "x"}, {"channel_id": "GOK", "ciphertext": "x", "staged_at": "bad"},
        ]
        self.assertEqual(set(routing.parked_channels(self.connection.sync_cursor)), {"GOK"})
        self.assertIsNone(routing.parked_channels(self.connection.sync_cursor)["GOK"]["staged_at"])
        self.assertEqual(routing.parked_channels({PENDING: {"bad": "shape"}}), {})


class StagedLatencyTests(SimpleTestCase):
    def test_latency_is_content_free_and_ignores_malformed_timestamps(self):
        with patch.object(routing.telemetry, "record_latency") as latency, self.assertLogs(routing.logger) as logs:
            routing.log_staged_latency("targeted", 7, [100.0, 98.5, None, float("nan")], now=101.0)
        self.assertEqual([call.args for call in latency.call_args_list],
                         [("staged_routed", 1000), ("staged_routed", 2500)])
        self.assertEqual(logs.output, ["INFO:integrations.services.message_sync.staged_routing:"
                                       "slack_staged_routed grant_id=7 path=targeted events=2 latency_ms=2500"])
        with patch.object(routing.telemetry, "record_latency") as latency:
            routing.log_staged_latency("targeted", 7, [None])
        latency.assert_not_called()

    def test_staged_routed_is_a_known_latency_kind(self):
        from integrations.services.message_sync import telemetry
        self.assertIn("staged_routed", telemetry.LATENCY_KINDS)
        with patch.object(telemetry, "_enqueue") as enqueue:
            telemetry.record_latency("staged_routed", 1)
        enqueue.assert_called()

    def test_drain_logs_only_enqueued_events_after_its_transaction(self):
        now = datetime.now(timezone.utc).timestamp()
        connection = SimpleNamespace(save=MagicMock(), sync_cursor={PENDING: [
            parked("GROOM", now - 4), parked("GROOM", now - 2), parked("GOTHER", now - 1),
        ]})
        grant = SimpleNamespace(pk=7)
        conversation = SimpleNamespace(pk=3, slack_conversation_id="GROOM")
        results = iter([("enqueued", "1"), ("duplicate", "")])
        order = []
        with patch.object(dm.transaction, "atomic", side_effect=lambda: nullcontext()), patch.object(
            dm, "_lock_slack_grant_api_authority", return_value=(grant, connection),
        ), patch.object(dm.SlackDmMirrorConversation, "objects") as objects, patch.object(
            dm, "decrypt_credential_value", return_value='{"source_message_id": "1.2"}',
        ), patch.object(dm, "_enqueue_normalized_slack_event_locked", side_effect=lambda *_: next(results)), patch.object(
            routing, "log_staged_latency", side_effect=lambda *args: order.append(("log", args)),
        ):
            connection.save.side_effect = lambda **_: order.append(("save",))
            objects.select_for_update.return_value.filter.return_value.first.return_value = conversation
            routed = dm._drain_staged_events_for_conversation(SimpleNamespace(grant_id=7), 3, path="targeted")
        self.assertEqual(routed, 1)
        self.assertEqual(order[0], ("save",))
        path, grant_id, stamps = order[1][1]
        self.assertEqual((path, grant_id), ("targeted", 7))
        self.assertEqual(len(stamps), 1)
        self.assertAlmostEqual(stamps[0], now - 4, places=3)
        self.assertEqual([item["channel_id"] for item in connection.sync_cursor[PENDING]], ["GOTHER"])


class StagedKindHintTests(SimpleTestCase):
    def test_ingest_passes_slack_channel_type_as_a_scope_hint(self):
        for channel_type, expected in (("mpim", "mpim"), ("group", "private_channel"), ("", None)):
            with self.subTest(channel_type=channel_type), patch.object(
                dm.SlackDmMirrorConversation, "objects",
            ) as objects, patch.object(dm, "_normalize_private_slack_event", return_value={"source_message_id": "1.2"}), patch.object(
                dm, "_stage_unknown_slack_event", return_value=1,
            ) as stage:
                objects.filter.return_value.exists.return_value = channel_type == ""
                objects.select_related.return_value.filter.return_value = []
                result = dm.ingest_slack_dm_event({
                    "team_id": "TSYNTH", "authorizations": [{"user_id": "UOWNER"}],
                    "event": {"type": "message", "channel": "GROOM", "channel_type": channel_type},
                })
            self.assertEqual(result, {"status": "discovery_queued", "staged": 1})
            self.assertEqual(stage.call_args.kwargs["kind_hint"], expected)

    def test_staged_item_records_only_known_private_kinds(self):
        connection = SimpleNamespace(sync_cursor={}, save=MagicMock())
        grant = SimpleNamespace(pk=7, last_discovery_at=1, save=MagicMock())
        candidate = SimpleNamespace(connection=connection)
        for hint, expected in (("mpim", "mpim"), ("private_channel", "private_channel"), ("im", None), (None, None)):
            connection.sync_cursor = {}
            with self.subTest(hint=hint), patch.object(dm.SlackDmMirrorGrant, "objects") as grants, patch.object(
                dm, "_capture_slack_grant_api_authority", return_value=SimpleNamespace(grant_id=7),
            ), patch.object(dm, "_lock_slack_grant_api_authority", return_value=(grant, connection)), patch.object(
                dm.transaction, "atomic", side_effect=lambda: nullcontext(),
            ), patch.object(dm, "encrypt_credential_value", return_value="opaque-ciphertext"):
                grants.select_related.return_value.filter.return_value = [candidate]
                dm._stage_unknown_slack_event("TSYNTH", "GROOM", {"source_message_id": "1.2"},
                                              authorized_user_ids={"UOWNER"}, kind_hint=hint)
            [item] = connection.sync_cursor[PENDING]
            self.assertEqual(item.get("kind"), expected)
            self.assertEqual(set(item) - {"kind"}, {"key", "channel_id", "ciphertext", "staged_at"})


@override_settings(MESSAGE_SYNC_ENABLED=True, MESSAGE_SYNC_EVENT_ROUTING=True,
                   MESSAGE_SYNC_TARGETED_STAGED_ROUTING=False)
class EventStageWakeTests(SimpleTestCase):
    """A callback must durably wake its owner without becoming a plaintext queue."""

    def setUp(self):
        from integrations.services.message_sync.discovery import KEY

        self.discovery_key = KEY
        self.now = datetime(2030, 3, 17, 12, tzinfo=timezone.utc)
        self.connection = SimpleNamespace(sync_cursor={KEY: {
            "due": self.now.timestamp() + 600, "token": "active-owner-lease",
            "expires": self.now.timestamp() + 120, "served": 42,
        }}, save=MagicMock())
        self.grant = SimpleNamespace(pk=7, connection=self.connection,
                                     last_discovery_at=self.now, save=MagicMock())
        stack = ExitStack()
        self.addCleanup(stack.close)
        grants = stack.enter_context(patch.object(dm.SlackDmMirrorGrant, "objects"))
        grants.select_related.return_value.filter.return_value = [self.grant]
        stack.enter_context(patch.object(dm, "_capture_slack_grant_api_authority",
                                         return_value=SimpleNamespace(grant_id=7)))
        stack.enter_context(patch.object(dm, "_lock_slack_grant_api_authority",
                                         return_value=(self.grant, self.connection)))
        stack.enter_context(patch.object(dm.transaction, "atomic", side_effect=lambda: nullcontext()))
        stack.enter_context(patch.object(dm.timezone, "now", return_value=self.now))
        stack.enter_context(patch.object(dm, "encrypt_credential_value", return_value="sealed-only-copy"))

    def stage(self, source_id="1900000000.000001"):
        return dm._stage_unknown_slack_event(
            "TSYNTH", "GROOM", {"source_message_id": source_id, "text": "private-test-body"},
            authorized_user_ids={"UOWNER"}, kind_hint="mpim",
        )

    def test_successful_stage_saves_ciphertext_and_wake_together_without_stealing_lease(self):
        self.assertEqual(self.stage(), 1)
        cursor = self.connection.sync_cursor
        self.assertIn("slack_event_routing_v1", cursor)
        self.assertLessEqual(cursor["slack_event_routing_v1"]["targets"]["GROOM"]["due"], self.now.timestamp())
        self.assertEqual(cursor[self.discovery_key]["due"], self.now.timestamp() + 600)
        self.assertEqual(cursor[self.discovery_key]["token"], "active-owner-lease")
        self.assertEqual(cursor[self.discovery_key]["expires"], self.now.timestamp() + 120)
        self.assertEqual(cursor[self.discovery_key]["served"], 42)
        self.assertEqual(len(cursor[PENDING]), 1)
        self.assertEqual(cursor[PENDING][0]["ciphertext"], "sealed-only-copy")
        self.assertNotIn("private-test-body", json.dumps(cursor))
        self.connection.save.assert_called_once()

    def test_duplicate_does_not_retimestamp_or_duplicate_the_encrypted_event(self):
        self.assertEqual(self.stage(), 1)
        original = json.loads(json.dumps(self.connection.sync_cursor))
        self.assertEqual(self.stage(), 0)
        self.assertEqual(self.connection.sync_cursor, original)
        self.assertEqual(len(self.connection.sync_cursor[PENDING]), 1)

    def test_event_flag_does_not_authorize_a_second_plaintext_schedule(self):
        self.stage()
        cursor = self.connection.sync_cursor
        schedule = cursor.get("slack_event_routing_v1")
        self.assertIsInstance(schedule, dict)
        self.assertNotIn("ciphertext", json.dumps(schedule))
        self.assertNotIn("private-test-body", json.dumps(schedule))
        self.assertNotIn("source_message_id", json.dumps(schedule))
        self.assertEqual([row["ciphertext"] for row in cursor[PENDING]], ["sealed-only-copy"])

    def test_event_wake_obeys_processing_pause_regardless_of_retired_flag(self):
        for durable, event in ((False, False), (False, True)):
            with self.subTest(durable=durable, event=event), override_settings(
                MESSAGE_SYNC_ENABLED=durable, MESSAGE_SYNC_EVENT_ROUTING=event,
            ):
                self.connection.sync_cursor = {}
                self.stage()
                self.assertNotIn("slack_event_routing_v1", self.connection.sync_cursor)
                self.assertEqual(len(self.connection.sync_cursor[PENDING]), 1)


@override_settings(MESSAGE_SYNC_ENABLED=True, MESSAGE_SYNC_TARGETED_STAGED_ROUTING=True)
class DiscoveryTurnOrderTests(SimpleTestCase):
    def run_turn(self, routed):
        connection = SimpleNamespace(provider="slack", status="connected", access_token="synthetic",
                                     scopes=ALL_SCOPES, sync_cursor={})
        grant = SimpleNamespace(pk=7, connection=connection, slack_workspace_id="TSYNTH", slack_user_id="UOWNER",
                                consent_version="synthetic-consent")
        authority = SimpleNamespace(grant_id=7)
        seen = []
        with ExitStack() as stack:
            stack.enter_context(patch.object(dm, "ensure_owner_identity", return_value=(None, True, None)))
            grants = stack.enter_context(patch.object(dm.SlackDmMirrorGrant, "objects"))
            grants.select_related.return_value.filter.return_value.first.return_value = grant
            stack.enter_context(patch.object(dm, "_connection_identity", return_value=("TSYNTH", "UOWNER")))
            stack.enter_context(patch.object(dm, "_capture_slack_grant_api_authority", return_value=authority))
            stack.enter_context(patch("integrations.services.slack_open_requests.process_next_open",
                                      side_effect=lambda *_: seen.append("open") and False))
            route = stack.enter_context(patch.object(
                routing, "route_staged_events", side_effect=lambda *_, **__: seen.append("staged") or routed))
            stack.enter_context(patch("integrations.services.message_sync.device_recovery.fanout_batch",
                                      side_effect=lambda: seen.append("fanout") and 0))
            stack.enter_context(patch.object(dm, "_load_discovery_checkpoint",
                                             side_effect=AssertionError("directory reached")))
            if routed:
                result = dm.discover_conversations(grant)
            else:
                with self.assertRaisesRegex(AssertionError, "directory reached"):
                    dm.discover_conversations(grant)
                result = None
        return result, seen, route

    def test_parked_channels_run_after_opens_and_before_device_or_directory_work(self):
        result, seen, route = self.run_turn(True)
        self.assertEqual((result, seen), (1, ["open", "staged"]))
        self.assertEqual(route.call_args.kwargs, {"reset_history": True})
        _, seen, _ = self.run_turn(False)
        self.assertEqual(seen, ["open", "staged", "fanout"])
