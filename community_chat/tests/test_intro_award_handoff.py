"""One canonical introduction across pending native requests and Slack awards."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone as datetime_timezone
from threading import Barrier

from django.contrib.auth import get_user_model
from django.db import close_old_connections, connection, connections
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from community_chat.models import CommunityChatDevice
from community_chat.volunteer.models import VolunteerRecognition
from community_chat.volunteer.access import VolunteerError
from community_chat.volunteer.receipts import ingest_receipt, persist_receipt, process_receipt
from community_chat.volunteer.services import request_recognition
from roo.models import ChannelFirstPost, Ledger
from roo.services import PointsService
from roo.views import award_first_channel_post_bonus


INTRO_SETTINGS = dict(
    COMMUNITY_CHAT_VOLUNTEER_ENABLED=True,
    COMMUNITY_CHAT_VOLUNTEER_RECOGNITION_ENABLED=True,
    COMMUNITY_CHAT_VOLUNTEER_AWARDS_ENABLED=False,
    COMMUNITY_CHAT_VOLUNTEER_INTRO_REWARDS_ENABLED=True,
    COMMUNITY_CHAT_VOLUNTEER_INTRO_ACTIVE_FROM="2020-01-01T00:00:00Z",
    COMMUNITY_CHAT_VOLUNTEER_BONUSES_ENABLED=False,
    COMMUNITY_CHAT_VOLUNTEER_COMMUNITY="synthetic-intro-handoff",
    COMMUNITY_CHAT_VOLUNTEER_CHANNELS={"start_here": "start"},
)


class IntroFixture:
    def setUp(self):
        self.user = get_user_model().objects.create_user(email="intro-handoff@example.test", slack_id="UINTRO")
        self.device = CommunityChatDevice.objects.create(user=self.user, public_key="a" * 64,
            status="verified", verified_at=timezone.now())
        self.payload = {
            "source_key": "native-intro", "origin": "relay", "kind": "post", "actor_public_key": "a" * 64,
            "source": {"source_id": "native-intro", "message_id": "native-intro", "channel_id": "start"},
            "occurred_at": timezone.now().isoformat(),
            "metadata": {"original": True, "top_level": True, "has_text": True},
        }

    def pending(self):
        receipt = persist_receipt(self.payload)
        record, _ = request_recognition(self.user, {
            "action_key": "introduce_yourself", "source": receipt.source, "note": "My original introduction",
        }, trusted_receipt=receipt)
        return receipt, record


@override_settings(**INTRO_SETTINGS)
class PendingIntroHandoffTests(IntroFixture, TestCase):
    def test_delayed_original_intro_cannot_credit_a_new_owner_of_the_key(self):
        now = timezone.now()
        CommunityChatDevice.objects.filter(pk=self.device.pk).update(
            created_at=now - timedelta(seconds=40), verified_at=now - timedelta(seconds=35),
            status="revoked", revoked_at=now - timedelta(seconds=10),
        )
        other = get_user_model().objects.create_user(email="reenrolled-intro@example.test")
        replacement = CommunityChatDevice.objects.create(user=other, public_key="a" * 64,
            status="verified", verified_at=now - timedelta(seconds=5))
        CommunityChatDevice.objects.filter(pk=replacement.pk).update(created_at=now - timedelta(seconds=6))
        self.payload["occurred_at"] = (now - timedelta(seconds=20)).isoformat()
        with self.assertRaisesMessage(VolunteerError, "member_unavailable"):
            ingest_receipt(self.payload)
        self.assertFalse(Ledger.objects.filter(user__in=(self.user, other)).exists())
        self.assertFalse(VolunteerRecognition.objects.filter(user__in=(self.user, other)).exists())

    def test_intro_enrollment_within_the_source_second_is_supported(self):
        second = int(timezone.now().timestamp()) - 100
        start = datetime.fromtimestamp(second, tz=datetime_timezone.utc)
        CommunityChatDevice.objects.filter(pk=self.device.pk).update(
            created_at=start + timedelta(milliseconds=100), verified_at=start + timedelta(milliseconds=200),
        )
        self.payload["occurred_at"] = start.isoformat()
        self.assertEqual(ingest_receipt(self.payload).status, "processed")
        self.assertEqual(PointsService.get_balance(self.user)["balance"], 4)

    def test_ambiguous_two_owner_source_second_cannot_credit_either_account(self):
        second = int(timezone.now().timestamp()) - 100
        start = datetime.fromtimestamp(second, tz=datetime_timezone.utc)
        CommunityChatDevice.objects.filter(pk=self.device.pk).update(
            created_at=start - timedelta(seconds=10), verified_at=start - timedelta(seconds=9),
            status="revoked", revoked_at=start + timedelta(milliseconds=500),
        )
        other = get_user_model().objects.create_user(email="ambiguous-intro@example.test")
        replacement = CommunityChatDevice.objects.create(user=other, public_key="a" * 64,
            status="verified", verified_at=start + timedelta(milliseconds=800))
        CommunityChatDevice.objects.filter(pk=replacement.pk).update(created_at=start + timedelta(milliseconds=700))
        self.payload["occurred_at"] = start.isoformat()
        with self.assertRaisesMessage(VolunteerError, "member_unavailable"):
            ingest_receipt(self.payload)
        self.assertFalse(Ledger.objects.filter(user__in=(self.user, other)).exists())

    def test_intro_without_historical_verification_is_not_silently_accepted(self):
        CommunityChatDevice.objects.filter(pk=self.device.pk).update(verified_at=None)
        with self.assertRaisesMessage(VolunteerError, "member_unavailable"):
            ingest_receipt(self.payload)
        self.assertFalse(Ledger.objects.exists())

    def test_slack_award_completes_the_existing_pending_outcome_and_native_retry(self):
        receipt, record = self.pending()
        record.review_history = [{"decision": "submitted", "note": "Original request retained"}]
        record.save(update_fields=("review_history",))
        original_source, original_version = record.source, record.version
        awarded, balance = award_first_channel_post_bonus("UINTRO", "CSTART")
        self.assertTrue(awarded)
        self.assertEqual(balance, 4)
        record.refresh_from_db()
        self.assertEqual(record.status, "approved")
        self.assertEqual(record.source, original_source)
        self.assertEqual(record.version, original_version + 1)
        self.assertEqual(record.review_history[0]["note"], "Original request retained")
        self.assertTrue(record.review_history[-1]["automatic"])
        self.assertEqual(record.review_history[-1]["decision"], "approve")
        self.assertEqual(record.ledger.reference_type, "FIRST_CHANNEL_POST")
        retried = process_receipt(receipt)
        self.assertEqual(retried.status, "processed", retried.error)
        self.assertEqual(retried.recognition_id, record.pk)
        self.assertEqual(VolunteerRecognition.objects.filter(user=self.user).count(), 1)
        self.assertEqual(Ledger.objects.filter(user=self.user).count(), 1)
        self.assertFalse(award_first_channel_post_bonus("UINTRO", "CSTART")[0])

    def test_slack_automation_preserves_reviewed_or_withdrawn_native_outcomes(self):
        _, record = self.pending()
        for status in ("approved", "reversed", "not_approved", "needs_update", "withdrawn"):
            with self.subTest(status=status):
                ChannelFirstPost.objects.all().delete()
                record.status = status
                record.save(update_fields=("status",))
                version, history = record.version, record.review_history
                self.assertEqual(award_first_channel_post_bonus("UINTRO", "CSTART"), (False, None))
                record.refresh_from_db()
                self.assertEqual(record.status, status)
                self.assertEqual(record.version, version)
                self.assertEqual(record.review_history, history)
                self.assertFalse(Ledger.objects.filter(user=self.user).exists())

    def test_native_automation_preserves_reviewed_withdrawn_or_corrected_outcomes(self):
        receipt, record = self.pending()
        for status in ("needs_update", "not_approved", "withdrawn", "reversed"):
            with self.subTest(status=status):
                receipt.status, receipt.error = "pending", ""
                receipt.save(update_fields=("status", "error"))
                record.status = status
                record.save(update_fields=("status",))
                version, history = record.version, record.review_history
                result = process_receipt(receipt)
                self.assertEqual(result.status, "recorded")
                self.assertEqual(result.error, "recognition_review_preserved")
                self.assertEqual(result.recognition_id, record.pk)
                record.refresh_from_db()
                self.assertEqual(record.status, status)
                self.assertEqual(record.version, version)
                self.assertEqual(record.review_history, history)
                self.assertFalse(Ledger.objects.filter(user=self.user).exists())


@override_settings(**INTRO_SETTINGS)
class ConcurrentIntroHandoffTests(IntroFixture, TransactionTestCase):
    def race(self):
        if connection.vendor != "postgresql":
            self.skipTest("Introduction lock serialization requires PostgreSQL")
        barrier = Barrier(2)

        def run(callback):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                return callback()
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as pool:
            native = pool.submit(run, lambda: ingest_receipt(self.payload).status)
            slack = pool.submit(run, lambda: award_first_channel_post_bonus("UINTRO", "CSTART"))
            self.assertEqual(native.result(timeout=30), "processed")
            self.assertIsInstance(slack.result(timeout=30)[0], bool)
        self.assertEqual(VolunteerRecognition.objects.filter(user=self.user).count(), 1)
        self.assertEqual(VolunteerRecognition.objects.get(user=self.user).status, "approved")
        self.assertEqual(Ledger.objects.filter(user=self.user).count(), 1)
        self.assertEqual(PointsService.get_balance(self.user)["balance"], 4)

    def test_native_and_slack_first_awards_are_one_outcome_under_intro_only_gate(self):
        self.race()

    def test_pending_native_and_slack_awards_share_the_original_request(self):
        _, original = self.pending()
        self.race()
        self.assertEqual(VolunteerRecognition.objects.get(user=self.user).pk, original.pk)
