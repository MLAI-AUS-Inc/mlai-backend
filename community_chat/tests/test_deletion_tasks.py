"""Deletion queue retries cannot reset deadlines or manufacture erasure."""

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from community_chat.deletion_tasks import (
    claim_task, complete_task, deletion_deadline, fail_task, schedule_deletion, targets_for,
)
from community_chat.models import AccountDeletionRequest, AccountDeletionTask


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
        task = claim_task(record.tasks.first().pk)
        self.assertIsNone(claim_task(task.pk))
        AccountDeletionTask.objects.filter(pk=task.pk).update(next_attempt_at=timezone.now() - timedelta(seconds=1))
        retry = claim_task(task.pk)
        self.assertEqual(retry.attempts, 2)
        self.assertFalse(complete_task(task.pk, attempt=1, verified_counts={"remaining_rows": 0}))
        self.assertFalse(fail_task(task.pk, attempt=1, error_code="provider_unavailable"))
        self.assertTrue(complete_task(task.pk, attempt=2, verified_counts={"remaining_rows": 0, "revoked_credentials": 1}))
        self.assertIsNone(claim_task(task.pk))

    def test_partial_cleanup_never_completes_the_request(self):
        record = self.request()
        tasks = list(record.tasks.order_by("target"))
        first = claim_task(tasks[0].pk)
        complete_task(first.pk, attempt=first.attempts, verified_counts={"remaining_rows": 0})
        second = claim_task(tasks[1].pk)
        fail_task(second.pk, attempt=second.attempts, error_code="provider_unavailable")
        claim_task(tasks[2].pk)
        record.refresh_from_db()
        self.assertEqual(record.status, "needs_attention")
        self.assertIsNone(record.completed_at)
        self.assertEqual(record.tasks.filter(status="completed").count(), 1)

    def test_sensitive_evidence_or_remaining_data_cannot_be_marked_complete(self):
        task = claim_task(self.request().tasks.first().pk)
        for evidence in ({}, {"email": self.user.email}, {"remaining_rows": 1}, {"deleted_rows": True}):
            with self.subTest(evidence=evidence), self.assertRaises(ValueError):
                complete_task(task.pk, attempt=task.attempts, verified_counts=evidence)
        task.refresh_from_db()
        self.assertEqual(task.status, "processing")
