"""No database/network: provider authentication, failure ordering and schedule bounds."""

import base64
import hmac
import json
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase, override_settings
from rest_framework.test import APIRequestFactory

from crm.community_optins import current_consent
from crm.resend_feedback import ResendWebhookView, next_delivery_state, parse_event, verify_signature
from crm.warmup import MESSAGES, schedule_slots

SENDERS = ("hello@mlai-aus.com", "hello@mlai.org.au")
KEY = b"synthetic-webhook-secret-32-bytes!"
SECRET = "whsec_" + base64.b64encode(KEY).decode()
BODY = b'{"type":"email.sent"}'


def signed_headers(body=BODY, stamp=1800000000):
    signature = base64.b64encode(hmac.digest(KEY, f"msg_test.{stamp}.".encode() + body, "sha256")).decode()
    return {"svix-id": "msg_test", "svix-timestamp": str(stamp), "svix-signature": f"v1,{signature}"}


class SignatureTests(SimpleTestCase):
    def test_valid_multi_signature_and_exact_raw_body(self):
        headers = signed_headers()
        self.assertTrue(verify_signature(BODY, headers, SECRET, now=1800000000))
        headers["svix-signature"] = "v2,ignored v1,invalid " + headers["svix-signature"]
        self.assertTrue(verify_signature(BODY, headers, SECRET, now=1800000000))
        self.assertFalse(verify_signature(BODY + b" ", headers, SECRET, now=1800000000))

    def test_expiry_future_wrong_key_and_missing_headers(self):
        for now in (1799999699, 1800000301):
            self.assertFalse(verify_signature(BODY, signed_headers(), SECRET, now=now))
        for secret in ("", "whsec_bad", "whsec_" + base64.b64encode(b"wrong" * 8).decode()):
            self.assertFalse(verify_signature(BODY, signed_headers(), secret, now=1800000000))
        for missing in signed_headers():
            headers = signed_headers(); del headers[missing]
            self.assertFalse(verify_signature(BODY, headers, SECRET, now=1800000000))

    @override_settings(CRM_RESEND_WEBHOOK_SECRET=SECRET, CRM_RESEND_MONITORED_SENDERS=SENDERS)
    def test_unsigned_request_and_storage_failure_are_not_acknowledged(self):
        factory = APIRequestFactory()
        response = ResendWebhookView.as_view()(factory.post("/", data=BODY, content_type="application/json"))
        self.assertEqual(response.status_code, 403)
        with patch("crm.resend_feedback.verify_signature", return_value=True), patch(
            "crm.resend_feedback.parse_event", return_value={"type": "email.sent"}
        ), patch("crm.resend_feedback.record_event", side_effect=RuntimeError("synthetic")):
            response = ResendWebhookView.as_view()(factory.post("/", data=BODY, content_type="application/json", HTTP_SVIX_ID="msg_test"))
            self.assertEqual(response.status_code, 503)


@override_settings(CRM_RESEND_MONITORED_SENDERS=SENDERS)
class EventTests(SimpleTestCase):
    def payload(self, **data):
        return {"type": "email.bounced", "data": {
            "from": "MLAI <hello@mlai-aus.com>", "to": ["bounced+test@resend.dev"],
            "subject": "Test", "email_id": "e72632e8-7372-4d4b-9ec2-6fb517bb9929", **data}}

    def test_scope_and_test_detection(self):
        self.assertTrue(parse_event(self.payload())["test"])
        self.assertFalse(parse_event(self.payload(to=["member@example.net"]))["test"])
        self.assertIsNone(parse_event(self.payload(**{"from": "other@mlai-au.com"})))
        self.assertIsNone(parse_event({"type": "email.opened"}))
        for recipients in ([], ["a@example.net", "b@example.net"], "a@example.net"):
            with self.assertRaises(ValueError):
                parse_event(self.payload(to=recipients))

    def test_terminal_failure_and_complaint_win_out_of_order(self):
        self.assertEqual(next_delivery_state("delivered", "", "email.sent"), ("delivered", ""))
        self.assertEqual(next_delivery_state("bounced", "resend_bounced", "email.delivered"), ("bounced", "resend_bounced"))
        self.assertEqual(next_delivery_state("bounced", "resend_bounced", "email.complained"), ("unsubscribed", "resend_complained"))
        self.assertEqual(next_delivery_state("unsubscribed", "resend_complained", "email.bounced"), ("unsubscribed", "resend_complained"))
        self.assertEqual(next_delivery_state("sent", "resend_delivery_delayed", "email.delivered"), ("delivered", ""))


class ConsentTests(SimpleTestCase):
    def test_absent_unverified_inactive_and_withdrawn_are_not_optins(self):
        profile = SimpleNamespace(status="approved", marketing_opt_in=True,
            user=SimpleNamespace(is_active=True, email_verified_at=datetime.now(timezone.utc)))
        with patch("crm.community_optins.CommunityMemberConsent.objects") as manager:
            latest = manager.filter.return_value.order_by.return_value.first
            for evidence in (None, SimpleNamespace(granted=False)):
                latest.return_value = evidence
                self.assertIsNone(current_consent(profile))
            latest.return_value = SimpleNamespace(granted=True)
            self.assertIsNotNone(current_consent(profile))
            for target, field, value in ((profile, "marketing_opt_in", None), (profile.user, "is_active", False),
                                         (profile.user, "email_verified_at", None), (profile, "status", "suspended")):
                original = getattr(target, field); setattr(target, field, value)
                self.assertIsNone(current_consent(profile)); setattr(target, field, original)


class ScheduleTests(SimpleTestCase):
    def test_actual_audience_is_100_messages_with_stable_senders_and_weekly_spacing(self):
        slots = list(schedule_slots([(str(i), i < 18) for i in range(41)], date(2026, 10, 27)))
        self.assertEqual(Counter(week for _, _, week, _ in slots), {0: 18, 1: 41, 2: 41})
        people, daily = defaultdict(list), Counter()
        for person, sender, week, due in slots:
            people[person].append((sender, due)); daily[(sender, due.date())] += 1
            self.assertGreaterEqual(due.hour, 10); self.assertLessEqual(due.hour, 16)
            self.assertLess(due.date(), date(2026, 11, 17))
        self.assertLessEqual(max(daily.values()), 3)
        for visits in people.values():
            self.assertEqual(len({sender for sender, _ in visits}), 1)
            for (_, first), (_, second) in zip(visits, visits[1:]):
                self.assertEqual(second - first, timedelta(days=7))

    def test_outside_reviewed_capacity_is_refused(self):
        with self.assertRaises(ValueError):
            list(schedule_slots([(str(i), True) for i in range(57)], date(2026, 10, 27)))

    def test_copy_uses_supported_tags_and_no_tracking_redirect(self):
        from crm.blast_email import campaign_body
        from crm.merge_tags import validate
        for subject, body in MESSAGES:
            validate(subject); validate(body)
            blast = SimpleNamespace(stats={"warmup": {"run_id": "test"}}, body_markdown=body, event=None)
            self.assertNotIn("@TrackLink", campaign_body(blast))
