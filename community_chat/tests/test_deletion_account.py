"""Whole-account removal is conditional on verified evidence and reviewed relations."""

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from community_chat import deletion_tasks
from community_chat.deletion_account import AccountErasureBlocked, finish_account
from community_chat.models import (
    AccountDeletionRequest,
    CommunityChatChallenge,
    CommunityChatDevice,
    CommunityChatInviteAudit,
    DeviceBindingStatus,
)
from core.models import PasswordResetChallenge, PasswordResetEmailDelivery, UserEmailAlias
from system_email.models import SystemEmail


class AccountErasureTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(email="delete-account@example.test")
        self.other = get_user_model().objects.create_user(email="other-account@example.test")
        self.case = AccountDeletionRequest.objects.create(
            user=self.user, scope=AccountDeletionRequest.Scope.ACCOUNT, policy_version="fixture",
        )
        deletion_tasks.schedule_deletion(self.case)

    def verify_prerequisites(self):
        for target in deletion_tasks.CHAT_TARGETS:
            task = deletion_tasks.claim_task(self.case.tasks.get(target=target).pk)
            self.assertTrue(deletion_tasks.complete_task(
                task.pk, attempt=task.attempts,
                verified_counts={"remaining_rows": 0, "verified_targets": 1},
            ))

    def test_erases_protected_identity_rows_and_leaves_an_unlinked_receipt(self):
        deadline = timezone.now() + timedelta(hours=1)
        device = CommunityChatDevice.objects.create(
            user=self.user, revoked_by=self.user, public_key="a" * 64, status=DeviceBindingStatus.REVOKED,
        )
        challenge = CommunityChatChallenge.objects.create(
            user=self.user, public_key=device.public_key, installation_id=device.installation_id,
            expires_at=deadline, nonce_hash="f" * 64,
        )
        invite = CommunityChatInviteAudit.objects.create(device=device, challenge=challenge, expires_at=deadline)
        reset = PasswordResetChallenge.objects.create(user=self.user, expires_at=deadline, secret_hash="b" * 64)
        delivery = PasswordResetEmailDelivery.objects.create(challenge=reset, encrypted_reset_link="synthetic")
        UserEmailAlias.objects.create(user=self.user, email="alias-for-deletion@example.test")
        retained = CommunityChatDevice.objects.create(user=self.other, public_key="e" * 64)
        user_id = self.user.pk
        log = SystemEmail.objects.create(
            user_id=user_id, to_email=self.user.email, message_key="chat_sign_in_code",
            category="auth", idempotency_key="synthetic-auth-case", provider="fixture",
        )

        with self.assertRaisesMessage(AccountErasureBlocked, "Verify all Chat"):
            finish_account(self.case.pk, apply=True)
        self.verify_prerequisites()
        self.assertEqual(finish_account(self.case.pk)["status"], "ready_for_review")
        self.assertTrue(CommunityChatInviteAudit.objects.filter(pk=invite.pk).exists())
        self.assertEqual(finish_account(self.case.pk, apply=True)["status"], "completed")
        self.assertEqual(finish_account(self.case.pk, apply=True)["status"], "already_completed")
        for row in [device, challenge, invite, reset, delivery]:
            self.assertFalse(type(row).objects.filter(pk=row.pk).exists())
        self.assertFalse(UserEmailAlias.objects.filter(user_id=user_id).exists())
        self.assertFalse(get_user_model().objects.filter(pk=user_id).exists())
        self.assertTrue(CommunityChatDevice.objects.filter(pk=retained.pk).exists())
        log.refresh_from_db()
        self.assertEqual(log.to_email, "")
        self.assertIsNone(log.user_id)
        self.case.refresh_from_db()
        self.assertIsNone(self.case.user_id)
        self.assertEqual(self.case.status, AccountDeletionRequest.Status.COMPLETED)

    def test_preserves_another_accounts_device_when_actor_is_departing(self):
        self.verify_prerequisites()
        device = CommunityChatDevice.objects.create(
            user=self.other, revoked_by=self.user, public_key="e" * 64, status=DeviceBindingStatus.REVOKED,
        )
        with self.assertRaises(AccountErasureBlocked) as blocked:
            finish_account(self.case.pk, apply=True)
        self.assertIn("community_chat.communitychatdevice.revoked_by", blocked.exception.dependencies)
        device.refresh_from_db()
        self.assertEqual(device.revoked_by_id, self.user.pk)
        self.assertTrue(get_user_model().objects.filter(pk=self.user.pk).exists())

    def test_indirect_dependency_rolls_back_earlier_identity_cleanup(self):
        from agent_runtime.models import AgentInstallation

        self.verify_prerequisites()
        device = CommunityChatDevice.objects.create(
            user=self.user, public_key="a" * 64, status=DeviceBindingStatus.REVOKED,
        )
        challenge = CommunityChatChallenge.objects.create(
            user=self.user, public_key=device.public_key, installation_id=device.installation_id,
            expires_at=timezone.now(), nonce_hash="c" * 64,
        )
        protected = AgentInstallation.objects.create(
            owner=self.other, owner_device=device, name="Protected installation",
        )
        with self.assertRaises(AccountErasureBlocked) as blocked:
            finish_account(self.case.pk, apply=True)
        self.assertIn("agent_runtime.agentinstallation", blocked.exception.dependencies)
        self.assertTrue(CommunityChatChallenge.objects.filter(pk=challenge.pk).exists())
        self.assertTrue(AgentInstallation.objects.filter(pk=protected.pk).exists())
        self.case.refresh_from_db()
        self.assertIsNone(self.case.completed_at)
        self.assertEqual(self.case.user_id, self.user.pk)
