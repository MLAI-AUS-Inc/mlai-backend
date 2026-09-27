"""No-database regressions for due reminders and independent channel delivery."""
from contextlib import nullcontext
from dataclasses import replace
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from django.test import SimpleTestCase, override_settings
from slack_sdk.errors import SlackApiError

from startup_updates import monthly_update_reminders as reminders
from startup_updates import roo_update_reminders as chat
from startup_updates.models import MonthlyUpdateReminderKind

UTC = ZoneInfo("UTC")
MELBOURNE = ZoneInfo("Australia/Melbourne")


def target(**changes):
    result = reminders.MonthlyUpdateReminderTarget(
        user_id=1, recipient_email="founder@example.test", first_name="Sam",
        organization_id=10, organization_name="MLAI", company_id="09ea238a-c6b5-4b66-96d9-80a162ac8e43",
        company_name="MLAI", domain="example.test", source_update_id=101,
        ready_date=date(2026, 7, 1), valid_through=date(2026, 7, 31),
        expires_on=date(2026, 7, 31),
        expires_at=datetime(2026, 7, 31, 12, tzinfo=MELBOURNE),
        reminder_kind=MonthlyUpdateReminderKind.ONE_DAY, reminder_date=date(2026, 7, 30),
        update_url="https://example.test/update",
    )
    return replace(result, **changes)


@override_settings(COMMUNITY_CHAT_FRONTEND_URL="https://chat.mlai.au")
class RooUpdateReminderTests(SimpleTestCase):
    def setUp(self):
        self.target = target()
        self.now = datetime(2026, 7, 30, 9, tzinfo=MELBOURNE)
        self.delivery = SimpleNamespace(pk=1, provider_response={}, save=Mock())
        self.ledger = Mock()
        self.ledger.get_or_create.return_value = self.delivery, True
        self.ledger.select_for_update.return_value.get.return_value = self.delivery
        self.grant = SimpleNamespace(pk=5, slack_user_id="UFOUNDER")
        self.client = Mock()
        self.client.auth_test.return_value = {"ok": True, "team_id": "TMLAI", "user_id": "UROO"}
        self.client.conversations_open.return_value = {"ok": True, "channel": {"id": "DROO"}}
        self.client.chat_postMessage.return_value = {"ok": True, "ts": "100.123"}
        self._real_client = chat._roo_client
        patches = [
            patch.object(chat.MonthlyUpdateReminderDelivery, "objects", self.ledger),
            patch.object(chat.transaction, "atomic", side_effect=lambda: nullcontext()),
            patch.object(chat, "public_roo_target", return_value=("TMLAI", "UROO")),
            patch.object(chat, "_active_grant", return_value=self.grant),
            patch.object(chat, "_roo_client", return_value=self.client),
            patch.object(chat, "_fresh_targets", return_value=[self.target]),
        ]
        self.mocks = [item.start() for item in patches]
        for item in patches:
            self.addCleanup(item.stop)

    def send(self, now=None):
        return chat.dispatch_roo_reminder([self.target], now=now or self.now)

    def test_transport_disables_uncontrolled_sdk_retries(self):
        with (
            patch.object(chat.SlackService, "get_client", return_value=SimpleNamespace(token="synthetic")),
            patch.object(chat, "WebClient") as client,
        ):
            self._real_client()
        client.assert_called_once_with(token="synthetic", timeout=30, retry_handlers=[])

    def test_due_dm_is_once_and_company_scoped(self):
        self.assertEqual(self.send()["status"], "sent")
        self.assertEqual(self.send()["reason"], "already_sent")
        self.client.chat_postMessage.assert_called_once()
        message = self.client.chat_postMessage.call_args.kwargs
        self.assertEqual(message["channel"], "DROO")
        self.assertIn("31 July 2026 at 12:00 PM AEST", message["text"])
        self.assertIn("https://chat.mlai.au/pulse?startup=09ea238a-c6b5-4b66-96d9-80a162ac8e43&startupView=new", message["text"])
        self.assertIn("20 Roo points once per startup/month", message["text"])
        self.assertIn("4 points instead of 8", message["text"])
        self.assertEqual(self.delivery.provider_response["roo_chat"]["message_ts"], "100.123")

    def test_chat_preserves_existing_email_receipt(self):
        self.delivery.provider_response = {"delivery_id": "email-1"}
        self.send()
        self.assertEqual(self.delivery.provider_response["delivery_id"], "email-1")

    def test_requires_active_owner_mirror_connection(self):
        self.mocks[3].return_value = None
        self.assertEqual(self.send()["reason"], "no_active_chat_connection")
        self.ledger.get_or_create.assert_not_called()
        self.client.auth_test.assert_not_called()

    def test_revoking_grant_between_preflight_and_send_suppresses(self):
        self.mocks[3].side_effect = [self.grant, None]
        self.assertEqual(self.send()["status"], "suppressed")
        self.client.chat_postMessage.assert_not_called()

    def test_wrong_bot_identity_cannot_send_and_retries_with_backoff(self):
        self.client.auth_test.return_value["user_id"] = "UOTHER"
        self.assertEqual(self.send()["reason"], "public_roo_identity_mismatch")
        self.assertEqual(self.send()["reason"], "retry_backoff")
        self.client.auth_test.return_value["user_id"] = "UROO"
        self.assertEqual(self.send(self.now + timedelta(minutes=1))["status"], "sent")
        self.client.chat_postMessage.assert_called_once()

    def test_safe_preflight_failure_retries_and_is_bounded(self):
        self.client.conversations_open.side_effect = TimeoutError()
        self.assertEqual(self.send()["status"], "retry")
        for minute in (1, 3, 7, 15):
            self.assertEqual(self.send(self.now + timedelta(minutes=minute))["status"], "retry")
        self.assertEqual(self.send(self.now + timedelta(hours=1))["reason"], "retry_exhausted")
        self.client.chat_postMessage.assert_not_called()

    def test_ambiguous_post_failure_is_quarantined(self):
        self.client.chat_postMessage.side_effect = TimeoutError()
        self.assertEqual(self.send()["status"], "unknown")
        self.assertEqual(self.send(self.now + timedelta(hours=1))["reason"], "already_unknown")
        self.client.chat_postMessage.assert_called_once()

    def test_known_rate_limit_retries_with_same_message_id(self):
        self.client.chat_postMessage.side_effect = [
            SlackApiError("rate limited", {"error": "ratelimited"}),
            {"ok": True, "ts": "100.124"},
        ]
        self.assertEqual(self.send()["status"], "retry")
        self.assertEqual(self.send(self.now + timedelta(minutes=1))["status"], "sent")
        calls = self.client.chat_postMessage.call_args_list
        self.assertEqual(calls[0].kwargs["client_msg_id"], calls[1].kwargs["client_msg_id"])

    def test_new_update_before_dispatch_suppresses_old_cycle(self):
        self.mocks[5].return_value = []
        self.assertEqual(self.send()["status"], "suppressed")
        self.client.chat_postMessage.assert_not_called()

    def test_preparing_lease_can_recover_but_sending_cannot_repost(self):
        self.delivery.provider_response["roo_chat"] = {
            "status": "preparing", "attempt_count": 1, "claim": "crashed-worker",
            "retry_at": (self.now - timedelta(seconds=1)).isoformat(),
        }
        self.assertEqual(self.send()["status"], "sent")
        self.delivery.provider_response["roo_chat"]["status"] = "sending"
        self.assertEqual(self.send()["reason"], "already_sending")
        self.client.chat_postMessage.assert_called_once()

    def test_replaced_claim_cannot_post(self):
        def replace_claim(*args):
            self.delivery.provider_response["roo_chat"]["claim"] = "other-worker"
            return [self.target]
        self.mocks[5].side_effect = replace_claim
        self.assertEqual(self.send()["reason"], "claim_replaced")
        self.client.chat_postMessage.assert_not_called()

    def test_seven_day_email_does_not_send_chat(self):
        self.target = replace(self.target, reminder_kind=MonthlyUpdateReminderKind.SEVEN_DAY)
        self.assertEqual(self.send()["reason"], "not_due_tomorrow")
        self.client.chat_postMessage.assert_not_called()

    def test_company_markup_cannot_ping_slack_members(self):
        message = chat._message([replace(self.target, company_name="<!channel> & Co")])
        self.assertIn("&lt;!channel&gt; &amp; Co", message)
        self.assertNotIn("<!channel>", message)


@override_settings(
    MONTHLY_UPDATE_REMINDERS_ENABLED=False,
    MONTHLY_UPDATE_ROO_REMINDERS_ENABLED=True,
    MONTHLY_UPDATE_REMINDER_TIMEZONE="Australia/Melbourne",
    MONTHLY_UPDATE_REMINDER_HOUR=9,
    MONTHLY_UPDATE_REMINDER_MINUTE=0,
    CUSTOMERIO_API_KEY="", CUSTOMERIO_MONTHLY_UPDATE_1D_TEMPLATE_ID="",
)
class ReminderSchedulerUnitTests(SimpleTestCase):
    def test_chat_does_not_require_customerio_config(self):
        with (
            patch.object(reminders, "collect_monthly_update_reminder_targets", return_value=[target()]),
            patch.object(chat, "dispatch_roo_reminder", return_value={"status": "sent"}) as send,
            patch.object(reminders, "_dispatch_group") as email,
        ):
            result = reminders.run_monthly_update_reminder_scheduler(now=datetime(2026, 7, 30, 9, tzinfo=MELBOURNE))
        self.assertEqual(result["chat_outcomes"], [{"status": "sent"}])
        send.assert_called_once()
        email.assert_not_called()

    def test_preview_and_before_schedule_never_send(self):
        with (
            patch.object(reminders, "collect_monthly_update_reminder_targets", return_value=[target()]),
            patch.object(chat, "dispatch_roo_reminder") as send,
        ):
            now = datetime(2026, 7, 30, 8, tzinfo=MELBOURNE)
            self.assertEqual(reminders.run_monthly_update_reminder_scheduler(now=now)["reason"], "before_schedule_window")
            self.assertEqual(reminders.run_monthly_update_reminder_scheduler(now=now, dry_run=True)["status"], "dry_run")
        send.assert_not_called()


@override_settings(MONTHLY_UPDATE_REMINDER_TIMEZONE="Australia/Melbourne")
class ReminderEligibilityUnitTests(SimpleTestCase):
    def setUp(self):
        self.user = SimpleNamespace(pk=1, email="founder@example.test", first_name="Sam")
        self.company = SimpleNamespace(
            pk="mlai", name="MLAI", domain="mlai.example", registered=True,
            abn="51824753556", acn="", entity_type_code="OIE", abr_verified_at=datetime(2026, 7, 1, tzinfo=UTC),
            profile=SimpleNamespace(user_id=1, user=self.user), organization_id=10,
            organization=SimpleNamespace(name="MLAI", domain="mlai.example"),
        )
        self.update = SimpleNamespace(
            pk=101, organization_id=10, month=date(2026, 7, 1),
            ready_at=datetime(2026, 7, 1, 2, tzinfo=UTC),
            published_at=datetime(2026, 7, 1, 2, tzinfo=UTC), first_approved_at=None,
        )
        self.companies = Mock()
        self.companies.filter.return_value.exclude.return_value.select_related.return_value.order_by.return_value = [self.company]
        self.bindings = Mock()
        self.bindings.filter.return_value.values_list.return_value = [(1, 10)]
        self.updates = Mock()
        self.updates.filter.return_value.annotate.return_value.order_by.return_value = [self.update]
        self.history = Mock()
        self.history.exclude.return_value.first.return_value = None
        history_patch = patch("startup_updates.benefits.monthly_reward_history", return_value=self.history)
        history_patch.start()
        self.addCleanup(history_patch.stop)
        for model, manager in (
            (reminders.VibeRaisingCompany, self.companies),
            (reminders.UserStartupBinding, self.bindings),
            (reminders.MonthlyUpdateDraft, self.updates),
        ):
            patcher = patch.object(model, "objects", manager)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_abn_only_nonprofit_receives_thirty_day_approved_reminder(self):
        result = reminders.collect_monthly_update_reminder_targets(date(2026, 7, 30))
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].expires_at, datetime(2026, 7, 31, 12, tzinfo=MELBOURNE))
        self.assertEqual(result[0].reminder_kind, MonthlyUpdateReminderKind.ONE_DAY)
        query = self.updates.filter.call_args.kwargs
        self.assertEqual(query["published_at__isnull"], False)
        self.assertNotIn("status", query)
        self.assertEqual(reminders.collect_monthly_update_reminder_targets(date(2026, 7, 29)), [])
        self.assertEqual(reminders.collect_monthly_update_reminder_targets(date(2026, 7, 31)), [])

    def test_melbourne_date_and_dst_use_exact_elapsed_thirty_days(self):
        self.update.ready_at = datetime(2026, 9, 10, 15, tzinfo=UTC)
        result = reminders.collect_monthly_update_reminder_targets(date(2026, 10, 10))
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].ready_date, date(2026, 9, 11))
        self.assertEqual(result[0].expires_at, datetime(2026, 10, 11, 2, tzinfo=MELBOURNE))

    def test_unverified_or_wrong_owner_is_not_eligible(self):
        self.company.abr_verified_at = None
        self.assertEqual(reminders.collect_monthly_update_reminder_targets(date(2026, 7, 30)), [])
        self.company.abr_verified_at = datetime(2026, 7, 1, tzinfo=UTC)
        self.bindings.filter.return_value.values_list.return_value = [(2, 10)]
        self.assertEqual(reminders.collect_monthly_update_reminder_targets(date(2026, 7, 30)), [])

    def test_latest_approved_update_renews_reminder_cycle(self):
        newer = SimpleNamespace(
            pk=102, organization_id=10, month=date(2026, 8, 1),
            ready_at=datetime(2026, 7, 20, 2, tzinfo=UTC),
            published_at=datetime(2026, 7, 20, 2, tzinfo=UTC), first_approved_at=None,
        )
        self.updates.filter.return_value.annotate.return_value.order_by.return_value = [newer, self.update]
        self.assertEqual(reminders.collect_monthly_update_reminder_targets(date(2026, 7, 30)), [])
        result = reminders.collect_monthly_update_reminder_targets(date(2026, 8, 18))
        self.assertEqual(result[0].source_update_id, 102)


    def test_immutable_approval_overrides_earlier_generation_timestamp(self):
        self.update.first_approved_at = datetime(2026, 7, 10, 2, tzinfo=UTC)
        self.update.published_at = self.update.first_approved_at
        self.assertEqual(reminders.collect_monthly_update_reminder_targets(date(2026, 7, 30)), [])
        result = reminders.collect_monthly_update_reminder_targets(date(2026, 8, 8))
        self.assertEqual(result[0].expires_at, datetime(2026, 8, 9, 12, tzinfo=MELBOURNE))
        self.assertEqual(result[0].ready_date, date(2026, 7, 10))

    def test_recreated_month_retains_original_paid_window(self):
        self.update.ready_at = datetime(2026, 7, 20, 2, tzinfo=UTC)
        self.update.first_approved_at = self.update.ready_at
        self.update.published_at = self.update.ready_at
        self.history.exclude.return_value.first.return_value = SimpleNamespace(
            created_at=datetime(2026, 7, 1, 2, tzinfo=UTC),
        )
        result = reminders.collect_monthly_update_reminder_targets(date(2026, 7, 30))
        self.assertEqual(result[0].expires_at, datetime(2026, 7, 31, 12, tzinfo=MELBOURNE))
        self.assertEqual(reminders.collect_monthly_update_reminder_targets(date(2026, 8, 18)), [])
        self.history.exclude.assert_called_with(reference_type="MONTHLY_UPDATE_DRAFT", reference_id="101")

    def test_latest_cycle_uses_resolved_approval_not_draft_id_or_generated_stamp(self):
        self.update.first_approved_at = datetime(2026, 7, 21, 2, tzinfo=UTC)
        newer = SimpleNamespace(
            pk=102, organization_id=10, month=date(2026, 8, 1),
            ready_at=datetime(2026, 7, 20, 2, tzinfo=UTC),
            published_at=datetime(2026, 7, 20, 2, tzinfo=UTC), first_approved_at=None,
        )
        self.updates.filter.return_value.annotate.return_value.order_by.return_value = [newer, self.update]
        self.assertEqual(reminders.collect_monthly_update_reminder_targets(date(2026, 8, 18)), [])
        result = reminders.collect_monthly_update_reminder_targets(date(2026, 8, 19))
        self.assertEqual(result[0].source_update_id, 101)

    def test_date_only_compatibility_value_handles_partial_and_midnight_expiry(self):
        result = reminders.collect_monthly_update_reminder_targets(date(2026, 7, 30))
        self.assertEqual(result[0].valid_through, date(2026, 7, 31))
        self.update.first_approved_at = datetime(2026, 7, 1, 14, tzinfo=UTC)
        result = reminders.collect_monthly_update_reminder_targets(date(2026, 7, 31))
        self.assertEqual(result[0].expires_at, datetime(2026, 8, 1, tzinfo=MELBOURNE))
        self.assertEqual(result[0].valid_through, date(2026, 7, 31))

    def test_immutable_approval_does_not_require_legacy_ready_stamp(self):
        self.update.ready_at = None
        self.update.first_approved_at = datetime(2026, 7, 1, 2, tzinfo=UTC)
        result = reminders.collect_monthly_update_reminder_targets(date(2026, 7, 30))
        self.assertEqual(len(result), 1)
        self.assertNotIn("ready_at__isnull", self.updates.filter.call_args.kwargs)


@override_settings(
    CUSTOMERIO_MONTHLY_UPDATE_1D_TEMPLATE_ID="synthetic-one-day",
    MONTHLY_UPDATE_REMINDERS_QUEUE_DRAFT=False,
)
class ReminderChannelMergeTests(SimpleTestCase):
    def test_email_completion_reads_latest_locked_chat_receipt(self):
        initial = SimpleNamespace(
            pk=1, status="pending", provider_response={}, attempt_count=0, save=Mock(),
        )
        chat_receipt = {"status": "sent", "claim": "chat-worker", "message_ts": "100.123"}
        latest = SimpleNamespace(pk=1, provider_response={"roo_chat": chat_receipt}, save=Mock())
        ledger = Mock()
        ledger.get_or_create.return_value = initial, True
        # The email network call releases the lock. Another worker completes
        # the chat delivery before email reacquires it to persist its response.
        ledger.select_for_update.return_value.get.side_effect = [initial, latest]
        client = Mock()
        client.send_email.return_value = {
            "delivery_id": "email-receipt",
            "roo_chat": {"status": "provider-must-not-override-our-state"},
        }
        with (
            patch.object(reminders.MonthlyUpdateReminderDelivery, "objects", ledger),
            patch.object(reminders.transaction, "atomic", side_effect=lambda: nullcontext()),
            patch.object(reminders, "collect_monthly_update_reminder_targets", return_value=[target()]),
            patch.object(reminders, "_customerio_client", return_value=client),
        ):
            outcome = reminders._dispatch_group([target()])
        self.assertEqual(outcome["status"], "sent")
        self.assertEqual(latest.provider_response["roo_chat"], chat_receipt)
        self.assertEqual(latest.provider_response["delivery_id"], "email-receipt")
        self.assertEqual(ledger.select_for_update.return_value.get.call_count, 2)

    def test_chat_completion_preserves_email_receipt_written_during_post(self):
        latest = SimpleNamespace(
            pk=1, save=Mock(), provider_response={
                "delivery_id": "concurrent-email-receipt",
                "roo_chat": {"claim": "chat-worker", "status": "sending"},
            },
        )
        ledger = Mock()
        ledger.select_for_update.return_value.get.return_value = latest
        with (
            patch.object(chat.MonthlyUpdateReminderDelivery, "objects", ledger),
            patch.object(chat.transaction, "atomic", side_effect=lambda: nullcontext()),
        ):
            self.assertTrue(chat._set_state(1, "chat-worker", "sent", message_ts="100.123"))
        self.assertEqual(latest.provider_response["delivery_id"], "concurrent-email-receipt")
        self.assertEqual(latest.provider_response["roo_chat"]["status"], "sent")
