"""Canonical native rewards and cross-transport races; synthetic sources only."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

from django.contrib.auth import get_user_model
from django.db import close_old_connections, connection, connections, transaction
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from community_chat.models import CommunityChatDevice
from community_chat.volunteer.access import VolunteerError, awards_enabled
from community_chat.volunteer.models import VolunteerRecognition
from community_chat.volunteer.receipts import ingest_receipt, persist_receipt, process_receipt
from integrations.models import CommunityBridgeChannel, CommunityBridgeDelivery, CommunityBridgeMessageLink
from roo.models import Ledger
from roo.services import PointsService
from roo.system_rewards import award_native_boost, canonical_link_love_key

CHANNEL = "00000000-0000-4000-8000-000000000001"
ROOT = "c" * 64
PREFIX = "link_love:CBOOST:1793842044.896789"
SETTINGS = dict(
    COMMUNITY_CHAT_ROO_SLACK_WORKSPACE_ID="TTEST",
    COMMUNITY_CHAT_VOLUNTEER_COMMUNITY="native-reward-tests",
    COMMUNITY_CHAT_VOLUNTEER_ENABLED=True,
    COMMUNITY_CHAT_VOLUNTEER_AWARDS_ENABLED=False,
    COMMUNITY_CHAT_VOLUNTEER_RECOGNITION_ENABLED=False,
    COMMUNITY_CHAT_VOLUNTEER_BOOST_REWARDS_ENABLED=True,
    COMMUNITY_CHAT_VOLUNTEER_BOOST_ACTIVE_FROM="2020-01-01T00:00:00Z",
    COMMUNITY_CHAT_VOLUNTEER_INTRO_REWARDS_ENABLED=True,
    COMMUNITY_CHAT_VOLUNTEER_INTRO_ACTIVE_FROM="2020-01-01T00:00:00Z",
    COMMUNITY_CHAT_VOLUNTEER_BONUSES_ENABLED=False,
    COMMUNITY_CHAT_VOLUNTEER_CHANNELS={"boost_startup": CHANNEL, "start_here": "start"},
    COMMUNITY_CHAT_VOLUNTEER_LIKE_REACTIONS=["👍"],
    COMMUNITY_CHAT_VOLUNTEER_RECEIPT_TOKEN="synthetic-receipt-service-token-32",
)


class NativeFixture:
    def setUp(self):
        self.user = get_user_model().objects.create_user(email="native@example.test")
        self.author = get_user_model().objects.create_user(email="author@example.test", slack_id="UAUTHOR")
        self.enrolled = timezone.now() - timedelta(hours=1)
        for user, key in ((self.user, "a"), (self.author, "b")):
            device = CommunityChatDevice.objects.create(user=user, public_key=key * 64,
                status="verified", verified_at=self.enrolled)
            CommunityChatDevice.objects.filter(pk=device.pk).update(created_at=self.enrolled)
        self.when = timezone.now() - timedelta(minutes=1)

    def payload(self, key, kind="reaction", *, imported=False):
        metadata = {"reaction": "👍"} if kind == "reaction" else {"engagement_confirmed": True}
        if imported:
            metadata["target_bridge_source_id"] = ROOT
        else:
            metadata.update(target_public_key="b" * 64, target_occurred_at=(self.when - timedelta(minutes=1)).isoformat())
        return dict(source_key=key, origin="relay", kind=kind, actor_public_key="a" * 64,
            occurred_at=self.when.isoformat(), metadata=metadata,
            source={"channel_id": CHANNEL, "source_id": ROOT, "message_id": key, "thread_root_id": ROOT})

    def bridge_root(self):
        mapping = CommunityBridgeChannel.objects.create(slack_workspace_id="TTEST", slack_channel_id="CBOOST",
            destination_platform="buzz", destination_channel_id=CHANNEL, audience="community", slack_visibility="public")
        self.link = CommunityBridgeMessageLink.objects.create(channel=mapping, source_platform="slack",
            destination_platform="buzz", source_channel_id="CBOOST", source_message_id="1793842044.896789",
            source_author_id="UAUTHOR", destination_channel_id=CHANNEL, destination_message_id=ROOT)
        self.delivery = CommunityBridgeDelivery.objects.create(channel=mapping, source_platform="slack",
            target_platform="buzz", source_channel_id="CBOOST", source_message_id="1793842044.896789",
            target_channel_id=CHANNEL, delivery_type="create", status="completed",
            completed_at=timezone.now(), available_at=timezone.now())


@override_settings(**SETTINGS)
class NativeRewardParityTests(NativeFixture, TestCase):
    def test_chat_only_intro_without_ai_consent_is_paid_once_after_later_slack_link(self):
        payload = dict(source_key="intro", origin="relay", kind="post", actor_public_key="a" * 64,
            occurred_at=self.when.isoformat(), source={"channel_id": "start", "source_id": "intro"},
            metadata={"original": True, "top_level": True, "has_text": True})
        self.assertEqual(ingest_receipt(payload).status, "processed")
        self.user.slack_id = "ULATER"
        self.user.save(update_fields=("slack_id",))
        self.assertEqual(ingest_receipt(payload).status, "processed")
        self.assertEqual(PointsService.get_balance(self.user)["balance"], 4)
        self.assertEqual(Ledger.objects.filter(user=self.user).count(), 1)

    def test_native_reaction_then_reply_and_later_slack_alias_share_one_credit(self):
        self.assertEqual(ingest_receipt(self.payload("reaction")).status, "processed")
        self.assertEqual(ingest_receipt(self.payload("reply", "reply")).status, "processed")
        self.bridge_root()
        self.user.slack_id = "ULATER"
        self.user.save(update_fields=("slack_id",))
        key = canonical_link_love_key(self.user, f"{PREFIX}:ULATER", recipient="ULATER", points=2, reason="link-love")
        ledger, created = PointsService.award(user=self.user, delta=2, source="EVENT", description="link-love",
            created_by_slack_id="SYSTEM", idempotency_key=key)
        self.assertFalse(created)
        self.assertEqual(ledger.pk, Ledger.objects.get(user=self.user).pk)
        self.assertEqual(PointsService.get_balance(self.user)["balance"], 2)
        self.assertEqual(VolunteerRecognition.objects.filter(user=self.user).count(), 1)

    def test_existing_slack_credit_is_mirrored_without_second_native_payment(self):
        self.bridge_root()
        original, _ = PointsService.award(user=self.user, delta=2, source="EVENT", description="link-love",
            created_by_slack_id="SYSTEM", idempotency_key=f"{PREFIX}:UHISTORICAL")
        result = ingest_receipt(self.payload("native-reply", "reply", imported=True))
        self.assertEqual(result.status, "processed", result.error)
        self.assertEqual(result.recognition.ledger_id, original.pk)
        self.assertEqual(Ledger.objects.filter(user=self.user).count(), 1)

    def test_imported_root_requires_exact_completed_public_mapping(self):
        payload = self.payload("unproven", imported=True)
        with self.assertRaisesMessage(VolunteerError, "source_unavailable"):
            ingest_receipt(payload)
        self.bridge_root()
        self.delivery.status = "processing"
        self.delivery.save(update_fields=("status",))
        with self.assertRaisesMessage(VolunteerError, "source_unavailable"):
            ingest_receipt(payload)
        self.delivery.status = "completed"
        self.delivery.save(update_fields=("status",))
        self.link.channel.hold_reason = "restricted"
        self.link.channel.save(update_fields=("hold_reason",))
        with self.assertRaisesMessage(VolunteerError, "source_unavailable"):
            ingest_receipt(payload)
        self.assertFalse(Ledger.objects.filter(user=self.user).exists())

    def test_revoked_source_is_terminal_at_ingestion_without_acknowledging_bad_credentials(self):
        self.bridge_root()
        self.link.channel.hold_reason = "restricted"
        self.link.channel.save(update_fields=("hold_reason",))
        client = APIClient()
        path = '/api/v1/community-chat/volunteer/internal/receipts/'
        result = client.post(path, self.payload('revoked-root', imported=True), format='json',
            HTTP_AUTHORIZATION='Bearer synthetic-receipt-service-token-32')
        self.assertEqual(result.status_code, 202)
        self.assertEqual(result.data['status'], 'ignored')
        denied = client.post(path, self.payload('unauthorised', imported=True), format='json',
            HTTP_AUTHORIZATION='Bearer invalid-service-key')
        self.assertEqual(denied.status_code, 403)
        self.assertFalse(Ledger.objects.filter(user=self.user).exists())

    def test_imported_self_support_and_deleted_reply_cannot_pay(self):
        self.bridge_root()
        self.user.slack_id = "UAUTHOR"
        self.author.slack_id = None
        self.author.save(update_fields=("slack_id",))
        self.user.save(update_fields=("slack_id",))
        result = ingest_receipt(self.payload("own-root", imported=True))
        self.assertEqual(result.status, "ineligible")
        self.assertFalse(Ledger.objects.filter(user=self.user).exists())

    def test_scoped_gate_and_activation_preserve_other_programme_gates(self):
        self.assertTrue(awards_enabled("boost_startup"))
        self.assertFalse(awards_enabled("report_bug"))
        with override_settings(COMMUNITY_CHAT_VOLUNTEER_BOOST_ACTIVE_FROM=""):
            result = ingest_receipt(self.payload("no-boundary"))
        self.assertEqual(result.status, "ineligible")
        self.assertEqual(result.error, "action_inactive")
        self.assertFalse(Ledger.objects.filter(user=self.user).exists())

    def test_author_deletion_of_confirmation_blocks_deferred_payment(self):
        with override_settings(COMMUNITY_CHAT_VOLUNTEER_BOOST_REWARDS_ENABLED=False):
            receipt = ingest_receipt(self.payload("deleted-reply", "reply"))
        ingest_receipt(dict(source_key="deletion", origin="relay", kind="invalidation",
            actor_public_key="a" * 64, occurred_at=self.when.isoformat(),
            source={"channel_id": CHANNEL, "source_id": "deleted-reply"},
            metadata={"invalidated": True, "deletion_kind": 5}))
        result = process_receipt(receipt)
        self.assertEqual(result.status, "ineligible")
        self.assertFalse(Ledger.objects.filter(user=self.user).exists())

    def answer_mark(self):
        payload = self.payload("useful-answer")
        payload["source"]["source_id"] = "d" * 64
        payload["metadata"].update(reaction="✅", question_bridge_source_id=ROOT)
        return payload

    def test_only_the_verified_original_slack_questioner_can_mark_a_native_answer(self):
        self.bridge_root()
        self.author.slack_id = None
        self.author.save(update_fields=("slack_id",))
        self.user.slack_id = "UAUTHOR"
        self.user.save(update_fields=("slack_id",))
        with override_settings(COMMUNITY_CHAT_VOLUNTEER_CHANNELS={"help": CHANNEL},
                COMMUNITY_CHAT_VOLUNTEER_ADVICE_REWARDS_ENABLED=True,
                COMMUNITY_CHAT_VOLUNTEER_ACTIVE_FROM="2020-01-01T00:00:00Z"):
            result = ingest_receipt(self.answer_mark())
        self.assertEqual(result.status, "processed", result.error)
        self.assertEqual(result.recognition.user_id, self.author.pk)
        self.assertEqual(PointsService.get_balance(self.author)["balance"], 2)
        self.assertFalse(Ledger.objects.filter(user=self.user).exists())

    def test_unrelated_native_marker_cannot_claim_a_bridged_question(self):
        self.bridge_root()
        with override_settings(COMMUNITY_CHAT_VOLUNTEER_CHANNELS={"help": CHANNEL},
                COMMUNITY_CHAT_VOLUNTEER_ADVICE_REWARDS_ENABLED=True):
            result = ingest_receipt(self.answer_mark())
        self.assertEqual(result.status, "ineligible")
        self.assertFalse(Ledger.objects.filter(user__in=(self.user, self.author)).exists())

    def test_rotated_then_reassigned_root_key_keeps_its_original_canonical_author(self):
        now = timezone.now()
        CommunityChatDevice.objects.filter(user=self.author, public_key='b' * 64).update(
            status='revoked', revoked_at=now-timedelta(seconds=20))
        replacement = get_user_model().objects.create_user(email='new-key-owner@example.test')
        device = CommunityChatDevice.objects.create(user=replacement, public_key='b' * 64,
            status='verified', verified_at=now-timedelta(seconds=10))
        CommunityChatDevice.objects.filter(pk=device.pk).update(created_at=now-timedelta(seconds=11))
        result = ingest_receipt(self.payload('old-root-new-key-owner'))
        self.assertEqual(result.status, 'processed', result.error)
        self.assertEqual(result.target_id, self.author.pk)
        self.assertEqual(PointsService.get_balance(self.user)['balance'], 2)
        self.assertFalse(Ledger.objects.filter(user=replacement).exists())

    def test_rotating_ones_own_key_cannot_turn_self_support_into_a_reward(self):
        now = timezone.now()
        CommunityChatDevice.objects.filter(user=self.user, public_key='a' * 64).update(
            status='revoked', revoked_at=now-timedelta(seconds=20))
        device = CommunityChatDevice.objects.create(user=self.user, public_key='d' * 64,
            status='verified', verified_at=now-timedelta(seconds=10))
        CommunityChatDevice.objects.filter(pk=device.pk).update(created_at=now-timedelta(seconds=11))
        payload=self.payload('self-after-rotation')
        payload.update(actor_public_key='d' * 64,occurred_at=now.isoformat())
        payload['metadata']['target_public_key']='a' * 64
        result=ingest_receipt(payload)
        self.assertEqual(result.status, 'ineligible')
        self.assertFalse(Ledger.objects.filter(user=self.user).exists())

    def test_older_receipt_retries_a_reassigned_target_until_source_time_is_supplied(self):
        now = timezone.now()
        CommunityChatDevice.objects.filter(user=self.author, public_key='b' * 64).update(
            status='revoked', revoked_at=now-timedelta(seconds=20))
        replacement = get_user_model().objects.create_user(email='replacement-owner@example.test')
        device = CommunityChatDevice.objects.create(user=replacement, public_key='b' * 64,
            status='verified', verified_at=now-timedelta(seconds=10))
        CommunityChatDevice.objects.filter(pk=device.pk).update(created_at=now-timedelta(seconds=11))
        payload = self.payload('old-format-retry')
        timestamp = payload['metadata'].pop('target_occurred_at')
        with self.assertRaises(VolunteerError) as caught:
            ingest_receipt(payload)
        self.assertEqual((caught.exception.code, caught.exception.status), ('source_time_required', 409))
        self.assertFalse(Ledger.objects.exists())
        payload['metadata']['target_occurred_at'] = timestamp
        result = ingest_receipt(payload)
        self.assertEqual(result.status, 'processed', result.error)
        self.assertEqual(result.target_id, self.author.pk)
        self.assertEqual(Ledger.objects.filter(user=self.user).count(), 1)
        self.assertFalse(Ledger.objects.filter(user=replacement).exists())


@override_settings(**SETTINGS)
class NativeRewardRaceTests(NativeFixture, TransactionTestCase):
    def test_slack_and_native_awards_serialize_on_the_canonical_member(self):
        if connection.vendor != "postgresql":
            self.skipTest("Canonical reward serialization requires PostgreSQL")
        self.bridge_root()
        barrier = Barrier(2)
        def run(native):
            close_old_connections()
            try:
                user = get_user_model().objects.get(pk=self.user.pk)
                barrier.wait(timeout=10)
                if native:
                    return award_native_boost(user, {"channel_id": CHANNEL, "source_id": ROOT})[0].pk
                with transaction.atomic():
                    get_user_model().objects.select_for_update().get(pk=user.pk)
                    recipient = f"profile:{user.community_chat_profile_id}"
                    key = canonical_link_love_key(user, f"{PREFIX}:{recipient}", recipient=recipient, points=2, reason="link-love")
                    return PointsService.award(user=user, delta=2, source="EVENT", description="link-love",
                        created_by_slack_id="SYSTEM", idempotency_key=key)[0].pk
            finally:
                connections.close_all()
        with ThreadPoolExecutor(max_workers=2) as pool:
            a, b = pool.submit(run, True), pool.submit(run, False)
            self.assertEqual(a.result(timeout=30), b.result(timeout=30))
        self.assertEqual(Ledger.objects.filter(user=self.user).count(), 1)
        self.assertEqual(PointsService.get_balance(self.user)["balance"], 2)
