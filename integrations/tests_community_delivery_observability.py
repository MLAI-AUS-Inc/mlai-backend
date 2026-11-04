"""Operator-visible hold and drop reasons for Community Chat-to-Slack sends.

Pure unit tests: no database is created or migrated. Database-backed delivery
behavior stays owned by `integrations.tests_community_member_send_phase5`.
"""

import asyncio
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
import uuid

from django.test import SimpleTestCase

from integrations.models import CommunityBridgeDeliveryStatus
from integrations.services.community_bridge import community_delivery as community
from integrations.services.community_bridge import member_delivery as member
from integrations.services.community_bridge import observability, store, worker
from integrations.services.community_bridge.ai_consent import PublicBridgeConsentRequired
from integrations.services.message_sync.scheduler import BudgetDeferred


ROOM = str(uuid.uuid4())
SECRET_TEXT = "my phone is 0400 000 000"
# Operator-facing codes as published in docs/slack-community-phase5-operations.md,
# "Reading held and dropped sends". The document is the oracle, not the allowlist.
DOCUMENTED_HOLD_CODES = (
    "provider_budget_deferred", "community_bot_reader_unavailable", "community_bot_reader_scope_unavailable",
    "legacy_community_send_unproven", "community_channel_not_admitted", "community_mapping_pending",
    "community_member_access_disabled", "community_receipt_unverified", "community_source_closed",
    "member_membership_unproven", "member_delivery_deferred",
)
DOCUMENTED_DROP_CODES = ("consent_required", "device_unverified", "permanent_failure")


def _original(audience="community"):
    return SimpleNamespace(pk=7, audience=audience, destination_platform="buzz",
                           slack_workspace_id="T1", slack_channel_id="C1", destination_channel_id=ROOM)


def _delivery():
    return {"id": 42, "delivery_type": "create", "source_platform": "buzz", "target_platform": "slack",
            "source_message_id": "e" * 64, "payload": {"text": SECRET_TEXT, "source_author_id": "a" * 64}}


BOT = {"kind": "bot", "user_id": 3, "public_key": "a" * 64, "workspace_id": "T1", "slack_user_id": "UBOT"}


class HoldReasonTests(SimpleTestCase):
    def test_hold_exceptions_carry_their_own_reason(self):
        self.assertEqual(BudgetDeferred(5).reason, "provider_budget_deferred")
        self.assertEqual(member.MemberDeliveryDeferred(5).reason, "member_delivery_deferred")
        self.assertEqual(member.MemberDeliveryDeferred(5, reason="community_bot_reader_unavailable").reason,
                         "community_bot_reader_unavailable")
        self.assertEqual(community.CommunityLegacyHold().reason, "legacy_community_send_unproven")
        self.assertEqual(community.CommunityLegacyHold().retry_after, 300)
        self.assertEqual(BudgetDeferred(5, reason="member_membership_unproven").reason, "member_membership_unproven")

    def _defer(self, **kwargs):
        row = SimpleNamespace(status=CommunityBridgeDeliveryStatus.PROCESSING, attempts=2, available_at=None,
                              locked_at=object(), last_error="", lease_token="t", lease_expires_at=object(), save=Mock())
        manager = Mock()
        manager.select_for_update.return_value.get.return_value = row
        with patch.object(store.transaction, "atomic", return_value=nullcontext()), \
                patch.object(store.CommunityBridgeDelivery, "objects", manager), \
                patch.object(store, "guard_delivery"), \
                patch("integrations.services.message_sync.delivery.refund_current_delivery_turn"):
            store.defer_delivery(delivery_id=42, retry_after=60, **kwargs)
        return row

    def test_defer_delivery_records_the_real_hold_reason(self):
        row = self._defer(reason="community_bot_reader_unavailable")
        self.assertEqual(row.last_error, "community_bot_reader_unavailable")
        # Status semantics are unchanged: a held row is pending, not failed.
        self.assertEqual(row.status, CommunityBridgeDeliveryStatus.PENDING)
        self.assertEqual(row.attempts, 1)
        self.assertIsNone(row.lease_token)

    def test_defer_delivery_defaults_and_rejects_unclosed_reason(self):
        self.assertEqual(self._defer().last_error, "provider_budget_deferred")
        self.assertEqual(self._defer(reason=SECRET_TEXT).last_error, "provider_budget_deferred")
        self.assertEqual(self._defer(reason=None).last_error, "provider_budget_deferred")
        # A well-formed but unlisted code must not reach the row either.
        self.assertEqual(self._defer(reason="unknown_code").last_error, "provider_budget_deferred")
        for reason in DOCUMENTED_HOLD_CODES:
            self.assertEqual(self._defer(reason=reason).last_error, reason)

    def test_worker_passes_the_hold_reason_to_the_row(self):
        fake = SimpleNamespace(_process_delivery=AsyncMock(side_effect=community.CommunityLegacyHold()))
        defer = Mock()
        with patch.object(worker, "message_sync_enabled", return_value=False), \
                patch.object(worker, "defer_delivery", defer):
            asyncio.run(worker.CommunityBridgeDiscordClient._process_claimed_public_delivery(fake, _delivery()))
        defer.assert_called_once_with(delivery_id=42, retry_after=300, reason="legacy_community_send_unproven")


class FailureOutcomeTests(SimpleTestCase):
    def test_permanent_consent_and_device_failures_are_dropped_not_retry(self):
        cases = {
            PublicBridgeConsentRequired("Current AI sharing permission is required"): "consent_required",
            PublicBridgeConsentRequired("x", reason="device_unverified"): "device_unverified",
            PublicBridgeConsentRequired("x", reason="not-a-code"): "consent_required",
        }
        for error, reason in cases.items():
            self.assertEqual(observability.failure_outcome(error), ("dropped", reason))

    def test_other_outcomes(self):
        self.assertEqual(observability.failure_outcome(member.MemberDeliveryDeferred(60, reason="community_bot_reader_unavailable")),
                         ("deferred", "community_bot_reader_unavailable"))
        self.assertEqual(observability.failure_outcome(BudgetDeferred(1)), ("deferred", "provider_budget_deferred"))
        self.assertEqual(observability.failure_outcome(member.MemberDeliveryDeferred(60, reason="unknown_code")),
                         ("deferred", "authority_or_provider_hold"))
        self.assertEqual(observability.failure_outcome(worker.ParentMappingPending("p")), ("deferred", "parent_mapping_pending"))
        self.assertEqual(observability.failure_outcome(member.MemberDeliveryUnavailable("x")),
                         ("unavailable", "authority_or_provider_hold"))
        self.assertEqual(observability.failure_outcome(member.MemberDeliveryRetry("x")), ("retry", "authority_or_provider_hold"))

    def _logged_line(self, outcome, reason):
        info = Mock()
        connection = SimpleNamespace(connection=object(), in_atomic_block=False, autocommit=True)
        with patch.object(observability.logger, "info", info), \
                patch.object(observability, "connections", SimpleNamespace(_connections=SimpleNamespace(default=connection))):
            observability.emit("slack_member_send", mapping_id=7, workspace_id="T1", channel_id="C1", room_id=ROOM,
                               delivery_id=42, outcome=outcome, reason=reason)
        if not info.call_args_list:
            return None
        (fmt, family, body), _ = info.call_args
        return f"{family} {body}"

    def test_every_documented_code_is_written_to_the_send_log(self):
        for reason in DOCUMENTED_HOLD_CODES + ("parent_mapping_pending",):
            line = self._logged_line("deferred", reason)
            self.assertIsNotNone(line, reason)
            self.assertIn("outcome=deferred", line)
            self.assertIn(f"reason={reason}", line)
        for reason in DOCUMENTED_DROP_CODES:
            line = self._logged_line("dropped", reason)
            self.assertIsNotNone(line, reason)
            self.assertIn(f"reason={reason}", line)
        self.assertIsNone(self._logged_line("deferred", "unknown_code"))
        self.assertIsNone(self._logged_line("deferred", SECRET_TEXT))


class ExecuteObservationTests(SimpleTestCase):
    """Drive `execute` with stubbed authority so only its logging is under test."""

    def _run(self, **stubs):
        emit = Mock()
        with patch.object(community, "_stamp_identity", return_value=(_original(), {}, _delivery())), \
                patch.object(community, "admitted", return_value=True), \
                patch.object(observability, "emit", emit), \
                patch.multiple(community, **stubs):
            with self.assertRaises(Exception) as caught:
                community.execute(_delivery())
        sends = [call.kwargs for call in emit.call_args_list if call.args == ("slack_member_send",)]
        self.assertEqual(len(sends), 1)
        self.assertNotIn(SECRET_TEXT, str(emit.call_args_list))
        return caught.exception, sends[0]

    def test_choose_device_failure_is_logged_as_dropped_without_poster(self):
        error = PublicBridgeConsentRequired("Community sharing requires a current verified account device",
                                            reason="device_unverified")
        raised, fields = self._run(_choose=Mock(side_effect=error))
        self.assertIs(raised, error)  # row status still decided by the worker (dead)
        self.assertEqual(fields["outcome"], "dropped")
        self.assertEqual(fields["reason"], "device_unverified")
        self.assertEqual(fields["delivery_id"], 42)
        self.assertNotIn("poster", fields)

    def test_missing_bot_reader_is_logged_as_held_with_its_reason(self):
        raised, fields = self._run(_choose=Mock(return_value=(None, BOT)),
                                   _bot_reader=Mock(side_effect=member.MemberDeliveryDeferred(
                                       60, reason=community.BOT_READER_HOLD)))
        self.assertEqual(raised.reason, "community_bot_reader_unavailable")
        self.assertEqual((fields["outcome"], fields["reason"], fields["poster"]),
                         ("deferred", "community_bot_reader_unavailable", "bot"))

    def test_fence_consent_failure_is_logged_as_dropped_not_retry(self):
        error = PublicBridgeConsentRequired("Current AI sharing permission is required")
        raised, fields = self._run(_choose=Mock(return_value=(None, BOT)), _bot_reader=Mock(return_value=object()),
                                   _fence=Mock(side_effect=error))
        self.assertIs(raised, error)
        self.assertTrue(raised.permanent)
        self.assertEqual((fields["outcome"], fields["reason"], fields["poster"]), ("dropped", "consent_required", "bot"))


class MemberExecuteObservationTests(SimpleTestCase):
    """The member-room path records selection failures with the same observation."""

    def _run(self, enabled=True, **stubs):
        emit = Mock()
        with patch.object(member, "_stamp_identity", return_value=(_original("members"), {}, {})), \
                patch.object(member.access, "enabled", return_value=enabled), \
                patch.object(observability, "emit", emit), \
                patch.multiple(member, **stubs):
            with self.assertRaises(Exception) as caught:
                member.execute(_delivery())
        sends = [call.kwargs for call in emit.call_args_list if call.args == ("slack_member_send",)]
        self.assertEqual(len(sends), 1)
        self.assertNotIn(SECRET_TEXT, str(emit.call_args_list))
        return caught.exception, sends[0]

    def test_member_access_disabled_is_logged_as_held_with_its_code(self):
        raised, fields = self._run(enabled=False, _choose=Mock(side_effect=AssertionError("selection must not run")))
        self.assertEqual(raised.retry_after, 60)
        self.assertEqual((fields["outcome"], fields["reason"], fields["delivery_id"]),
                         ("deferred", "community_member_access_disabled", 42))
        self.assertNotIn("poster", fields)

    def test_member_selection_failures_are_logged_without_poster(self):
        cases = (
            (worker.ParentMappingPending("p"), ("deferred", "parent_mapping_pending")),
            (member.MemberDeliveryUnavailable("not_a_slack_member"), ("unavailable", "authority_or_provider_hold")),
            (BudgetDeferred(60, reason="member_membership_unproven"), ("deferred", "member_membership_unproven")),
        )
        for error, expected in cases:
            raised, fields = self._run(_choose=Mock(side_effect=error))
            self.assertIs(raised, error)
            self.assertEqual((fields["outcome"], fields["reason"]), expected)
            self.assertNotIn("poster", fields)
