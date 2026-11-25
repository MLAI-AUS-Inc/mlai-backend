"""Deletion queue retries cannot reset deadlines or manufacture erasure."""

from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from community_chat import deletion_access
from community_chat.deletion_tasks import (
    claim_task, complete_task, deletion_deadline, fail_task, schedule_deletion, targets_for,
)
from community_chat.models import (
    AccountDeletionRequest, AccountDeletionTask, AiConsentRecord, CommunityChatDevice, CommunityMemberProfile, DeviceBindingStatus,
)
from community_chat.onboarding import has_community_access, onboarding_payload

VERIFIED = {"remaining_rows": 0, "verified_targets": 1}


class DeletionTaskTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(email="deletion-queue@example.com")

    def request(self, scope=AccountDeletionRequest.Scope.ACCOUNT):
        record = AccountDeletionRequest.objects.create(user=self.user, scope=scope, policy_version="test")
        schedule_deletion(record)
        return record

    def test_scope_and_idempotency_keep_original_deadline_and_data(self):
        for scope in AccountDeletionRequest.Scope.values:
            record = self.request(scope)
            deadline = deletion_deadline(record)
            schedule_deletion(record)
            self.assertEqual(set(record.tasks.values_list("target", flat=True)), set(targets_for(scope)))
            self.assertEqual(record.tasks.count(), len(targets_for(scope)))
            record.refresh_from_db()
            self.assertEqual(deletion_deadline(record), deadline)
            self.assertEqual(deadline, record.requested_at + timedelta(days=30))
            self.assertEqual(record.status, "requested")
        self.assertTrue(get_user_model().objects.filter(pk=self.user.pk).exists())

    def test_stale_worker_cannot_complete_after_new_attempt_claimed(self):
        record = self.request()
        task = claim_task(record.tasks.get(target="chat_access").pk)
        self.assertIsNone(claim_task(task.pk))
        AccountDeletionTask.objects.filter(pk=task.pk).update(next_attempt_at=timezone.now() - timedelta(seconds=1))
        retry = claim_task(task.pk)
        self.assertEqual(retry.attempts, 2)
        self.assertFalse(complete_task(task.pk, attempt=1, verified_counts=VERIFIED))
        self.assertFalse(fail_task(task.pk, attempt=1, error_code="provider_unavailable"))
        self.assertTrue(complete_task(task.pk, attempt=2, verified_counts=VERIFIED))
        self.assertIsNone(claim_task(task.pk))

    def test_partial_cleanup_never_completes_the_request(self):
        record = self.request()
        tasks = list(record.tasks.order_by("target"))
        first = claim_task(tasks[0].pk)
        complete_task(first.pk, attempt=first.attempts, verified_counts=VERIFIED)
        second = claim_task(tasks[1].pk)
        fail_task(second.pk, attempt=second.attempts, error_code="provider_unavailable")
        claim_task(tasks[2].pk)
        record.refresh_from_db()
        self.assertEqual(record.status, "needs_attention")
        self.assertIsNone(record.completed_at)
        self.assertEqual(record.tasks.filter(status="completed").count(), 1)

    def test_sensitive_evidence_or_remaining_data_cannot_be_marked_complete(self):
        task = claim_task(self.request().tasks.first().pk)
        for evidence in ({}, {"email": self.user.email}, {"remaining_rows": 1},
                         {"deleted_rows": True}, {"remaining_rows": 0}, {"verified_targets": 1}):
            with self.subTest(evidence=evidence), self.assertRaises(ValueError):
                complete_task(task.pk, attempt=task.attempts, verified_counts=evidence)
        task.refresh_from_db()
        self.assertEqual(task.status, "processing")

    def test_chat_only_completion_keeps_the_shared_account(self):
        record = self.request(AccountDeletionRequest.Scope.CHAT)
        for pending in record.tasks.all():
            task = claim_task(pending.pk)
            self.assertTrue(complete_task(task.pk, attempt=task.attempts, verified_counts=VERIFIED))
        record.refresh_from_db()
        self.assertEqual(record.status, AccountDeletionRequest.Status.COMPLETED)
        self.assertEqual(record.user_id, self.user.pk)

    def test_whole_account_completion_requires_removal_of_the_linked_user(self):
        record = self.request()
        tasks = list(record.tasks.order_by("target"))
        for pending in tasks[:-1]:
            task = claim_task(pending.pk)
            self.assertTrue(complete_task(task.pk, attempt=task.attempts, verified_counts=VERIFIED))
        record.refresh_from_db()
        self.assertIsNone(record.completed_at)
        task = claim_task(tasks[-1].pk)
        with self.assertRaisesMessage(ValueError, "requires removal of the linked account"):
            complete_task(task.pk, attempt=task.attempts, verified_counts=VERIFIED)
        record.refresh_from_db()
        self.assertEqual(record.user_id, self.user.pk)
        self.assertIsNone(record.completed_at)
        self.user.delete()
        self.assertTrue(complete_task(task.pk, attempt=task.attempts, verified_counts=VERIFIED))
        record.refresh_from_db()
        self.assertEqual(record.status, AccountDeletionRequest.Status.COMPLETED)
        self.assertIsNone(record.user_id)

    def test_access_worker_revokes_owned_relay_keys_and_does_not_erase_other_accounts(self):
        profile = CommunityMemberProfile.objects.create(
            user=self.user, status=CommunityMemberProfile.Status.APPROVED, city="Melbourne", interests=["AI"],
        )
        consent = AiConsentRecord.objects.create(
            user=self.user, granted_at=timezone.now(), disclosure_version="test", provider_digest="0" * 64,
        )
        self.assertTrue(has_community_access(self.user))
        device = CommunityChatDevice.objects.create(user=self.user, public_key="a" * 64)
        other = get_user_model().objects.create_user(email="other-member@example.test")
        other_device = CommunityChatDevice.objects.create(user=other, public_key="b" * 64)
        record = self.request(AccountDeletionRequest.Scope.CHAT)
        self.assertFalse(has_community_access(self.user))
        payload = onboarding_payload(self.user)
        self.assertEqual(payload["status"], CommunityMemberProfile.Status.SUSPENDED)
        self.assertTrue(payload["deletion_pending"])
        with patch.object(deletion_access.adapter, "revoke_relay_membership", return_value=("revoked", None)) as revoke:
            task = record.tasks.get(target=deletion_access.TARGET)
            self.assertTrue(deletion_access.execute_task(task.pk))
            self.assertFalse(deletion_access.execute_task(task.pk))
            revoke.assert_called_once_with(device.public_key)
        device.refresh_from_db()
        other_device.refresh_from_db()
        task.refresh_from_db()
        consent.refresh_from_db()
        profile.refresh_from_db()
        self.assertEqual(device.status, DeviceBindingStatus.REVOKED)
        self.assertEqual(other_device.status, DeviceBindingStatus.PENDING)
        self.assertEqual(task.status, AccountDeletionTask.Status.COMPLETED)
        self.assertIsNotNone(consent.withdrawn_at)
        self.assertEqual((profile.city, profile.interests), ("", []))
        self.assertTrue(get_user_model().objects.get(pk=self.user.pk).is_active)
