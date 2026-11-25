"""Privacy receipt delivery and address minimisation."""

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from community_chat import deletion_notice
from community_chat.models import AccountDeletionCompletionNotice, AccountDeletionRequest
from system_email import transport_resend


KEY_SETTINGS = {
    "CONNECTOR_CREDENTIAL_KEYS": '{"test":"MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="}',
    "CONNECTOR_CREDENTIAL_ACTIVE_KEY_ID": "test",
    "COMMUNITY_CHAT_DELETION_CONTACT": "privacy@example.test",
}


@override_settings(**KEY_SETTINGS)
class CompletionNoticePayloadTests(SimpleTestCase):
    def test_capture_encrypts_address_and_binds_it_to_the_request(self):
        request = SimpleNamespace(pk=uuid4(), requested_at=timezone.now())
        with patch.object(AccountDeletionCompletionNotice.objects, "create") as create:
            deletion_notice.capture_recipient(request, " Member@Example.Test ")
        record = create.call_args.kwargs
        self.assertEqual(record["expires_at"], request.requested_at + timedelta(days=30))
        ciphertext = record["encrypted_recipient"]
        self.assertNotIn("member@example.test", ciphertext)
        self.assertEqual(
            deletion_notice._recipient(SimpleNamespace(request_id=request.pk, encrypted_recipient=ciphertext)),
            "member@example.test",
        )
        with self.assertRaises(ValueError):
            deletion_notice._recipient(SimpleNamespace(request_id=uuid4(), encrypted_recipient=ciphertext))

    def test_invalid_recipient_leaves_no_address_and_does_not_block_erasure(self):
        request = SimpleNamespace(pk=uuid4(), requested_at=timezone.now())
        with patch.object(AccountDeletionCompletionNotice.objects, "create") as create:
            deletion_notice.capture_recipient(request, "not-an-address")
        record = create.call_args.kwargs
        self.assertEqual(record["encrypted_recipient"], "")
        self.assertEqual(record["status"], AccountDeletionCompletionNotice.Status.NEEDS_ATTENTION)

    def test_completion_email_contains_case_reference_and_no_profile_data(self):
        record = uuid4()
        rendered = deletion_notice._message(
            record, AccountDeletionRequest.Scope.CHAT, contact="privacy@example.test",
        )
        self.assertIn("MLAI Chat data", rendered.text)
        self.assertIn(str(record), rendered.text)
        self.assertIn("privacy@example.test", rendered.text)
        self.assertFalse(rendered.html)


@override_settings(**KEY_SETTINGS)
class CompletionNoticeDatabaseTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(email="member@example.test")
        self.request = AccountDeletionRequest.objects.create(
            user=self.user, scope=AccountDeletionRequest.Scope.ACCOUNT, policy_version="fixture",
        )
        deletion_notice.capture_recipient(self.request, self.user.email)
        self.notice = AccountDeletionCompletionNotice.objects.get(pk=self.request.pk)

    def complete_request(self):
        self.user.delete()
        AccountDeletionRequest.objects.filter(pk=self.request.pk).update(
            status=AccountDeletionRequest.Status.COMPLETED, completed_at=timezone.now(),
        )

    def test_send_after_completion_retries_with_one_key_and_forgets_recipient(self):
        self.assertIsNone(deletion_notice.claim_notice(timezone.now()))
        self.complete_request()
        with patch.object(transport_resend, "send", side_effect=[
            transport_resend.SystemEmailRetryable("retry"),
            "synthetic-provider-receipt",
        ]) as send:
            claimed = deletion_notice.claim_notice(timezone.now())
            self.assertIsNotNone(claimed)
            self.assertIsNone(deletion_notice.claim_notice(timezone.now()))
            self.assertTrue(deletion_notice.deliver_notice(claimed))
            AccountDeletionCompletionNotice.objects.filter(pk=claimed.pk).update(next_attempt_at=timezone.now())
            retry = deletion_notice.claim_notice(timezone.now())
            self.assertFalse(deletion_notice.deliver_notice(claimed))
            self.assertTrue(deletion_notice.deliver_notice(retry))
        self.assertEqual(send.call_args_list[0], send.call_args_list[1])
        self.notice.refresh_from_db()
        self.assertEqual(self.notice.status, self.notice.Status.ACCEPTED)
        self.assertEqual(self.notice.encrypted_recipient, "")
        self.assertIsNone(deletion_notice.claim_notice(timezone.now()))

    def test_expiry_forgets_the_address_even_before_cleanup_finishes(self):
        self.assertEqual(deletion_notice.expire_notices(self.notice.expires_at, limit=20), 1)
        self.notice.refresh_from_db()
        self.assertEqual(self.notice.encrypted_recipient, "")
        self.assertEqual(self.notice.failure_code, "delivery_window_closed")

    def test_reused_address_is_not_sent_a_former_accounts_notice(self):
        self.complete_request()
        get_user_model().objects.create_user(email="member@example.test")
        with patch.object(transport_resend, "send") as send:
            self.assertTrue(deletion_notice.deliver_notice(deletion_notice.claim_notice(timezone.now())))
            send.assert_not_called()
        self.notice.refresh_from_db()
        self.assertEqual(self.notice.failure_code, "recipient_changed")
        self.assertEqual(self.notice.encrypted_recipient, "")
