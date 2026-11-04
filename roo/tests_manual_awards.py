"""Manual Roo credits use canonical MLAI wallets, including without Slack."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import patch
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.db import close_old_connections, connection
from django.test import SimpleTestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient, APITestCase

from community_chat.models import CommunityChatDevice
from roo.manual_awards import ManualAwardError, parse_manual_award
from roo.models import Ledger, PointsAccount, PointsAdmin

User = get_user_model()


class ManualAwardInputTests(SimpleTestCase):
    def test_profile_requires_a_stable_key_and_exactly_one_recipient(self):
        base = {"target_profile_id": str(uuid4()), "points": 4}
        for extra in ({}, {"idempotency_key": "command", "target_slack_id": "UOTHER"}):
            with self.subTest(extra=extra), self.assertRaises(ManualAwardError):
                parse_manual_award({**base, **extra})

    def test_noninteger_points_cannot_be_silently_coerced(self):
        for points in (True, 4.5, "4.5", None, [], 0):
            with self.subTest(points=points), self.assertRaises(ManualAwardError):
                parse_manual_award({"target_slack_id": "UMEMBER", "points": points})


@override_settings(ROO_API_KEY="synthetic-roo-points-key", INTERNAL_API_KEY="synthetic-other-key")
class CanonicalManualAwardTests(APITestCase):
    def setUp(self):
        self.url = reverse("manual-award")
        self.actor = User.objects.create_user(email="admin@example.test", slack_id="UADMIN")
        self.recipient = User.objects.create_user(email="member@example.test", first_name="Member")
        self.device = CommunityChatDevice.objects.create(
            user=self.recipient, public_key="a" * 64, status="verified",
        )
        self.admin = PointsAdmin.objects.create(
            user=self.actor, slack_user_id="UADMIN", role="admin", weekly_allowance=10,
        )
        self.client.credentials(HTTP_X_API_KEY="synthetic-roo-points-key")
        self.payload = {
            "admin_slack_id": "UADMIN", "target_profile_id": str(self.recipient.community_chat_profile_id),
            "points": 4, "reason": "Helpful community contribution", "idempotency_key": "source-event:0",
        }

    def award(self, **changes):
        return self.client.post(self.url, {**self.payload, **changes}, format="json")

    def test_chat_only_account_receives_points_in_its_existing_wallet(self):
        users_before = User.objects.count()
        with patch("roo.views.get_or_create_user_for_slack_id") as lookup:
            result = self.award()
        self.assertEqual(result.status_code, 200, result.data)
        self.assertTrue(result.data["created"])
        self.assertIsNone(result.data["recipient"]["slack_user_id"])
        self.assertEqual(result.data["recipient"]["profile_id"], self.payload["target_profile_id"])
        self.assertEqual(PointsAccount.objects.get(user=self.recipient).balance, 4)
        self.assertEqual(Ledger.objects.get().user_id, self.recipient.pk)
        self.assertEqual(User.objects.count(), users_before)
        lookup.assert_not_called()
        self.client.force_authenticate(self.recipient)
        balance = self.client.get("/api/v1/points/me/balance/")
        self.assertEqual(balance.status_code, 200, balance.data)
        self.assertEqual(balance.data["balance"], 4)

    def test_repeat_returns_same_credit_even_after_allowance_is_exhausted(self):
        self.admin.weekly_allowance = 4
        self.admin.save(update_fields=["weekly_allowance"])
        first, replay = self.award(), self.award()
        self.assertEqual((first.status_code, replay.status_code), (200, 200))
        self.assertEqual(first.data["ledger_id"], replay.data["ledger_id"])
        self.assertFalse(replay.data["created"])
        self.assertTrue(replay.data["replayed"])
        self.assertEqual(Ledger.objects.count(), 1)
        self.assertEqual(PointsAccount.objects.get(user=self.recipient).balance, 4)

    def test_changed_operation_payload_is_a_conflict(self):
        self.assertEqual(self.award().status_code, 200)
        other = User.objects.create_user(email="other@example.test")
        CommunityChatDevice.objects.create(user=other, public_key="b" * 64, status="verified")
        for changes in ({"points": 5}, {"reason": "Changed reason"},
                        {"target_profile_id": str(other.community_chat_profile_id)}):
            with self.subTest(changes=changes):
                self.assertEqual(self.award(**changes).status_code, 409)
        self.assertEqual(Ledger.objects.count(), 1)

    def test_linking_slack_later_keeps_the_same_wallet_and_operation(self):
        original = self.award()
        self.recipient.slack_id = "UNEWLINK"
        self.recipient.save(update_fields=["slack_id"])
        payload = {k: v for k, v in self.payload.items() if k != "target_profile_id"}
        replay = self.client.post(self.url, {**payload, "target_slack_id": "UNEWLINK"}, format="json")
        self.assertEqual(replay.status_code, 200, replay.data)
        self.assertEqual(replay.data["ledger_id"], original.data["ledger_id"])
        self.assertEqual(Ledger.objects.count(), 1)

    def test_canonical_account_comparison_blocks_profile_self_awards(self):
        CommunityChatDevice.objects.create(user=self.actor, public_key="c" * 64, status="verified")
        result = self.award(target_profile_id=str(self.actor.community_chat_profile_id))
        self.assertEqual(result.status_code, 400)
        self.assertFalse(Ledger.objects.exists())

    def test_inactive_and_revoked_recipients_cannot_receive(self):
        self.device.revoked_at = timezone.now()
        self.device.save(update_fields=["revoked_at"])
        self.assertEqual(self.award().status_code, 404)
        self.device.revoked_at = None
        self.device.save(update_fields=["revoked_at"])
        self.recipient.is_active = False
        self.recipient.save(update_fields=["is_active"])
        self.assertEqual(self.award().status_code, 404)
        self.assertFalse(Ledger.objects.exists())

    def test_allowance_and_role_are_enforced_in_backend(self):
        self.assertEqual(self.award().status_code, 200)
        self.assertEqual(self.award(points=7, idempotency_key="second-command").status_code, 400)
        self.admin.role = "partner"
        self.admin.save(update_fields=["role"])
        self.assertEqual(self.award(idempotency_key="third-command").status_code, 403)
        self.assertEqual(Ledger.objects.count(), 1)

    def test_unknown_profile_never_creates_a_slack_placeholder(self):
        count = User.objects.count()
        with patch("roo.views.get_or_create_user_for_slack_id") as lookup:
            self.assertEqual(self.award(target_profile_id=str(uuid4())).status_code, 404)
        lookup.assert_not_called()
        self.assertEqual(User.objects.count(), count)

    def test_service_authentication_is_required(self):
        self.client.credentials()
        self.assertIn(self.award().status_code, (401, 403))

    def test_stale_admin_snapshot_cannot_award_after_deactivation_or_unlink(self):
        from roo.manual_awards import adjust_points
        from roo.permissions import PermissionDeniedError

        values = parse_manual_award(self.payload)
        for changes in ({"is_active": False}, {"is_active": True, "slack_id": None}):
            User.objects.filter(pk=self.actor.pk).update(**changes)
            with self.subTest(changes=changes), self.assertRaises(PermissionDeniedError):
                adjust_points(actor=self.actor, admin_slack_id="UADMIN", values=values,
                              resolve_slack_user=lambda _: self.recipient)
        self.assertFalse(Ledger.objects.exists())

    def test_stale_recipient_snapshot_is_revalidated_under_account_lock(self):
        from roo.manual_awards import adjust_points

        self.recipient.slack_id = "URECIPIENT"
        self.recipient.save(update_fields=["slack_id"])
        User.objects.filter(pk=self.recipient.pk).update(is_active=False)
        values = parse_manual_award({"target_slack_id": "URECIPIENT", "points": 4})
        with self.assertRaises(ManualAwardError):
            adjust_points(actor=self.actor, admin_slack_id="UADMIN", values=values,
                          resolve_slack_user=lambda _: self.recipient)
        self.assertFalse(Ledger.objects.exists())

    def test_legacy_slack_adjustments_keep_optional_keys_and_replay_safe_debits(self):
        self.recipient.slack_id = "ULEGACY"
        self.recipient.save(update_fields=["slack_id"])
        payload = {"admin_slack_id": "UADMIN", "target_slack_id": "ULEGACY", "points": 4}
        self.assertEqual(self.client.post(self.url, payload, format="json").status_code, 200)
        self.assertEqual(self.client.post(self.url, payload, format="json").status_code, 200)
        debit = {**payload, "points": -2, "idempotency_key": "legacy-debit"}
        first = self.client.post(self.url, debit, format="json")
        replay = self.client.post(self.url, debit, format="json")
        self.assertEqual((first.status_code, replay.status_code), (200, 200))
        self.assertEqual(first.data["ledger_id"], replay.data["ledger_id"])
        self.assertEqual(PointsAccount.objects.get(user=self.recipient).balance, 6)
        self.assertEqual(Ledger.objects.count(), 3)

    def test_stale_slack_recipient_cannot_receive_after_unlink_or_transfer(self):
        from roo.manual_awards import adjust_points

        self.recipient.slack_id = "URECIPIENT"
        self.recipient.save(update_fields=["slack_id"])
        values = parse_manual_award({"target_slack_id": "URECIPIENT", "points": 4})
        User.objects.filter(pk=self.recipient.pk).update(slack_id=None)
        for transferred in (False, True):
            if transferred:
                User.objects.create_user(email="new-slack-owner@example.test", slack_id="URECIPIENT")
            with self.subTest(transferred=transferred), self.assertRaises(ManualAwardError) as raised:
                adjust_points(actor=self.actor, admin_slack_id="UADMIN", values=values,
                              resolve_slack_user=lambda _: self.recipient)
            self.assertEqual(raised.exception.status, 409)
        self.assertFalse(Ledger.objects.exists())


@override_settings(ROO_API_KEY="synthetic-concurrency-key", INTERNAL_API_KEY="synthetic-other-key")
class ConcurrentCanonicalAwardTests(TransactionTestCase):
    def setUp(self):
        if connection.vendor != "postgresql":
            self.skipTest("Real row-lock serialization requires PostgreSQL")
        self.actor = User.objects.create_user(email="parallel-admin@example.test", slack_id="UPARALLEL")
        self.recipient = User.objects.create_user(email="parallel-member@example.test")
        CommunityChatDevice.objects.create(user=self.recipient, public_key="d" * 64, status="verified")
        PointsAdmin.objects.create(user=self.actor, slack_user_id="UPARALLEL", role="admin", weekly_allowance=4)

    def concurrent(self, operations):
        barrier = Barrier(2)
        def request(operation):
            close_old_connections()
            try:
                client = APIClient()
                client.credentials(HTTP_X_API_KEY="synthetic-concurrency-key")
                barrier.wait(timeout=10)
                response = client.post(reverse("manual-award"), {
                    "admin_slack_id": "UPARALLEL", "target_profile_id": str(self.recipient.community_chat_profile_id),
                    "points": 4, "reason": "Concurrent contribution", "idempotency_key": operation,
                }, format="json")
                return response.status_code, response.data
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as pool:
            return list(pool.map(request, operations))

    def test_simultaneous_retries_credit_once(self):
        results = self.concurrent(["same-command", "same-command"])
        self.assertEqual([row[0] for row in results], [200, 200], results)
        self.assertEqual(results[0][1]["ledger_id"], results[1][1]["ledger_id"])
        self.assertEqual(Ledger.objects.count(), 1)
        self.assertEqual(PointsAccount.objects.get(user=self.recipient).balance, 4)

    def test_simultaneous_distinct_commands_cannot_overspend_allowance(self):
        results = self.concurrent(["command-one", "command-two"])
        self.assertEqual(sorted(row[0] for row in results), [200, 400], results)
        self.assertEqual(Ledger.objects.count(), 1)
        self.assertEqual(PointsAccount.objects.get(user=self.recipient).balance, 4)
