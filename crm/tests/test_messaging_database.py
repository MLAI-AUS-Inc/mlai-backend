"""PostgreSQL-backed checks for SMS and WhatsApp blasts, status and replies.

Twilio is never called: every send goes through a mocked
``integrations.http_client``.
"""

import base64
import hashlib
import hmac
import io
import itertools
import json
from datetime import datetime, timedelta, timezone as dt_timezone
from unittest.mock import patch

import requests
from django.core.management import CommandError, call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from content_factory.models import (
    NotificationChannel,
    NotificationChannelType,
    NotificationConsentState,
)
from crm.inbound import handle_inbound
from crm.messaging import deliver_twilio_deliveries, record_status_callback, remaining_today
from crm.models import (
    Blast,
    BlastDelivery,
    Contact,
    ContactConsentEvent,
    CrmAuditEvent,
    CrmEvent,
    InboundMessage,
    MessageTemplate,
)
from crm.webhooks import EMPTY_TWIML
from organizations.models import Organization

TWILIO = {
    "TWILIO_ACCOUNT_SID": "AC-test",
    "TWILIO_AUTH_TOKEN": "twilio-auth-token",
    "TWILIO_MESSAGING_SERVICE_SID": "MG-test",
    "TWILIO_WHATSAPP_FROM": "+61480000000",
    "DEFAULT_BACKEND_URL": "https://api.test",
    "CRM_SMS_ENABLED": True,
    "CRM_WHATSAPP_ENABLED": True,
    "CRM_SMS_ALLOWED_PREFIXES": ["+614"],
    "CRM_SMS_DAILY_LIMIT": 500,
    "CRM_WHATSAPP_DAILY_LIMIT": 500,
}
STATUS_URL = "https://api.test/api/v1/crm/webhooks/twilio/status/"
STATUS_PATH = "/api/v1/crm/webhooks/twilio/status/"
SMS_INBOUND_PATH = "/api/v1/crm/webhooks/twilio/sms-inbound/"


class FakeResponse:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def twilio_accepts():
    numbers = itertools.count(1)
    return lambda url, **kwargs: FakeResponse(201, {"sid": f"SM{next(numbers):032d}"})


def signature(path, params):
    signed = "https://api.test" + path + "".join(key + params[key] for key in sorted(params))
    return base64.b64encode(hmac.new(b"twilio-auth-token", signed.encode(), hashlib.sha1).digest()).decode()


class CrmMessagingFixtures:
    def setUp(self):
        self.event = CrmEvent.objects.create(
            luma_event_id="evt-1",
            name="MLAI Meetup",
            url="https://luma.com/mlai",
            start_at=datetime(2026, 10, 30, 7, 30, tzinfo=dt_timezone.utc),
            timezone="Australia/Melbourne",
        )
        self.template = MessageTemplate.objects.create(
            name="mlai_event_reminder",
            content_sid="HX1",
            body="Hi {{1}}, a reminder that {{2}} starts {{3}}. Details: {{4}}. Reply STOP to opt out.",
            variables=[
                {"index": 1, "source": "first_name", "label": "Name"},
                {"index": 2, "source": "event_name", "label": "Event"},
                {"index": 3, "source": "event_start_local", "label": "When"},
                {"index": 4, "source": "custom", "label": "Details"},
            ],
            approved=True,
            enabled=True,
        )
        self.blast = self.make_blast()
        self.people = [
            self.contact("ada", "+61412345671", first_name="Ada"),
            self.contact("grace", "+61412345672", display_name="Grace Hopper"),
            self.contact("alan", "+61412345673"),
        ]

    def make_blast(self, **fields):
        return Blast.objects.create(
            purpose=Blast.Purpose.EVENT_UPDATE,
            event=self.event,
            channels=["sms", "whatsapp"],
            sms_body="Hi {{ first_name }}, doors open at 6.",
            whatsapp_template=self.template,
            whatsapp_variables={"4": "Level 3"},
            status=Blast.Status.SENDING,
            **fields,
        )

    def contact(self, name, phone, **fields):
        return Contact.objects.create(
            email=f"{name}@example.com",
            source=Contact.Source.LUMA,
            phone_e164=phone,
            sms_status=fields.pop("sms_status", Contact.SmsStatus.ACTIVE),
            whatsapp_opt_in=fields.pop("whatsapp_opt_in", Contact.WhatsAppOptIn.GRANTED),
            **fields,
        )

    def queue(self, contact, channel="sms", blast=None, **fields):
        return BlastDelivery.objects.create(
            blast=blast or self.blast,
            contact=contact,
            channel=channel,
            address=contact.phone_e164,
            consent_basis=BlastDelivery.ConsentBasis.EVENT_REGISTRATION,
            **fields,
        )


@override_settings(**TWILIO)
class TwilioDispatchTests(CrmMessagingFixtures, TestCase):
    @patch("integrations.http_client.post")
    def test_three_people_get_three_deliveries_with_sids(self, post):
        post.side_effect = twilio_accepts()
        for person in self.people:
            self.queue(person)

        summary = deliver_twilio_deliveries(self.blast, limit=200)

        self.assertEqual(summary, {"sms": {"sent": 3}})
        deliveries = list(BlastDelivery.objects.filter(blast=self.blast).order_by("created_at", "id"))
        self.assertEqual([delivery.status for delivery in deliveries], ["sent"] * 3)
        self.assertEqual(len({delivery.provider_id for delivery in deliveries}), 3)
        self.assertTrue(all(delivery.provider_id.startswith("SM") for delivery in deliveries))
        self.assertEqual([delivery.segments for delivery in deliveries], [1, 1, 1])
        self.assertEqual(post.call_count, 3)
        bodies = [call.kwargs["data"]["Body"] for call in post.call_args_list]
        self.assertEqual(
            bodies,
            [
                "MLAI: Hi Ada, doors open at 6.\n\nReply STOP to opt out",
                "MLAI: Hi Grace, doors open at 6.\n\nReply STOP to opt out",
                "MLAI: Hi there, doors open at 6.\n\nReply STOP to opt out",
            ],
        )
        first = post.call_args_list[0].kwargs["data"]
        self.assertEqual(first["MessagingServiceSid"], "MG-test")
        self.assertEqual(first["To"], "+61412345671")
        self.assertEqual(first["StatusCallback"], STATUS_URL)

    @patch("integrations.http_client.post")
    def test_each_row_is_submitting_before_its_call(self, post):
        seen = []

        def accept(url, **kwargs):
            seen.append(BlastDelivery.objects.get(address=kwargs["data"]["To"]).status)
            return FakeResponse(201, {"sid": f"SM-{kwargs['data']['To']}"})

        post.side_effect = accept
        for person in self.people[:2]:
            self.queue(person)

        deliver_twilio_deliveries(self.blast, limit=200)

        self.assertEqual(seen, ["submitting", "submitting"])

    @patch("integrations.http_client.post")
    def test_failures_and_timeouts_are_never_retried(self, post):
        post.side_effect = [
            FakeResponse(400, {"code": 21211, "message": "Invalid 'To' Phone Number"}),
            FakeResponse(503, {"code": 20503}),
            requests.exceptions.ReadTimeout("slow"),
        ]
        for person in self.people:
            self.queue(person)

        summary = deliver_twilio_deliveries(self.blast, limit=200)

        self.assertEqual(summary, {"sms": {"failed": 1, "unknown": 2}})
        rows = list(
            BlastDelivery.objects.order_by("created_at", "id").values_list("status", "error_code", "provider_id")
        )
        self.assertEqual(rows, [("failed", "21211", ""), ("unknown", "20503", ""), ("unknown", "timeout", "")])
        self.assertEqual(post.call_count, 3)

        self.assertEqual(deliver_twilio_deliveries(self.blast, limit=200), {})
        self.assertEqual(post.call_count, 3)

    @override_settings(CRM_SMS_DAILY_LIMIT=3)
    @patch("integrations.http_client.post")
    def test_daily_cap_counts_the_last_24_hours(self, post):
        post.side_effect = twilio_accepts()
        earlier = self.make_blast()
        self.queue(self.people[0], blast=earlier, status="delivered", provider_id="SM-earlier-1")
        self.queue(self.people[1], blast=earlier, status="unknown")
        # A 4xx refusal never reached anyone, and old rows have aged out.
        self.queue(self.people[2], blast=earlier, status="failed", error_code="21211")
        old = self.queue(self.contact("old", "+61412345679"), blast=earlier, status="sent", provider_id="SM-old")
        BlastDelivery.objects.filter(pk=old.pk).update(created_at=timezone.now() - timedelta(hours=25))
        self.assertEqual(remaining_today("sms"), 1)
        for person in self.people:
            self.queue(person)

        summary = deliver_twilio_deliveries(self.blast, limit=200)

        self.assertEqual(summary, {"sms": {"sent": 1, "blocked": "daily_limit"}})
        self.assertEqual(post.call_count, 1)
        self.assertEqual(BlastDelivery.objects.filter(blast=self.blast, status="queued").count(), 2)
        self.assertEqual(remaining_today("sms"), 0)
        self.assertEqual(remaining_today("whatsapp"), 500)

    @patch("integrations.http_client.post")
    def test_limit_per_call(self, post):
        post.side_effect = twilio_accepts()
        for person in self.people:
            self.queue(person)

        self.assertEqual(deliver_twilio_deliveries(self.blast, limit=2), {"sms": {"sent": 2}})
        self.assertEqual(BlastDelivery.objects.filter(status="queued").count(), 1)
        self.assertEqual(deliver_twilio_deliveries(self.blast, limit=2), {"sms": {"sent": 1}})

    @override_settings(CRM_SMS_ENABLED=False)
    @patch("integrations.http_client.post")
    def test_disabled_channel_sends_nothing(self, post):
        self.queue(self.people[0])
        self.queue(self.people[1], channel="whatsapp")
        post.side_effect = twilio_accepts()

        summary = deliver_twilio_deliveries(self.blast, limit=200)

        self.assertEqual(summary, {"sms": {"blocked": "channel_disabled"}, "whatsapp": {"sent": 1}})
        self.assertEqual(post.call_count, 1)
        self.assertEqual(BlastDelivery.objects.get(channel="sms").status, "queued")

    @override_settings(TWILIO_MESSAGING_SERVICE_SID="")
    @patch("integrations.http_client.post")
    def test_unconfigured_channel_keeps_rows_queued(self, post):
        self.queue(self.people[0])

        self.assertEqual(deliver_twilio_deliveries(self.blast, limit=200), {"sms": {"blocked": "not_configured"}})
        post.assert_not_called()

    @patch("integrations.http_client.post")
    def test_consent_is_rechecked_at_send_time(self, post):
        post.side_effect = twilio_accepts()
        opted_out = self.queue(self.people[0])
        Contact.objects.filter(pk=self.people[0].pk).update(sms_status=Contact.SmsStatus.OPTED_OUT)
        landline = self.queue(self.contact("land", "+61391234567", sms_status=Contact.SmsStatus.INVALID))
        overseas = self.queue(self.contact("uk", "+447700900123"))
        withdrawn = self.queue(self.people[1], channel="whatsapp")
        Contact.objects.filter(pk=self.people[1].pk).update(whatsapp_opt_in=Contact.WhatsAppOptIn.WITHDRAWN)

        summary = deliver_twilio_deliveries(self.blast, limit=200)

        self.assertEqual(summary, {"sms": {"unsubscribed": 1, "failed": 2}, "whatsapp": {"unsubscribed": 1}})
        post.assert_not_called()
        statuses = {
            row.pk: (row.status, row.error_code) for row in BlastDelivery.objects.all()
        }
        self.assertEqual(statuses[opted_out.pk], ("unsubscribed", "opted_out"))
        self.assertEqual(statuses[landline.pk], ("failed", "not_textable"))
        self.assertEqual(statuses[overseas.pk], ("failed", "not_allowed_country"))
        self.assertEqual(statuses[withdrawn.pk], ("unsubscribed", "not_opted_in"))
        self.assertEqual(remaining_today("sms"), 500)

    @patch("integrations.http_client.post")
    def test_whatsapp_sends_the_mapped_template(self, post):
        post.side_effect = twilio_accepts()
        self.queue(self.people[0], channel="whatsapp")

        self.assertEqual(deliver_twilio_deliveries(self.blast, limit=200), {"whatsapp": {"sent": 1}})

        data = post.call_args.kwargs["data"]
        self.assertEqual(data["From"], "whatsapp:+61480000000")
        self.assertEqual(data["To"], "whatsapp:+61412345671")
        self.assertEqual(data["ContentSid"], "HX1")
        self.assertEqual(
            json.loads(data["ContentVariables"]),
            {"1": "Ada", "2": "MLAI Meetup", "3": "Fri 30 Oct, 6:30pm", "4": "Level 3"},
        )
        self.assertEqual(data["StatusCallback"], STATUS_URL)
        delivery = BlastDelivery.objects.get()
        self.assertEqual(delivery.status, "sent")
        self.assertIsNone(delivery.segments)

    @patch("integrations.http_client.post")
    def test_a_template_that_is_not_enabled_is_refused(self, post):
        MessageTemplate.objects.filter(pk=self.template.pk).update(enabled=False)
        self.queue(self.people[0], channel="whatsapp")

        self.assertEqual(deliver_twilio_deliveries(self.blast, limit=200), {"whatsapp": {"failed": 1}})
        self.assertEqual(BlastDelivery.objects.get().error_code, "template_unavailable")
        post.assert_not_called()

    @patch("integrations.http_client.post")
    def test_unsupported_merge_tag_fails_the_row(self, post):
        Blast.objects.filter(pk=self.blast.pk).update(sms_body="Hi {{ last_name }}")
        self.queue(self.people[0])

        self.assertEqual(deliver_twilio_deliveries(self.blast, limit=200), {"sms": {"failed": 1}})
        self.assertEqual(BlastDelivery.objects.get().error_code, "unsupported_merge_tag")
        post.assert_not_called()

    @patch("integrations.http_client.post")
    def test_interrupted_submissions_become_unknown(self, post):
        stuck = self.queue(self.people[0], status="submitting")
        BlastDelivery.objects.filter(pk=stuck.pk).update(updated_at=timezone.now() - timedelta(hours=1))
        recent = self.queue(self.people[1], status="submitting")

        deliver_twilio_deliveries(self.blast, limit=200)

        stuck.refresh_from_db()
        recent.refresh_from_db()
        self.assertEqual((stuck.status, stuck.error_code), ("unknown", "interrupted"))
        self.assertEqual(recent.status, "submitting")
        post.assert_not_called()

    @patch("integrations.http_client.post")
    def test_cancelled_blast_sends_nothing(self, post):
        Blast.objects.filter(pk=self.blast.pk).update(status=Blast.Status.CANCELLED)
        self.queue(self.people[0])

        self.assertEqual(deliver_twilio_deliveries(self.blast, limit=200), {"sms": {"blocked": "cancelled"}})
        post.assert_not_called()


@override_settings(**TWILIO)
class StatusCallbackTests(CrmMessagingFixtures, TestCase):
    def setUp(self):
        super().setUp()
        self.delivery = self.queue(self.people[0], status="sent", provider_id="SM1")
        self.client = APIClient()

    def callback(self, status_value, **extra):
        return record_status_callback({"MessageSid": "SM1", "MessageStatus": status_value, **extra})

    def test_delivered_then_read_and_replays_are_no_ops(self):
        self.assertEqual(self.callback("delivered")["status"], "updated")
        self.delivery.refresh_from_db()
        delivered_at = self.delivery.delivered_at
        self.assertEqual(self.delivery.status, "delivered")
        self.assertIsNotNone(delivered_at)

        for replay in ("delivered", "sent", "queued", "accepted", "failed", "undelivered"):
            with self.subTest(replay=replay):
                self.assertEqual(self.callback(replay, ErrorCode="30003")["status"], "unchanged")
                self.delivery.refresh_from_db()
                self.assertEqual(self.delivery.status, "delivered")
                self.assertEqual(self.delivery.error_code, "")
                self.assertEqual(self.delivery.delivered_at, delivered_at)

        self.callback("read")
        self.delivery.refresh_from_db()
        self.assertEqual(self.delivery.status, "read")
        self.assertEqual(self.delivery.delivered_at, delivered_at)

    def test_read_without_delivered_sets_delivered_at(self):
        self.callback("read")
        self.delivery.refresh_from_db()
        self.assertEqual(self.delivery.status, "read")
        self.assertIsNotNone(self.delivery.delivered_at)

    def test_failures_override_sent_and_are_terminal(self):
        self.callback("undelivered", ErrorCode="30003")
        self.delivery.refresh_from_db()
        self.assertEqual((self.delivery.status, self.delivery.error_code), ("undelivered", "30003"))

        self.callback("delivered")
        self.callback("failed", ErrorCode="30008")
        self.delivery.refresh_from_db()
        self.assertEqual((self.delivery.status, self.delivery.error_code), ("undelivered", "30003"))
        self.assertIsNone(self.delivery.delivered_at)

    def test_price_is_kept_when_twilio_sends_one(self):
        self.callback("delivered", Price="-0.05150", PriceUnit="aud")
        self.delivery.refresh_from_db()
        self.assertEqual(str(self.delivery.price), "0.05150")
        self.assertEqual(self.delivery.price_unit, "AUD")

    def test_unknown_sids_are_ignored(self):
        self.assertEqual(
            record_status_callback({"MessageSid": "SM-other", "MessageStatus": "delivered"}),
            {"status": "ignored", "reason": "unknown_sid"},
        )
        self.assertEqual(record_status_callback({})["reason"], "missing_sid")

    def test_signed_webhook_updates_and_unsigned_is_forbidden(self):
        params = {"MessageSid": "SM1", "MessageStatus": "delivered", "ErrorCode": ""}

        unsigned = self.client.post(STATUS_PATH, data=params)
        self.assertEqual(unsigned.status_code, 403)
        self.delivery.refresh_from_db()
        self.assertEqual(self.delivery.status, "sent")

        response = self.client.post(STATUS_PATH, data=params, HTTP_X_TWILIO_SIGNATURE=signature(STATUS_PATH, params))
        self.assertEqual(response.status_code, 200)
        self.delivery.refresh_from_db()
        self.assertEqual(self.delivery.status, "delivered")

        unknown = {"MessageSid": "SM-nope", "MessageStatus": "delivered"}
        response = self.client.post(STATUS_PATH, data=unknown, HTTP_X_TWILIO_SIGNATURE=signature(STATUS_PATH, unknown))
        self.assertEqual(response.status_code, 200)


@override_settings(**TWILIO)
class SmsInboundTests(CrmMessagingFixtures, TestCase):
    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.ada = self.people[0]
        # Two contacts can share one number; both follow its keywords.
        self.ada_work = self.contact("ada.work", self.ada.phone_e164)

    def text(self, body, sid="SM-in-1", sender=None, **extra):
        params = {"MessageSid": sid, "From": sender or self.ada.phone_e164, "Body": body, **extra}
        return self.client.post(
            SMS_INBOUND_PATH, data=params, HTTP_X_TWILIO_SIGNATURE=signature(SMS_INBOUND_PATH, params)
        )

    def test_stop_opts_out_every_contact_with_the_number(self):
        response = self.text("Stop ")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content.decode(), EMPTY_TWIML)
        for contact in (self.ada, self.ada_work):
            contact.refresh_from_db()
            self.assertEqual(contact.sms_status, Contact.SmsStatus.OPTED_OUT)
            self.assertIsNotNone(contact.sms_status_changed_at)
            self.assertEqual(contact.whatsapp_opt_in, Contact.WhatsAppOptIn.GRANTED)
        events = ContactConsentEvent.objects.filter(channel="sms")
        self.assertEqual(events.count(), 2)
        self.assertEqual(
            set(events.values_list("kind", "source")), {("unsubscribed", "sms:inbound")}
        )
        self.assertEqual(events.first().detail, {"keyword": "STOP", "provider_id": "SM-in-1"})
        self.assertFalse(InboundMessage.objects.exists())

        self.text("STOP", sid="SM-in-2")
        self.assertEqual(ContactConsentEvent.objects.count(), 2)

    def test_twilio_opt_out_type_counts(self):
        self.text("No more texts please", OptOutType="STOP")
        self.ada.refresh_from_db()
        self.assertEqual(self.ada.sms_status, Contact.SmsStatus.OPTED_OUT)
        self.assertEqual(ContactConsentEvent.objects.first().detail["keyword"], "STOP")

    def test_start_re_enables_a_textable_number_only(self):
        Contact.objects.filter(phone_e164=self.ada.phone_e164).update(sms_status=Contact.SmsStatus.OPTED_OUT)
        landline = self.contact("land", "+61391234567", sms_status=Contact.SmsStatus.OPTED_OUT)

        self.text("start")
        self.text("UNSTOP", sid="SM-in-2", sender=landline.phone_e164)

        self.ada.refresh_from_db()
        landline.refresh_from_db()
        self.assertEqual(self.ada.sms_status, Contact.SmsStatus.ACTIVE)
        self.assertEqual(landline.sms_status, Contact.SmsStatus.OPTED_OUT)
        self.assertEqual(
            ContactConsentEvent.objects.filter(channel="sms", kind="granted", source="sms:inbound").count(), 2
        )

    def test_start_on_sms_never_touches_whatsapp(self):
        Contact.objects.filter(pk=self.ada.pk).update(whatsapp_opt_in=Contact.WhatsAppOptIn.WITHDRAWN)
        self.text("START")
        self.ada.refresh_from_db()
        self.assertEqual(self.ada.whatsapp_opt_in, Contact.WhatsAppOptIn.WITHDRAWN)

    def test_join_on_sms_is_a_reply(self):
        self.text("JOIN")
        self.assertEqual(InboundMessage.objects.get().body, "JOIN")

    def test_reply_links_the_latest_blast_on_that_channel(self):
        older = self.make_blast()
        self.queue(self.ada, blast=older, status="delivered", provider_id="SM-older")
        self.queue(self.ada, status="sent", provider_id="SM-latest")
        whatsapp_only = self.make_blast()
        self.queue(self.ada, channel="whatsapp", blast=whatsapp_only, status="read", provider_id="SM-wa")

        self.assertEqual(self.text("Running 10 minutes late").status_code, 200)
        self.text("Running 10 minutes late", sid="SM-in-1")

        reply = InboundMessage.objects.get()
        self.assertEqual(reply.blast, self.blast)
        self.assertEqual(reply.contact, self.ada)
        self.assertEqual(reply.channel, "sms")
        self.assertEqual(reply.from_address, "+61412345671")
        self.assertEqual(reply.body, "Running 10 minutes late")

    def test_reply_after_seven_days_links_only_the_contact(self):
        stale = self.queue(self.ada, status="delivered", provider_id="SM-stale")
        BlastDelivery.objects.filter(pk=stale.pk).update(created_at=timezone.now() - timedelta(days=8))

        self.text("Thanks!")

        reply = InboundMessage.objects.get()
        self.assertIsNone(reply.blast)
        self.assertIn(reply.contact, (self.ada, self.ada_work))

    def test_unknown_senders_and_empty_messages_are_not_kept(self):
        self.assertEqual(self.text("hello", sender="+61499999999").status_code, 200)
        self.assertEqual(self.text("", sid="SM-in-2").status_code, 200)
        self.assertFalse(InboundMessage.objects.exists())

    def test_unsigned_inbound_is_forbidden(self):
        response = self.client.post(
            SMS_INBOUND_PATH, data={"MessageSid": "SM-x", "From": self.ada.phone_e164, "Body": "STOP"}
        )
        self.assertEqual(response.status_code, 403)
        self.ada.refresh_from_db()
        self.assertEqual(self.ada.sms_status, Contact.SmsStatus.ACTIVE)

    def test_handle_inbound_never_raises(self):
        with patch("crm.inbound._handle", side_effect=RuntimeError("boom")):
            with self.assertLogs("crm.inbound", level="WARNING"):
                result = handle_inbound("sms", self.ada.phone_e164, "STOP", "SM-in-9")
        self.assertEqual(result["action"], "error")
        self.assertEqual(handle_inbound("email", "x", "STOP", "SM-in-10")["action"], "ignored")
        self.assertEqual(handle_inbound("sms", "MLAI", "STOP", "SM-in-11")["reason"], "unreadable_sender")


@override_settings(**TWILIO)
class WhatsAppInboundTests(CrmMessagingFixtures, TestCase):
    """The research-automation webhook keeps its behaviour and feeds crm."""

    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.ada = self.people[0]
        self.org = Organization.objects.create(name="WhatsApp Co", domain="wa.example.com")
        self.channel = NotificationChannel.objects.create(
            organization=self.org,
            channel_type=NotificationChannelType.WHATSAPP,
            route_id=self.ada.phone_e164,
            consent_state=NotificationConsentState.ACTIVE,
        )

    def message(self, body, sid="SM-wa-in-1", sender=None):
        sender = (sender or self.ada.phone_e164).lstrip("+")
        params = {"MessageSid": sid, "From": f"whatsapp:+{sender}", "WaId": sender, "Body": body}
        path = reverse("content_factory_whatsapp_webhook")
        return self.client.post(path, data=params, HTTP_X_TWILIO_SIGNATURE=signature(path, params))

    def test_stop_opts_out_of_research_automations_and_crm_whatsapp(self):
        response = self.message("STOP")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["opted_out"], 1)
        self.channel.refresh_from_db()
        self.assertEqual(self.channel.consent_state, NotificationConsentState.OPTED_OUT)
        self.ada.refresh_from_db()
        self.assertEqual(self.ada.whatsapp_opt_in, Contact.WhatsAppOptIn.WITHDRAWN)
        self.assertEqual(self.ada.whatsapp_opt_in_source, "whatsapp:inbound")
        self.assertEqual(self.ada.sms_status, Contact.SmsStatus.ACTIVE)
        event = ContactConsentEvent.objects.get()
        self.assertEqual((event.channel, event.kind), ("whatsapp", "withdrawn"))

    @patch("integrations.services.notification_adapters.send_whatsapp_text", return_value=(True, "SM-out", {}))
    def test_crm_only_keywords_leave_research_automations_alone(self, send_text):
        self.message("QUIT")
        self.channel.refresh_from_db()
        self.assertEqual(self.channel.consent_state, NotificationConsentState.ACTIVE)
        self.ada.refresh_from_db()
        self.assertEqual(self.ada.whatsapp_opt_in, Contact.WhatsAppOptIn.WITHDRAWN)

    @patch("integrations.services.notification_adapters.send_whatsapp_text", return_value=(True, "SM-out", {}))
    def test_join_and_start_grant_opt_in(self, send_text):
        grace = self.people[1]
        Contact.objects.filter(pk=grace.pk).update(whatsapp_opt_in=Contact.WhatsAppOptIn.UNKNOWN)
        Contact.objects.filter(pk=self.ada.pk).update(whatsapp_opt_in=Contact.WhatsAppOptIn.WITHDRAWN)

        self.message("join", sid="SM-wa-in-1", sender=grace.phone_e164)
        self.message("START", sid="SM-wa-in-2")

        for contact in (grace, self.ada):
            contact.refresh_from_db()
            self.assertEqual(contact.whatsapp_opt_in, Contact.WhatsAppOptIn.GRANTED)
            self.assertEqual(contact.whatsapp_opt_in_source, "whatsapp:inbound")
            self.assertIsNotNone(contact.whatsapp_opt_in_at)
        self.assertEqual(
            ContactConsentEvent.objects.filter(channel="whatsapp", kind="granted").count(), 2
        )
        self.assertFalse(InboundMessage.objects.exists())

    @patch("integrations.services.notification_adapters.send_whatsapp_text", return_value=(True, "SM-out", {}))
    def test_reply_is_kept_and_research_still_answers(self, send_text):
        self.queue(self.ada, channel="whatsapp", status="read", provider_id="SM-wa-out")

        response = self.message("See you there!")
        self.message("See you there!")

        self.assertEqual(response.data["replied"], 1)
        self.assertIn("Reply 1-3", send_text.call_args.args[1])
        reply = InboundMessage.objects.get()
        self.assertEqual((reply.channel, reply.blast, reply.contact), ("whatsapp", self.blast, self.ada))
        self.assertEqual(reply.from_address, "+61412345671")

    @patch("integrations.services.notification_adapters.send_whatsapp_text")
    def test_senders_crm_does_not_know_change_nothing(self, send_text):
        response = self.message("1", sender="+61499999999")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, {"status": "received", "opted_out": 0, "approved": 0, "replied": 0})
        send_text.assert_not_called()
        self.assertFalse(InboundMessage.objects.exists())
        self.assertFalse(ContactConsentEvent.objects.exists())

    @patch("crm.inbound._handle", side_effect=RuntimeError("crm down"))
    def test_a_crm_failure_keeps_the_research_opt_out(self, crm_handle):
        with self.assertLogs("crm.inbound", level="WARNING"):
            response = self.message("STOP")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["opted_out"], 1)
        self.channel.refresh_from_db()
        self.assertEqual(self.channel.consent_state, NotificationConsentState.OPTED_OUT)


@override_settings(**TWILIO)
class WhatsAppTemplateSyncTests(TestCase):
    contents = [
        {
            "sid": "HX1",
            "friendly_name": "mlai_event_reminder",
            "types": {"twilio/text": {"body": "Hi {{1}}, {{2}} starts {{3}}. Details: {{4}}."}},
            "approval_requests": {"name": "mlai_event_reminder", "category": "UTILITY", "status": "approved"},
        },
        {
            "sid": "HX2",
            "friendly_name": "mlai_event_thanks",
            "types": {"twilio/text": {"body": "Thanks for coming to {{2}}, {{1}}! {{3}}"}},
            "approval_requests": {"whatsapp": {"category": "MARKETING", "status": "pending"}},
        },
    ]

    def setUp(self):
        self.existing = MessageTemplate.objects.create(
            name="old name",
            content_sid="HX1",
            body="old body",
            variables=[{"index": 1, "source": "first_name", "label": "Name"}],
            approved=False,
            enabled=True,
        )

    @patch("crm.management.commands.crm_sync_whatsapp_templates.list_content_templates")
    def test_sync_upserts_without_enabling(self, list_templates):
        list_templates.return_value = self.contents
        out = io.StringIO()

        call_command("crm_sync_whatsapp_templates", stdout=out)

        self.assertEqual(json.loads(out.getvalue()), {"approved": 1, "created": 1, "updated": 1})
        self.existing.refresh_from_db()
        self.assertEqual(self.existing.name, "mlai_event_reminder")
        self.assertEqual(self.existing.body, "Hi {{1}}, {{2}} starts {{3}}. Details: {{4}}.")
        self.assertTrue(self.existing.approved)
        self.assertTrue(self.existing.enabled)
        self.assertEqual(self.existing.category, "utility")
        self.assertEqual(
            self.existing.variables,
            [
                {"index": 1, "source": "first_name", "label": "Name"},
                {"index": 2, "source": "custom", "label": "Variable 2"},
                {"index": 3, "source": "custom", "label": "Variable 3"},
                {"index": 4, "source": "custom", "label": "Variable 4"},
            ],
        )
        self.assertIsNotNone(self.existing.last_synced_at)
        created = MessageTemplate.objects.get(content_sid="HX2")
        self.assertEqual((created.name, created.category, created.channel), ("mlai_event_thanks", "marketing", "whatsapp"))
        self.assertFalse(created.approved)
        self.assertFalse(created.enabled)
        self.assertEqual([slot["index"] for slot in created.variables], [1, 2, 3])
        self.assertTrue(CrmAuditEvent.objects.filter(target_type="whatsapp_templates").exists())

        call_command("crm_sync_whatsapp_templates", stdout=io.StringIO())
        self.assertEqual(MessageTemplate.objects.count(), 2)
        self.existing.refresh_from_db()
        self.assertEqual(len(self.existing.variables), 4)

    @override_settings(TWILIO_ACCOUNT_SID="")
    def test_unconfigured_twilio_is_a_command_error(self):
        with self.assertRaises(CommandError):
            call_command("crm_sync_whatsapp_templates", stdout=io.StringIO())
