"""Strict audience protocol, durable recovery and real authority serialization."""

from datetime import timedelta
import hashlib
import threading
import time
from types import SimpleNamespace
import uuid
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import close_old_connections, connection, connections, transaction
from django.test import SimpleTestCase, TransactionTestCase, override_settings, skipUnlessDBFeature
from django.utils import timezone

from community_chat.models import CommunityChatDevice
from integrations.models import CommunityBridgeChannel, SlackChannelMembership, SlackDmMirrorGrant
from integrations.services import slack_channel_membership as membership
from integrations.services.community_bridge import buzz
from integrations.services.community_bridge import member_audiences as audiences
from integrations.tests_slack_member_access import MemberAccessFixture


ROOM = "574b53c4-8ba0-4da0-9ea9-3bfbf1e436c1"
COMMAND = "449f6f2d-fb0e-4157-a52e-6cf003cc1f16"


class MemberAudienceProtocolTests(SimpleTestCase):
    def test_hash_matches_rust_raw_sorted_unique_keys(self):
        expected = hashlib.sha256(b"mlai:bridge-channel-audience:v1:" + bytes.fromhex("a" * 64) + bytes.fromhex("b" * 64)).hexdigest()
        self.assertEqual(audiences.audience_hash(["b" * 64, "a" * 64, "a" * 64]), expected)
        self.assertEqual(audiences.audience_hash([]), hashlib.sha256(b"mlai:bridge-channel-audience:v1:").hexdigest())
        with self.assertRaises(ValueError):
            audiences.audience_hash(["A" * 64])

    def test_inspect_accepts_initial_empty_and_current_generation_only(self):
        for generation, keys in ((None, []), (COMMAND, ["a" * 64])):
            with patch.object(buzz.BuzzBridgeClient, "_post_adapter", return_value={
                "channel_id": ROOM, "generation": generation, "participant_pubkeys": keys,
            }) as submit:
                self.assertEqual(buzz.BuzzBridgeClient.inspect_member_audience(ROOM, generation=COMMAND)["generation"], generation)
                self.assertEqual(submit.call_args.args[1]["action"], "inspect")

    def test_inspect_rejects_unknown_missing_malformed_or_unfenced_reply(self):
        valid = {"channel_id": ROOM, "generation": COMMAND, "participant_pubkeys": []}
        for reply in (None, [], {**valid, "extra": True}, {"channel_id": ROOM, "participant_pubkeys": []},
                      {**valid, "channel_id": str(uuid.uuid4())}, {**valid, "generation": "bad"},
                      {**valid, "generation": None, "participant_pubkeys": ["a" * 64]},
                      {**valid, "participant_pubkeys": ["a" * 64] * 2},
                      {**valid, "participant_pubkeys": ["A" * 64]},
                      {**valid, "participant_pubkeys": [f"{n:064x}" for n in range(1001)]}):
            with self.subTest(shape=type(reply).__name__), patch.object(buzz.BuzzBridgeClient, "_post_adapter", return_value=reply):
                with self.assertRaises(buzz.BuzzBridgeError):
                    buzz.BuzzBridgeClient.inspect_member_audience(ROOM, generation=COMMAND)

    def test_update_validates_inputs_and_exact_ack_without_rebase(self):
        with patch.object(buzz.BuzzBridgeClient, "_post_adapter", return_value={"channel_id": ROOM, "generation": COMMAND}) as submit:
            buzz.BuzzBridgeClient.update_member_audience(ROOM, previous_pubkeys=[], participant_pubkeys=["a" * 64], expected_generation=None, generation=COMMAND)
            self.assertEqual(submit.call_count, 1)
        for changes in ({"generation": str(uuid.uuid4())}, {"channel_id": str(uuid.uuid4())}, {"extra": True}):
            with patch.object(buzz.BuzzBridgeClient, "_post_adapter", return_value={"channel_id": ROOM, "generation": COMMAND, **changes}):
                with self.assertRaises(buzz.BuzzBridgeError):
                    buzz.BuzzBridgeClient.update_member_audience(ROOM, previous_pubkeys=[], participant_pubkeys=[], expected_generation=None, generation=COMMAND)
        with patch.object(buzz.BuzzBridgeClient, "_post_adapter") as submit:
            with self.assertRaises(buzz.BuzzBridgeError):
                buzz.BuzzBridgeClient.update_member_audience(ROOM, previous_pubkeys=[], participant_pubkeys=["a" * 64] * 2, expected_generation=None, generation=COMMAND)
            submit.assert_not_called()

    @override_settings(BUZZ_BRIDGE_ADAPTER_URL="https://adapter.example.invalid", BUZZ_BRIDGE_ADAPTER_TOKEN="synthetic")
    def test_only_exact_update_cas_conflict_gets_retryable_typed_error(self):
        response = SimpleNamespace(status_code=409, ok=False, json=lambda: {"error": "audience_conflict"})
        with patch.object(buzz.requests, "post", return_value=response):
            with self.assertRaises(buzz.BuzzAudienceConflict):
                buzz.BuzzBridgeClient.update_member_audience(ROOM, previous_pubkeys=[], participant_pubkeys=[], expected_generation=None, generation=COMMAND)
            for path, action in ((f"v1/public-channels/{ROOM}/audience", "inspect"), ("v1/private-audience", "update"), ("v1/public-channels", "update")):
                with self.assertRaises(buzz.BuzzBridgePermanentError):
                    buzz.BuzzBridgeClient._post_adapter(path, {"action": action})


class AudienceKeySnapshotTests(SimpleTestCase):
    def setUp(self):
        cohort = patch.object(audiences, "_cohort", return_value=set())
        cohort.start()
        self.addCleanup(cohort.stop)

    @override_settings(MESSAGE_SYNC_ENABLED=False, SLACK_MEMBER_CHANNELS_ENABLED=False, SLACK_OWNER_INVENTORY_ENABLED=False)
    def test_snapshot_uses_complete_existing_consent_union_even_flags_off(self):
        grants = [SimpleNamespace(user_id=1), SimpleNamespace(user_id=2)]
        with patch.object(membership, "consenting_members", return_value=grants), patch(
            "integrations.services.slack_member_devices.device_key_result",
            return_value=({"a" * 64, "b" * 64}, ""),
        ) as devices:
            self.assertEqual(audiences.current_audience_keys("TTEST", "CROOM"), {"a" * 64, "b" * 64})
            devices.assert_called_once_with({1, 2})

    def test_invalid_or_overflow_snapshot_never_returns_a_truncated_set(self):
        for reason in ("audience_authority_unavailable", "audience_too_large"):
            with self.subTest(reason=reason), patch.object(
                membership, "consenting_members", return_value=[SimpleNamespace(user_id=1)],
            ), patch(
                "integrations.services.slack_member_devices.device_key_result", return_value=(None, reason),
            ):
                self.assertIsNone(audiences.current_audience_keys("TTEST", "CROOM"))

    def test_unbounded_cohort_does_not_load_unbounded_eligibility_snapshot(self):
        with patch.object(audiences, "_cohort", return_value=set(range(audiences.MAX_AUTHORITY_USERS + 1))), patch.object(
            membership, "consenting_members",
        ) as members:
            self.assertIsNone(audiences.current_audience_keys("TTEST", "CROOM"))
            members.assert_not_called()


class _Adapter:
    """An in-memory relay CAS, preserving fixed command content and generations."""

    def __init__(self, keys=(), generation=None):
        self.keys, self.generation = set(keys), generation
        self.lock = threading.Lock()
        self.commands, self.inspections = [], 0
        self.before_update = None

    def inspect(self, channel, *, generation):
        with self.lock:
            self.inspections += 1
            return {"channel_id": channel, "generation": self.generation, "participant_pubkeys": sorted(self.keys)}

    def apply(self, channel, **command):
        with self.lock:
            if command["expected_generation"] != self.generation or set(command["previous_pubkeys"]) != self.keys:
                raise buzz.BuzzAudienceConflict("synthetic conflict")
            self.keys, self.generation = set(command["participant_pubkeys"]), command["generation"]
            return {"channel_id": channel, "generation": self.generation}

    def update(self, channel, **command):
        self.commands.append((channel, command.copy()))
        if self.before_update:
            self.before_update(channel, command)
        return self.apply(channel, **command)


@override_settings(
    MESSAGE_SYNC_ENABLED=True, SLACK_MEMBER_CHANNELS_ENABLED=True, SLACK_OWNER_INVENTORY_ENABLED=True,
    MESSAGE_SYNC_SLACK_BOT_WORKSPACE_ID="TTEST", SLACK_MEMBER_CHANNEL_ALLOWED_IDS="",
    CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}},
)
class MemberAudienceDatabaseTests(MemberAccessFixture, TransactionTestCase):
    def setUp(self):
        super().setUp()
        self.channel.destination_channel_id = ROOM
        self.channel.room_state = {"applied": {"audience": "members"}}
        self.channel.slack_seen_at = timezone.now()
        self.channel.reader_grant = self.grant
        self.channel.enabled = False
        self.channel.save()
        membership.mark_joined(self.grant, "CROOM", observed_at=timezone.now(), public_verified=True)
        self.adapter = _Adapter()
        for method, implementation in (("inspect_member_audience", self.adapter.inspect), ("update_member_audience", self.adapter.update)):
            mocking = patch.object(buzz.BuzzBridgeClient, method, side_effect=implementation)
            mocking.start()
            self.addCleanup(mocking.stop)

    def sync(self, **kwargs):
        return audiences.sync_audience(self.channel.pk, **kwargs)

    def test_full_current_ack_enables_and_persists_only_identity_hash_generation(self):
        self.assertEqual(self.sync(), "applied")
        self.channel.refresh_from_db()
        self.assertTrue(self.channel.enabled)
        self.assertFalse(self.channel.audience_sync_pending)
        self.assertEqual(self.adapter.keys, {self.device.public_key})
        self.assertEqual(self.channel.room_state["audience_applied"], {
            "generation": str(self.channel.audience_generation), "hash": audiences.audience_hash(self.adapter.keys),
            "workspace_id": "TTEST", "channel_id": "CROOM", "destination_channel_id": ROOM,
        })
        self.assertNotIn("participant_pubkeys", repr(self.channel.room_state))
        self.provider.assert_not_called()

    def test_legacy_logical_members_actual_community_is_never_inspected_or_changed(self):
        self.channel.room_state = {"applied": {"audience": "community"}}
        self.channel.save(update_fields=("room_state",))
        self.assertEqual(self.sync(), "ignored")
        self.assertEqual(self.adapter.inspections, 0)

    def test_flagoff_and_offscope_only_reduce_current_remote_intersection(self):
        self.adapter.keys, self.adapter.generation = {self.device.public_key, "f" * 64}, str(uuid.uuid4())
        CommunityChatDevice.objects.create(user=self.grant.user, public_key="b" * 64, status="verified", verified_at=timezone.now())
        with override_settings(MESSAGE_SYNC_ENABLED=False, SLACK_MEMBER_CHANNELS_ENABLED=False,
                               SLACK_OWNER_INVENTORY_ENABLED=False, SLACK_MEMBER_CHANNEL_ALLOWED_IDS="COTHER"):
            self.assertEqual(self.sync(), "applied")
        self.assertEqual(self.adapter.keys, {self.device.public_key})
        self.channel.refresh_from_db()
        self.assertFalse(self.channel.enabled)

    def test_source_hold_empties_even_without_reader_or_freshness_and_flags(self):
        self.adapter.keys, self.adapter.generation = {self.device.public_key}, str(uuid.uuid4())
        self.channel.slack_visibility, self.channel.reader_grant, self.channel.slack_seen_at = "private", None, None
        self.channel.save()
        with override_settings(MESSAGE_SYNC_ENABLED=False, SLACK_MEMBER_CHANNELS_ENABLED=False, SLACK_OWNER_INVENTORY_ENABLED=False):
            self.assertEqual(self.sync(reduction_only=True), "applied")
        self.assertEqual(self.adapter.keys, set())

    def test_stale_source_or_reduction_turn_cannot_add_or_enable(self):
        self.adapter.keys, self.adapter.generation = {self.device.public_key}, str(uuid.uuid4())
        CommunityChatDevice.objects.create(user=self.grant.user, public_key="b" * 64, status="verified", verified_at=timezone.now())
        self.channel.slack_seen_at = timezone.now() - timedelta(minutes=6)
        self.channel.enabled = True
        self.channel.save()
        self.assertEqual(self.sync(), "applied")
        self.assertEqual(self.adapter.keys, {self.device.public_key})
        self.channel.refresh_from_db()
        self.assertFalse(self.channel.enabled)
        self.channel.slack_seen_at, self.channel.enabled = timezone.now() + timedelta(minutes=1), True
        self.channel.save()
        self.assertEqual(self.sync(), "applied")
        self.channel.refresh_from_db()
        self.assertFalse(self.channel.enabled)
        self.assertEqual(self.adapter.keys, {self.device.public_key})
        self.channel.slack_seen_at, self.channel.enabled = timezone.now(), True
        self.channel.save()
        self.assertEqual(self.sync(reduction_only=True), "applied")
        self.channel.refresh_from_db()
        self.assertFalse(self.channel.enabled)
        self.assertEqual(self.adapter.keys, {self.device.public_key})

    def test_bounded_unknown_cohort_empties_with_distinct_hold(self):
        self.adapter.keys, self.adapter.generation = {self.device.public_key}, str(uuid.uuid4())
        with patch.object(audiences, "MAX_AUTHORITY_USERS", 0):
            self.assertEqual(self.sync(), "held")
        self.channel.refresh_from_db()
        self.assertEqual(self.adapter.keys, set())
        self.assertEqual(self.channel.hold_reason, "audience_authority_unavailable")
        self.assertTrue(self.channel.audience_sync_pending)

    def test_ambiguous_remote_commit_is_recovered_by_inspection_without_resend(self):
        def committed_timeout(channel, command):
            self.adapter.apply(channel, **command)
            raise buzz.BuzzBridgeError("synthetic timeout")

        self.adapter.before_update = committed_timeout
        self.assertEqual(self.sync(), "retry")
        self.channel.refresh_from_db()
        old_attempt = self.channel.room_state["audience_attempt"]["generation"]
        self.assertTrue(self.channel.audience_sync_pending)
        self.assertFalse(self.channel.enabled)
        self.adapter.before_update = None
        self.assertEqual(self.sync(), "applied")
        self.channel.refresh_from_db()
        self.assertNotEqual(self.channel.room_state["audience_attempt"]["generation"], old_attempt)
        self.assertEqual(str(self.channel.audience_generation), old_attempt)
        self.assertEqual(len(self.adapter.commands), 1)

    def test_conflict_yields_without_rebasing_payload_and_preserves_pending(self):
        def conflict(channel, command):
            raise buzz.BuzzAudienceConflict("synthetic")

        self.adapter.before_update = conflict
        self.assertEqual(self.sync(), "conflict")
        self.assertEqual(len(self.adapter.commands), 1)
        self.channel.refresh_from_db()
        self.assertTrue(self.channel.audience_sync_pending)

    def test_existing_transaction_defers_before_any_command_or_adapter_io(self):
        with transaction.atomic():
            self.assertEqual(self.sync(), "deferred")
        self.assertEqual(self.adapter.inspections, 0)

    def test_hot_pending_room_does_not_starve_existing_room_audit(self):
        cold = CommunityBridgeChannel.objects.create(
            slack_workspace_id="TTEST", slack_channel_id="CCOLD", destination_platform="buzz",
            destination_channel_id=str(uuid.uuid4()), audience="members", audience_sync_pending=False,
            room_state={"applied": {"audience": "members"}},
        )
        CommunityBridgeChannel.objects.create(
            slack_workspace_id="TTEST", slack_channel_id="CNEWER", destination_platform="buzz",
            destination_channel_id=str(uuid.uuid4()), audience="members", audience_sync_pending=False,
            room_state={"applied": {"audience": "members"}, "audience_audited_at": timezone.now().isoformat()},
        )
        with patch.object(audiences, "sync_audience", return_value="applied") as sync:
            self.assertEqual(audiences.sync_audiences_once(limit=2), {"applied": 2})
        self.assertEqual({call.args[0] for call in sync.call_args_list}, {self.channel.pk, cold.pk})

    def test_proven_1001_key_overflow_empties_instead_of_truncating(self):
        grants = [self.grant, self.other] + [self.make_grant(number) for number in range(3, 128)]
        SlackChannelMembership.objects.bulk_create([
            SlackChannelMembership(grant=grant, slack_workspace_id="TTEST", slack_channel_id="CROOM", state="joined", source="event", observed_at=timezone.now())
            for grant in grants[1:]
        ])
        CommunityChatDevice.objects.bulk_create([
            CommunityChatDevice(user=grant.user, public_key=f"{1000 + index * 8 + offset:064x}", status="verified", verified_at=timezone.now())
            for index, grant in enumerate(grants) for offset in range(8)
        ])
        self.adapter.keys, self.adapter.generation = {self.device.public_key, "f" * 64}, str(uuid.uuid4())
        self.assertEqual(self.sync(), "held")
        self.assertEqual(self.adapter.keys, set())
        self.channel.refresh_from_db()
        self.assertEqual(self.channel.hold_reason, "audience_too_large")
        self.assertTrue(self.channel.audience_sync_pending)
        self.assertFalse(self.channel.enabled)

    @skipUnlessDBFeature("has_select_for_update")
    def test_command_uuid_is_committed_before_first_adapter_request(self):
        original = self.adapter.inspect

        def inspect(channel, **kwargs):
            other = connection.copy(alias="default")
            try:
                with other.cursor() as cursor:
                    cursor.execute("SELECT room_state->'audience_attempt'->>'generation' FROM community_bridge_channel WHERE id=%s", [self.channel.pk])
                    self.assertEqual(cursor.fetchone()[0], kwargs["generation"])
            finally:
                other.close()
            return original(channel, **kwargs)

        with patch.object(buzz.BuzzBridgeClient, "inspect_member_audience", side_effect=inspect):
            self.assertEqual(self.sync(), "applied")

    def background(self, operation):
        errors, results = [], []

        def run():
            close_old_connections()
            try:
                results.append(operation())
            except BaseException as error:
                errors.append(error)
            finally:
                # Healthy thread-local connections otherwise survive this
                # disposable test and can block PostgreSQL DROP DATABASE.
                connections.close_all()

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 10)
        return thread, errors, results

    def finish(self, handle):
        thread, errors, results = handle
        thread.join(10)
        self.assertFalse(thread.is_alive(), "Authority worker did not finish")
        if errors:
            raise errors[0]
        return results

    @skipUnlessDBFeature("has_select_for_update")
    def test_inflight_cas_serializes_revocation_then_cleanup_removes_reader(self):
        entered, release, revoker_ready = threading.Event(), threading.Event(), threading.Event()
        self.addCleanup(release.set)
        revoker_pid = []

        def block_cas(channel, command):
            if command["participant_pubkeys"]:
                entered.set()
                if not release.wait(8):
                    raise RuntimeError("Timed out waiting for test release")

        self.adapter.before_update = block_cas
        writer = self.background(self.sync)
        self.assertTrue(entered.wait(5))

        def revoke():
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_backend_pid()")
                revoker_pid.append(cursor.fetchone()[0])
            revoker_ready.set()
            with transaction.atomic():
                get_user_model().objects.select_for_update().get(pk=self.grant.user_id)
                SlackDmMirrorGrant.objects.filter(pk=self.grant.pk).update(status="paused")
            return self.sync(reduction_only=True)

        revoker = self.background(revoke)
        self.assertTrue(revoker_ready.wait(5))
        blocked = False
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            with connection.cursor() as cursor:
                cursor.execute("SELECT cardinality(pg_blocking_pids(%s))", [revoker_pid[0]])
                blocked = cursor.fetchone()[0] > 0
            if blocked:
                break
            time.sleep(0.02)
        self.assertTrue(blocked, "Revoker must wait for the held User authority fence")
        self.assertEqual(SlackDmMirrorGrant.objects.get(pk=self.grant.pk).status, "active")
        release.set()
        self.assertEqual(self.finish(writer), ["applied"])
        self.assertEqual(self.finish(revoker), ["applied"])
        self.assertEqual(self.adapter.keys, set())

    @skipUnlessDBFeature("has_select_for_update")
    def test_timed_out_fixed_command_cannot_readd_after_revocation_cleanup(self):
        delayed = []

        def timeout_before_remote_commit(channel, command):
            delayed.append((channel, command.copy()))
            raise buzz.BuzzBridgeError("synthetic timeout")

        self.adapter.before_update = timeout_before_remote_commit
        self.assertEqual(self.sync(), "retry")
        self.adapter.before_update = None

        def revoke_and_reduce():
            with transaction.atomic():
                get_user_model().objects.select_for_update().get(pk=self.grant.user_id)
                SlackDmMirrorGrant.objects.filter(pk=self.grant.pk).update(status="paused")
            return self.sync(reduction_only=True)

        self.assertEqual(self.finish(self.background(revoke_and_reduce)), ["applied"])
        old_channel, old_command = delayed[0]
        self.assertNotEqual(old_command["generation"], self.adapter.generation)
        with self.assertRaises(buzz.BuzzAudienceConflict):
            self.adapter.apply(old_channel, **old_command)
        self.assertEqual(self.adapter.keys, set())
        self.assertEqual(len(self.adapter.commands), 2)

    @skipUnlessDBFeature("has_select_for_update")
    def test_two_user_reductions_acquire_full_room_authority_in_one_order(self):
        membership.mark_joined(self.other, "CROOM", observed_at=timezone.now(), public_verified=True)
        CommunityChatDevice.objects.create(user=self.other.user, public_key="b" * 64, status="verified", verified_at=timezone.now())
        self.adapter.keys, self.adapter.generation = {self.device.public_key, "b" * 64}, str(uuid.uuid4())
        committed = threading.Barrier(2)

        def reduce(grant):
            with transaction.atomic():
                get_user_model().objects.select_for_update().get(pk=grant.user_id)
                SlackDmMirrorGrant.objects.filter(pk=grant.pk).update(status="paused")
            committed.wait(5)
            return self.sync(reduction_only=True)

        first = self.background(lambda: reduce(self.grant))
        second = self.background(lambda: reduce(self.other))
        outcomes = self.finish(first) + self.finish(second)
        self.assertIn("applied", outcomes)
        self.assertTrue(set(outcomes).issubset({"applied", "superseded"}))
        self.assertEqual(self.adapter.keys, set())

    @skipUnlessDBFeature("has_select_for_update")
    def test_new_cohort_member_after_selection_aborts_without_adapter_io(self):
        selected, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        original = audiences._lock_authority

        def pause_after_old_cohort_locks(*args, **kwargs):
            locked = original(*args, **kwargs)
            selected.set()
            if not release.wait(8):
                raise RuntimeError("Timed out waiting for cohort change")
            return locked

        with patch.object(audiences, "_lock_authority", side_effect=pause_after_old_cohort_locks):
            worker = self.background(self.sync)
            self.assertTrue(selected.wait(5))
            self.assertTrue(membership.mark_joined(self.other, "CROOM", observed_at=timezone.now(), public_verified=True))
            release.set()
            self.assertEqual(self.finish(worker), ["cohort_changed"])
        self.assertEqual(self.adapter.inspections, 0)
        self.channel.refresh_from_db()
        self.assertTrue(self.channel.audience_sync_pending)
