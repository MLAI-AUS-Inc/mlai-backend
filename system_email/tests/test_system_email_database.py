"""Database-backed: the send ledger, idempotency, suppression and webhook feedback."""

from unittest.mock import patch

from django.test import TestCase, override_settings

from system_email import api, transport_resend
from system_email.models import SystemEmail, SystemEmailSuppression, SystemEmailWebhookEvent
from system_email.webhooks import record_event

SETTINGS = dict(
    SYSTEM_EMAIL_RESEND_API_KEY="re_test_key",
    MLAI_SYSTEM_FROM_EMAIL="MLAI <notifications@notify.mlai.au>",
)
CODE_CONTEXT = {"verification_code": "246810", "expires_minutes": 10, "first_name": "Sam"}


@override_settings(**SETTINGS)
class SendLedgerTests(TestCase):
    @patch("system_email.transport_resend.send", return_value="re_1")
    def test_send_records_without_secrets_and_is_idempotent(self, send):
        with self.assertLogs("system_email", level="INFO") as logs:
            first = api.send("chat_sign_in_code", to="Sam@Example.com", idempotency_key="chat-code/1",
                             context=CODE_CONTEXT, user_id=4)
        second = api.send("chat_sign_in_code", to="sam@example.com", idempotency_key="chat-code/1",
                          context=CODE_CONTEXT, user_id=4)
        self.assertEqual(first.provider_message_id, "re_1")
        self.assertTrue(second.duplicate)
        send.assert_called_once()
        self.assertEqual(send.call_args.kwargs["idempotency_key"], "system-email/chat-code/1")
        row = SystemEmail.objects.get()
        self.assertEqual((row.status, row.to_email, row.category), ("sent", "sam@example.com", "auth"))
        stored = " ".join(str(value) for value in row.__dict__.values())
        self.assertNotIn("246810", stored)
        self.assertNotIn("246810", "\n".join(logs.output))

    @patch("system_email.transport_resend.send", side_effect=transport_resend.SystemEmailRetryable("resend_503"))
    def test_retryable_failure_stays_retryable(self, _send):
        with self.assertRaises(transport_resend.SystemEmailRetryable):
            api.send("chat_sign_in_code", to="a@example.com", idempotency_key="chat-code/2", context=CODE_CONTEXT)
        row = SystemEmail.objects.get()
        self.assertEqual((row.status, row.error_code, row.attempts), ("sending", "resend_503", 1))

    @patch("system_email.transport_resend.send", return_value="re_2")
    def test_suppressed_notification_is_not_sent_but_auth_is_attempted(self, send):
        SystemEmailSuppression.objects.create(email="a@example.com", reason="hard_bounce")
        with self.assertRaises(transport_resend.SystemEmailRejected):
            api.send("monthly_update_7d", to="a@example.com", idempotency_key="m/1",
                     context={"startups": [{"name": "X", "expires_at_display": "d", "update_url": "https://x"}]})
        send.assert_not_called()
        api.send("chat_sign_in_code", to="a@example.com", idempotency_key="chat-code/3", context=CODE_CONTEXT)
        send.assert_called_once()

    def test_reused_key_for_another_recipient_is_refused(self):
        with patch("system_email.transport_resend.send", return_value="re_3"):
            api.send("chat_sign_in_code", to="a@example.com", idempotency_key="chat-code/4", context=CODE_CONTEXT)
            with self.assertRaises(transport_resend.SystemEmailRejected):
                api.send("chat_sign_in_code", to="b@example.com", idempotency_key="chat-code/4",
                         context=CODE_CONTEXT)


@override_settings(**SETTINGS)
class WebhookRecordTests(TestCase):
    def setUp(self):
        self.row = SystemEmail.objects.create(
            message_key="magic_link", category="auth", provider="resend", to_email="a@example.com",
            idempotency_key="magic/1", provider_message_id="re_9", status="sent",
        )

    def event(self, kind, bounce=""):
        return {"type": kind, "email_id": "re_9", "recipient": "a@example.com", "bounce_type": bounce, "at": None}

    def test_delivered_then_late_sent_keeps_delivered(self):
        self.assertEqual(record_event(self.event("email.delivered"), "msg_1")["status"], "recorded")
        record_event(self.event("email.sent"), "msg_2")
        self.row.refresh_from_db()
        self.assertEqual(self.row.status, "delivered")
        self.assertIsNotNone(self.row.delivered_at)

    def test_duplicate_event_is_ignored(self):
        record_event(self.event("email.delivered"), "msg_1")
        self.assertEqual(record_event(self.event("email.delivered"), "msg_1")["status"], "duplicate")
        self.assertEqual(SystemEmailWebhookEvent.objects.count(), 1)

    def test_hard_bounce_and_complaint_suppress_soft_bounce_does_not(self):
        record_event(self.event("email.bounced", "transient"), "msg_1")
        self.assertFalse(SystemEmailSuppression.objects.exists())
        record_event(self.event("email.bounced", "permanent"), "msg_2")
        self.assertEqual(SystemEmailSuppression.objects.get().reason, "hard_bounce")
        record_event(self.event("email.complained"), "msg_3")
        self.assertEqual(SystemEmailSuppression.objects.get().reason, "complaint")
        self.row.refresh_from_db()
        self.assertEqual(self.row.status, "complained")

    def test_system_bounce_never_pauses_crm_senders(self):
        from crm.models import SenderMailbox

        mailbox = SenderMailbox.objects.create(
            email="hello@news.mlai.au", display_name="MLAI", domain="news.mlai.au", route="resend", enabled=True,
        )
        record_event(self.event("email.bounced", "permanent"), "msg_1")
        mailbox.refresh_from_db()
        self.assertTrue(mailbox.enabled)
