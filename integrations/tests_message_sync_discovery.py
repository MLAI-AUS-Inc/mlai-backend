"""Discovery fairness and late-worker fencing against disposable PostgreSQL."""
from contextlib import contextmanager
from datetime import timedelta
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.db import transaction
from django.test import TransactionTestCase, override_settings
from django.utils import timezone
from slack_sdk.errors import SlackApiError

from community_chat.tests.test_slack_dm_io_authority import SlackDmIoAuthorityFixture
from integrations.models import BridgeApiBudget, ExternalServiceConnection, SlackDmMirrorGrant
from integrations.services import slack_dm_mirror as dm
from integrations.services.message_sync.discovery import (
    KEY, claim_discovery, discovery_context, finish_discovery,
)
from integrations.services.message_sync.scheduler import BudgetDeferred, LeaseLost


class DiscoveryTests(SlackDmIoAuthorityFixture, TransactionTestCase):
    def make_grant(self, index, workspace):
        user = get_user_model().objects.create_user(email=f"discovery-{index}@example.invalid")
        connection = ExternalServiceConnection.objects.create(user=user, provider="slack", access_token="synthetic")
        return SlackDmMirrorGrant.objects.create(user=user, connection=connection,
            slack_workspace_id=workspace, slack_user_id=f"U{index}", consented_at=timezone.now())

    def release_due(self):
        for connection in ExternalServiceConnection.objects.all():
            value = dict(connection.sync_cursor or {})
            if KEY in value:
                value[KEY] = {**value[KEY], "due": 0}
                connection.sync_cursor = value
                connection.save(update_fields=["sync_cursor"])

    def test_incomplete_grants_rotate_across_workspaces_and_every_user(self):
        busy = [self.make_grant(i, "TIOAUTH") for i in range(12)]
        quiet = self.make_grant(99, "TQUIET")
        seen = []
        for _ in range(27):
            lease = claim_discovery(900)
            self.assertIsNotNone(lease)
            seen.append(lease.grant_id)
            finish_discovery(lease)
            self.release_due()
        self.assertEqual(seen[0:4], [self.grant.pk, quiet.pk, busy[0].pk, quiet.pk])
        self.assertEqual(set(seen), {self.grant.pk, quiet.pk, *(g.pk for g in busy)})

    def test_lease_blocks_second_claim_and_expired_worker_cannot_write_or_call(self):
        old = claim_discovery(900)
        self.assertIsNone(claim_discovery(900))
        self.connection.refresh_from_db()
        self.connection.sync_cursor[KEY]["expires"] = 0
        self.connection.save(update_fields=["sync_cursor"])
        current = claim_discovery(900)
        self.assertNotEqual(old.token, current.token)
        authority = dm._capture_slack_grant_api_authority(self.grant)
        with discovery_context(old), patch.object(dm, "WebClient") as client:
            with self.assertRaises(LeaseLost), transaction.atomic():
                dm._lock_slack_grant_api_authority(authority, required_scopes=dm.DIRECT_DM_SCOPES)
            with self.assertRaises(LeaseLost):
                dm._call_slack_with_grant_authority(authority, "users_conversations", required_scopes=dm.DIRECT_DM_SCOPES)
            client.assert_not_called()
        with self.assertRaises(LeaseLost):
            finish_discovery(old, return_turn=True)
        finish_discovery(current)

    def test_deferral_preserves_checkpoint_and_allows_another_user(self):
        other = self.make_grant(100, "TIOAUTH")
        self.connection.sync_cursor = {dm.DISCOVERY_CHECKPOINT_KEY: {"cursor": "next-page"}}
        self.connection.save(update_fields=["sync_cursor"])
        lease = claim_discovery(900)
        finish_discovery(lease, delay_seconds=60, error_code="BudgetDeferred")
        self.assertEqual(claim_discovery(900).grant_id, other.pk)
        self.connection.refresh_from_db()
        self.assertEqual(self.connection.sync_cursor[dm.DISCOVERY_CHECKPOINT_KEY], {"cursor": "next-page"})

    def test_provider_admission_delay_is_preserved_without_thirty_second_floor(self):
        from integrations.services.message_sync.discovery import discover_once

        other = self.make_grant(100, "TIOAUTH")
        before = timezone.now().timestamp()
        with patch.object(dm, "discover_conversations", side_effect=BudgetDeferred(3)):
            self.assertFalse(discover_once(300))
        self.connection.refresh_from_db()
        state = self.connection.sync_cursor[KEY]
        self.assertEqual(state["error"], "BudgetDeferred")
        self.assertGreaterEqual(state["due"], before + 3)
        self.assertLess(state["due"], before + 10)
        self.assertEqual(claim_discovery(300).grant_id, other.pk)

    def test_real_discovery_failures_keep_failure_backoff(self):
        from integrations.services.message_sync.discovery import discover_once

        before = timezone.now().timestamp()
        with patch.object(dm, "discover_conversations", side_effect=TimeoutError()):
            self.assertFalse(discover_once(300))
        self.connection.refresh_from_db()
        self.assertGreaterEqual(self.connection.sync_cursor[KEY]["due"], before + 30)

    def test_provider_retry_after_is_not_shortened(self):
        from integrations.services.message_sync.discovery import discover_once

        before = timezone.now().timestamp()
        with patch.object(dm, "discover_conversations", side_effect=BudgetDeferred(120)):
            self.assertFalse(discover_once(300))
        self.connection.refresh_from_db()
        self.assertGreaterEqual(self.connection.sync_cursor[KEY]["due"], before + 120)
        self.assertGreaterEqual(self.connection.sync_cursor[KEY]["served"], before)

    @override_settings(MESSAGE_SYNC_ENABLED=True)
    def test_actual_provider_429_keeps_its_turn_and_shared_cooldown(self):
        from integrations.services.message_sync.discovery import discover_once
        from integrations.services.message_sync.slack_client import budgeted_client

        response = Mock(status_code=429, headers={"Retry-After": "120"})
        client = Mock()
        original = client.api_call
        original.side_effect = SlackApiError("rate limited", response)
        wrapped = budgeted_client(client, workspace_id="TIOAUTH", app_id="SYNTHETIC")
        before = timezone.now().timestamp()
        with patch.object(dm, "discover_conversations", side_effect=lambda grant: wrapped.api_call("users.conversations")):
            self.assertFalse(discover_once(300))
        original.assert_called_once_with("users.conversations")
        self.connection.refresh_from_db()
        self.assertGreaterEqual(self.connection.sync_cursor[KEY]["due"], before + 120)
        self.assertGreaterEqual(self.connection.sync_cursor[KEY]["served"], before)
        budget = BridgeApiBudget.objects.get(app_id="SYNTHETIC", workspace_id="TIOAUTH", method="users.conversations")
        self.assertGreaterEqual(budget.cooldown_until.timestamp(), before + 120)

    @override_settings(MESSAGE_SYNC_ENABLED=True)
    def test_shared_budget_cannot_phase_lock_three_owners_to_one_winner(self):
        from integrations.services.message_sync import budgets

        others = [self.make_grant(index, "TIOAUTH") for index in (301, 302)]
        start = timezone.now()
        elapsed = 0
        admitted = []
        scope = dict(app_id="SYNTHETIC", workspace_id="TIOAUTH", method="users.conversations")
        BridgeApiBudget.objects.create(**scope, next_admitted_at=start - timedelta(seconds=1))
        real_budget_row = budgets.budget_row

        @contextmanager
        def clocked_budget_row(*args, **kwargs):
            # Keep real PostgreSQL locks, admission state, commits and rollback;
            # replace only the clock so a three-second budget is deterministic.
            with real_budget_row(*args, **kwargs) as (cursor, row, _):
                yield cursor, row, start + timedelta(seconds=elapsed)

        def source(grant):
            budgets.admit_request(**scope, interval_seconds=3)
            admitted.append((grant.pk, elapsed))
            # A real list page succeeded. A later method being deferred must
            # retain that turn, unlike a denied initial list admission.
            raise BudgetDeferred(2, before_request_method="conversations.info")

        with (
            patch("integrations.services.message_sync.discovery.timezone.now", side_effect=lambda: start + timedelta(seconds=elapsed)),
            patch.object(dm.time, "monotonic", side_effect=lambda: 100 + elapsed),
            patch.object(dm, "_last_grant_discovery_scan", 0.0),
            patch.object(dm, "_last_registration_cleanup_scan", 0.0),
            patch.object(dm, "_reconcile_due_registration_cleanup"),
            patch.object(dm, "discover_conversations", side_effect=source),
            patch.object(budgets, "budget_row", side_effect=clocked_budget_row),
        ):
            for elapsed in range(16):
                dm.discover_grants_if_due()
        self.assertEqual(admitted, [
            (self.grant.pk, 0), (others[0].pk, 3), (others[1].pk, 6),
            (self.grant.pk, 9), (others[0].pk, 12), (others[1].pk, 15),
        ])
        budget = BridgeApiBudget.objects.get(**scope)
        self.assertEqual(budget.next_admitted_at, start + timedelta(seconds=18))

    def test_each_discovery_turn_fetches_at_most_one_directory_list_page(self):
        old = f"{int((timezone.now() - timedelta(days=31)).timestamp())}.000001"
        page = {
            "channels": [{"id": f"DQUIET{index}", "is_im": True, "user": f"UQUIET{index}", "latest": old} for index in range(20)],
            "response_metadata": {"next_cursor": "second-page"},
        }
        with (
            patch.object(dm, "_call_slack_with_grant_authority", return_value=page) as source,
            patch.object(dm, "_reconcile_registration_cleanup"),
        ):
            dm.discover_conversations(self.grant)
        source.assert_called_once()
        self.assertEqual(source.call_args.args[1], "users_conversations")
        self.connection.refresh_from_db()
        self.assertEqual(self.connection.sync_cursor[dm.DISCOVERY_CHECKPOINT_KEY]["cursor"], "second-page")

    @override_settings(MESSAGE_SYNC_ENABLED=True)
    def test_three_due_owners_rotate_each_second_without_an_extra_five_second_wait(self):
        others = [self.make_grant(index, "TIOAUTH") for index in (201, 202)]
        start = timezone.now()
        elapsed = 0
        calls = []
        with (
            patch("integrations.services.message_sync.discovery.timezone.now", side_effect=lambda: start + timedelta(seconds=elapsed)),
            patch.object(dm.time, "monotonic", side_effect=lambda: 100 + elapsed),
            patch.object(dm, "_last_grant_discovery_scan", 0.0),
            patch.object(dm, "_last_registration_cleanup_scan", 0.0),
            patch.object(dm, "_reconcile_due_registration_cleanup") as cleanup,
            patch.object(dm, "discover_conversations", side_effect=lambda grant: calls.append((grant.pk, elapsed))),
        ):
            for elapsed in (0, 0.2, 0.99, 1, 1.99, 2, 3, 4, 5):
                dm.discover_grants_if_due()
            self.assertEqual(calls, [
                (self.grant.pk, 0), (others[0].pk, 1), (others[1].pk, 2),
                (self.grant.pk, 3), (others[0].pk, 4), (others[1].pk, 5),
            ])
            self.assertEqual(cleanup.call_count, 2)

    @override_settings(MESSAGE_SYNC_ENABLED=True)
    def test_fast_dispatch_honors_provider_due_time_and_does_not_starve_other_owner(self):
        other = self.make_grant(203, "TIOAUTH")
        start = timezone.now()
        elapsed = 0
        calls = []

        def source(grant):
            calls.append((grant.pk, elapsed))
            if grant.pk == self.grant.pk:
                raise BudgetDeferred(7)

        with (
            patch("integrations.services.message_sync.discovery.timezone.now", side_effect=lambda: start + timedelta(seconds=elapsed)),
            patch.object(dm.time, "monotonic", side_effect=lambda: 100 + elapsed),
            patch.object(dm, "_last_grant_discovery_scan", 0.0),
            patch.object(dm, "_last_registration_cleanup_scan", 0.0),
            patch.object(dm, "_reconcile_due_registration_cleanup"),
            patch.object(dm, "discover_conversations", side_effect=source),
        ):
            for elapsed in range(8):
                dm.discover_grants_if_due()
        self.assertEqual([at for grant, at in calls if grant == self.grant.pk], [0, 7])
        self.assertEqual([at for grant, at in calls if grant == other.pk], list(range(1, 7)))

    @override_settings(MESSAGE_SYNC_ENABLED=True)
    def test_idle_dispatch_is_bounded_and_waits_for_inflight_leases(self):
        start = timezone.now()
        elapsed = 0
        with (
            patch("integrations.services.message_sync.discovery.timezone.now", side_effect=lambda: start + timedelta(seconds=elapsed)),
            patch.object(dm.time, "monotonic", side_effect=lambda: 100 + elapsed),
            patch.object(dm, "_last_grant_discovery_scan", 0.0),
            patch.object(dm, "_last_registration_cleanup_scan", 0.0),
            patch.object(dm, "_reconcile_due_registration_cleanup") as cleanup,
            patch.object(dm, "discover_conversations") as source,
            patch("integrations.services.message_sync.discovery.claim_discovery", wraps=claim_discovery) as claim,
        ):
            held = claim_discovery(300, lease_seconds=120)
            self.assertIsNotNone(held)
            for elapsed in (0, 0.01, 0.2, 0.99, 1, 1.2, 2):
                dm.discover_grants_if_due()
            self.assertEqual(claim.call_count, 3)
            cleanup.assert_called_once()
            source.assert_not_called()
            finish_discovery(held)

    @override_settings(MESSAGE_SYNC_ENABLED=False)
    def test_legacy_dispatch_retains_five_second_cadence(self):
        start = timezone.now()
        elapsed = 0
        calls = []
        with (
            patch("integrations.services.message_sync.discovery.timezone.now", side_effect=lambda: start + timedelta(seconds=elapsed)),
            patch.object(dm.time, "monotonic", side_effect=lambda: 100 + elapsed),
            patch.object(dm, "_last_grant_discovery_scan", 0.0),
            patch.object(dm, "_last_registration_cleanup_scan", 0.0),
            patch.object(dm, "_reconcile_due_registration_cleanup") as cleanup,
            patch.object(dm, "discover_conversations", side_effect=lambda grant: calls.append(elapsed)),
        ):
            for elapsed in (0, 1, 2, 4.99, 5, 6, 10):
                dm.discover_grants_if_due()
            self.assertEqual(calls, [0, 5, 10])
            self.assertEqual(cleanup.call_count, 3)


    def set_pending_open(self, grant, *, served):
        from integrations.services.slack_open_requests import KEY as OPEN_KEY
        now = timezone.now().timestamp()
        grant.connection.sync_cursor = {KEY: {"served": served, "due": 0, "open_requested_at": now}, OPEN_KEY: {
            "synthetic": {"state": "pending", "until": now + 900, "requested_at": now},
        }}
        grant.connection.save(update_fields=["sync_cursor"])

    def test_pending_open_owner_claimed_before_older_served_owners(self):
        opened = self.make_grant(402, "TOPEN")
        self.set_pending_open(opened, served=timezone.now().timestamp())
        self.assertEqual(claim_discovery(300, prefer_opens=True).grant_id, opened.pk)

    def test_open_owners_round_robin(self):
        other = self.make_grant(403, self.grant.slack_workspace_id)
        now = timezone.now().timestamp()
        self.set_pending_open(self.grant, served=now - 2)
        self.set_pending_open(other, served=now - 1)
        first = claim_discovery(300, prefer_opens=True)
        self.assertEqual(first.grant_id, self.grant.pk)
        finish_discovery(first)
        self.release_due()
        self.assertEqual(claim_discovery(300, prefer_opens=True).grant_id, other.pk)

    def test_claim_preserves_and_finish_clears_marker(self):
        from integrations.services.slack_open_requests import KEY as OPEN_KEY
        self.set_pending_open(self.grant, served=0)
        stamp = self.grant.connection.sync_cursor[KEY]["open_requested_at"]
        lease = claim_discovery(300, prefer_opens=True)
        self.connection.refresh_from_db()
        self.assertEqual(self.connection.sync_cursor[KEY]["open_requested_at"], stamp)
        self.connection.sync_cursor[OPEN_KEY] = {}
        self.connection.save(update_fields=["sync_cursor"])
        finish_discovery(lease)
        self.connection.refresh_from_db()
        self.assertNotIn("open_requested_at", self.connection.sync_cursor[KEY])

    @override_settings(MESSAGE_SYNC_ENABLED=True)
    def test_discovery_precedence_open_recovery_prefetch_directory(self):
        from integrations.services import slack_open_requests, slack_owner_inventory
        for stop, expected in (("open", ["open"]), ("recovery", ["open", "recovery"]), ("prefetch", ["open", "recovery", "prefetch"]), ("directory", ["open", "recovery", "prefetch", "directory"])):
            seen = []
            def turn(name):
                seen.append(name)
                return name == stop
            with self.subTest(stop=stop), patch.object(dm, "_reconcile_registration_cleanup"), patch.object(slack_open_requests, "process_next_open", side_effect=lambda *_: turn("open")), patch("integrations.services.message_sync.device_recovery.recover_recent_conversation", side_effect=lambda *_, **__: turn("recovery")), patch.object(slack_open_requests, "prefetch_unread_source", side_effect=lambda *_, **__: turn("prefetch")), patch.object(slack_owner_inventory, "collect_private_page"), patch.object(slack_owner_inventory, "collect_public_page"), patch.object(slack_owner_inventory, "hydrate_source_names"), patch.object(dm, "_call_slack_with_grant_authority", side_effect=lambda *_, **__: (turn("directory") and {"channels": [], "response_metadata": {}})):
                dm.discover_conversations(self.grant)
            self.assertEqual(seen, expected)
