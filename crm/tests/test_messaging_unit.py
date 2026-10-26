"""Database-free checks for merge tags, SMS rules, Twilio calls and webhooks."""

import base64
import hashlib
import hmac
import json
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import requests
from django.test import SimpleTestCase, override_settings
from rest_framework.test import APIRequestFactory

from crm import merge_tags, twilio
from crm.inbound import keyword_action, sender_e164
from crm.messaging import (
    DeliveryRefused,
    _price,
    channel_block,
    next_status,
    status_callback_url,
    whatsapp_message,
)
from crm.sms import GSM7, UCS2, compose_sms, estimate_cost, sms_segments
from crm.webhooks import EMPTY_TWIML, TwilioSmsInboundWebhookView, TwilioStatusWebhookView
from integrations.services import notification_adapters

TWILIO = {
    "TWILIO_ACCOUNT_SID": "AC-test",
    "TWILIO_AUTH_TOKEN": "twilio-auth-token",
    "TWILIO_MESSAGING_SERVICE_SID": "MG-test",
    "TWILIO_WHATSAPP_FROM": "+61480000000",
    "DEFAULT_BACKEND_URL": "https://api.test",
}
MESSAGES_URL = "https://api.twilio.com/2010-04-01/Accounts/AC-test/Messages.json"


class FakeResponse:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload

    @property
    def text(self):
        return json.dumps(self._payload)


def person(**fields):
    return SimpleNamespace(
        **{"first_name": "", "last_name": "", "display_name": "", **fields}
    )


def meetup(**fields):
    return SimpleNamespace(
        **{
            "name": "MLAI Meetup",
            "url": "https://luma.com/mlai",
            "start_at": datetime(2026, 10, 30, 7, 30, tzinfo=timezone.utc),
            "timezone": "Australia/Melbourne",
            **fields,
        }
    )


class MergeTagTests(SimpleTestCase):
    def test_supported_tags_validate(self):
        merge_tags.validate(
            "Hi {{ first_name }} / {{display_name}}: {{ event_name }} {{ event_start_local }} {{ event_url }}"
        )
        merge_tags.validate("No tags at all, and a lone { brace.")

    def test_other_tags_are_rejected_with_a_code(self):
        for text in ("Hi {{ last_name }}", "{{1}}", "{{ }}", "{{ First_Name }}", "{{ first_name | upper }}"):
            with self.subTest(text=text):
                with self.assertRaises(merge_tags.UnsupportedMergeTag) as caught:
                    merge_tags.validate(text)
                self.assertEqual(caught.exception.code, "unsupported_merge_tag")
                self.assertEqual(caught.exception.status, 400)

    def test_first_name_falls_back_to_display_name_then_there(self):
        self.assertEqual(merge_tags.render("Hi {{ first_name }}", person(first_name="Ada"), None), "Hi Ada")
        self.assertEqual(
            merge_tags.render("Hi {{ first_name }}", person(display_name="Grace Hopper"), None), "Hi Grace"
        )
        self.assertEqual(merge_tags.render("Hi {{ first_name }}", person(), None), "Hi there")
        self.assertEqual(merge_tags.render("Hi {{ first_name }}", None, None), "Hi there")

    def test_display_name_falls_back_to_full_name(self):
        self.assertEqual(
            merge_tags.render("{{ display_name }}", person(first_name="Ada", last_name="Lovelace"), None),
            "Ada Lovelace",
        )
        self.assertEqual(merge_tags.render("{{ display_name }}", person(), None), "there")

    def test_event_fields(self):
        text = merge_tags.render(
            "{{ event_name }} starts {{ event_start_local }}: {{ event_url }}", person(), meetup()
        )
        self.assertEqual(text, "MLAI Meetup starts Fri 30 Oct, 6:30pm: https://luma.com/mlai")

    def test_event_start_uses_the_event_timezone(self):
        start = datetime(2026, 10, 30, 7, 0, tzinfo=timezone.utc)
        self.assertEqual(merge_tags.format_event_start(start, "Australia/Melbourne"), "Fri 30 Oct, 6pm")
        self.assertEqual(merge_tags.format_event_start(start, "Australia/Perth"), "Fri 30 Oct, 3pm")
        self.assertEqual(merge_tags.format_event_start(start, ""), "Fri 30 Oct, 6pm")
        self.assertEqual(merge_tags.format_event_start(start, "Not/AZone"), "Fri 30 Oct, 6pm")
        morning = datetime(2026, 10, 30, 22, 5, tzinfo=timezone.utc)
        self.assertEqual(merge_tags.format_event_start(morning, "Australia/Sydney"), "Sat 31 Oct, 9:05am")
        self.assertEqual(
            merge_tags.format_event_start(datetime(2026, 10, 30, 1, 0, tzinfo=timezone.utc), "UTC"),
            "Fri 30 Oct, 1am",
        )
        self.assertEqual(
            merge_tags.format_event_start(datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc), "UTC"),
            "Fri 30 Oct, 12pm",
        )
        self.assertEqual(merge_tags.format_event_start(None, "UTC"), "")

    def test_missing_event_renders_blank(self):
        self.assertEqual(merge_tags.render("[{{ event_name }}{{ event_start_local }}]", person(), None), "[]")


class SmsTests(SimpleTestCase):
    def test_compose_adds_prefix_and_footer_once(self):
        self.assertEqual(compose_sms("Doors open at 6"), "MLAI: Doors open at 6\n\nReply STOP to opt out")
        composed = compose_sms("Doors open at 6")
        self.assertEqual(compose_sms(composed), composed)
        self.assertEqual(
            compose_sms("mlai: doors open. reply stop to opt out"), "mlai: doors open. reply stop to opt out"
        )
        self.assertEqual(compose_sms("  Hi  \n"), "MLAI: Hi\n\nReply STOP to opt out")

    def test_gsm7_segments(self):
        self.assertEqual(sms_segments(""), (GSM7, 0))
        self.assertEqual(sms_segments("a" * 160), (GSM7, 1))
        self.assertEqual(sms_segments("a" * 161), (GSM7, 2))
        self.assertEqual(sms_segments("a" * 306), (GSM7, 2))
        self.assertEqual(sms_segments("a" * 307), (GSM7, 3))
        self.assertEqual(sms_segments("Café £5 @ Ñoño, ¿qué? Ü\n"), (GSM7, 1))

    def test_extension_characters_count_two(self):
        self.assertEqual(sms_segments("€" * 80), (GSM7, 1))
        self.assertEqual(sms_segments("€" * 81), (GSM7, 2))
        self.assertEqual(sms_segments("^{}\\[~]|"), (GSM7, 1))
        # 152 single characters then an escape pair: the pair moves whole
        # to the next segment rather than splitting.
        self.assertEqual(sms_segments("a" * 152 + "€" + "a" * 152), (GSM7, 3))

    def test_ucs2_segments(self):
        self.assertEqual(sms_segments("ā" * 70), (UCS2, 1))
        self.assertEqual(sms_segments("ā" * 71), (UCS2, 2))
        self.assertEqual(sms_segments("ā" * 134), (UCS2, 2))
        self.assertEqual(sms_segments("ā" * 135), (UCS2, 3))
        self.assertEqual(sms_segments("“quoted”"), (UCS2, 1))

    def test_an_emoji_switches_to_ucs2_and_counts_two(self):
        self.assertEqual(sms_segments("a" * 68 + "🎉"), (UCS2, 1))
        self.assertEqual(sms_segments("a" * 69 + "🎉"), (UCS2, 2))

    @override_settings(CRM_SMS_COST_PER_SEGMENT_AUD="0.0515")
    def test_estimate_cost(self):
        self.assertEqual(estimate_cost(2, 52), Decimal("5.36"))
        self.assertEqual(estimate_cost(0, 52), Decimal("0.00"))

    @override_settings(CRM_SMS_COST_PER_SEGMENT_AUD="not a number")
    def test_estimate_cost_ignores_unreadable_rates(self):
        self.assertEqual(estimate_cost(2, 52), Decimal("0.00"))


@override_settings(**TWILIO)
class TwilioSendTests(SimpleTestCase):
    @patch("integrations.http_client.post")
    def test_send_sms_posts_the_messaging_service(self, post):
        post.return_value = FakeResponse(201, {"sid": "SM123", "status": "accepted"})

        result = twilio.send_sms("+61412345678", "MLAI: Hi", "https://api.test/cb/")

        self.assertEqual(result, twilio.SendResult("sent", sid="SM123"))
        post.assert_called_once_with(
            MESSAGES_URL,
            data={
                "MessagingServiceSid": "MG-test",
                "To": "+61412345678",
                "Body": "MLAI: Hi",
                "StatusCallback": "https://api.test/cb/",
            },
            auth=("AC-test", "twilio-auth-token"),
            timeout=(3, 8),
        )

    @patch("integrations.http_client.post")
    def test_send_whatsapp_template_posts_content_variables(self, post):
        post.return_value = FakeResponse(201, {"sid": "SM456"})

        result = twilio.send_whatsapp_template("+61412345678", "HX1", {"1": "Ada", 2: "Meetup"}, "https://cb/")

        self.assertEqual(result.outcome, "sent")
        data = post.call_args.kwargs["data"]
        self.assertEqual(data["From"], "whatsapp:+61480000000")
        self.assertEqual(data["To"], "whatsapp:+61412345678")
        self.assertEqual(data["ContentSid"], "HX1")
        self.assertEqual(json.loads(data["ContentVariables"]), {"1": "Ada", "2": "Meetup"})
        self.assertEqual(data["StatusCallback"], "https://cb/")

    @patch("integrations.http_client.post")
    def test_outcomes_are_never_retried(self, post):
        cases = [
            (FakeResponse(400, {"code": 21211, "message": "Invalid 'To'"}), "failed", "21211"),
            (FakeResponse(429, {"code": 20429}), "failed", "20429"),
            (FakeResponse(403, None), "failed", "http_403"),
            (FakeResponse(500, {"code": 20500}), "unknown", "20500"),
            (FakeResponse(503, None), "unknown", "http_503"),
            (FakeResponse(201, {"status": "queued"}), "unknown", "missing_sid"),
            (requests.exceptions.ReadTimeout("slow"), "unknown", "timeout"),
            (requests.exceptions.ConnectTimeout("slow"), "unknown", "timeout"),
            (requests.exceptions.ConnectionError("reset"), "unknown", "connection_error"),
        ]
        for response, outcome, code in cases:
            with self.subTest(outcome=outcome, code=code):
                post.reset_mock()
                if isinstance(response, Exception):
                    post.side_effect, post.return_value = response, None
                else:
                    post.side_effect, post.return_value = None, response
                result = twilio.send_sms("+61412345678", "MLAI: Hi", "")
                self.assertEqual((result.outcome, result.error_code, result.sid), (outcome, code, ""))
                self.assertEqual(post.call_count, 1)

    @override_settings(TWILIO_MESSAGING_SERVICE_SID="")
    @patch("integrations.http_client.post")
    def test_unconfigured_sms_never_calls_twilio(self, post):
        self.assertEqual(twilio.send_sms("+61412345678", "x", "").error_code, "not_configured")
        self.assertFalse(twilio.sms_configured())
        post.assert_not_called()

    @override_settings(TWILIO_WHATSAPP_FROM="")
    @patch("integrations.http_client.post")
    def test_unconfigured_whatsapp_never_calls_twilio(self, post):
        result = twilio.send_whatsapp_template("+61412345678", "HX1", {}, "")
        self.assertEqual((result.outcome, result.error_code), ("failed", "not_configured"))
        self.assertFalse(twilio.whatsapp_configured())
        post.assert_not_called()

    @patch("integrations.http_client.post")
    def test_research_whatsapp_posts_are_unchanged(self, post):
        post.return_value = FakeResponse(201, {"sid": "SM789"})

        ok, sid, _ = notification_adapters.send_whatsapp_text("61400000000", "hello")

        self.assertEqual((ok, sid), (True, "SM789"))
        post.assert_called_once_with(
            MESSAGES_URL,
            data={"From": "whatsapp:+61480000000", "To": "whatsapp:+61400000000", "Body": "hello"},
            auth=("AC-test", "twilio-auth-token"),
            timeout=(3, 8),
        )


@override_settings(**TWILIO)
class ContentTemplateTests(SimpleTestCase):
    @patch("integrations.http_client.get")
    def test_lists_every_page(self, get):
        get.side_effect = [
            FakeResponse(200, {
                "contents": [{"sid": "HX1"}],
                "meta": {"next_page_url": "https://content.twilio.com/v1/ContentAndApprovals?Page=1"},
            }),
            FakeResponse(200, {"contents": [{"sid": "HX2"}, "junk"], "meta": {"next_page_url": None}}),
        ]

        contents = twilio.list_content_templates()

        self.assertEqual([item["sid"] for item in contents], ["HX1", "HX2"])
        self.assertEqual(
            [call.args[0] for call in get.call_args_list],
            [
                "https://content.twilio.com/v1/ContentAndApprovals?PageSize=100",
                "https://content.twilio.com/v1/ContentAndApprovals?Page=1",
            ],
        )
        self.assertEqual(get.call_args.kwargs["auth"], ("AC-test", "twilio-auth-token"))

    @patch("integrations.http_client.get")
    def test_refuses_to_follow_a_next_page_off_twilio(self, get):
        get.return_value = FakeResponse(200, {"contents": [], "meta": {"next_page_url": "https://evil.test/x"}})
        with self.assertRaises(twilio.TwilioContentError):
            twilio.list_content_templates()
        self.assertEqual(get.call_count, 1)

    @patch("integrations.http_client.get")
    def test_errors_raise(self, get):
        get.return_value = FakeResponse(401, {"code": 20003})
        with self.assertRaisesMessage(twilio.TwilioContentError, "http_401"):
            twilio.list_content_templates()
        for payload in (None, [], {"meta": {}}):
            with self.subTest(payload=payload):
                get.return_value = FakeResponse(200, payload)
                with self.assertRaisesMessage(twilio.TwilioContentError, "unreadable_response"):
                    twilio.list_content_templates()

    @override_settings(TWILIO_ACCOUNT_SID="")
    @patch("integrations.http_client.get")
    def test_unconfigured(self, get):
        with self.assertRaisesMessage(twilio.TwilioContentError, "not_configured"):
            twilio.list_content_templates()
        get.assert_not_called()

    def test_parse_content_and_approvals_item(self):
        template = twilio.parse_content_template({
            "sid": "HX1",
            "friendly_name": "mlai_event_reminder",
            "variables": {"1": "Ada", "2": "Meetup", "3": "Thu", "4": "url"},
            "types": {
                "twilio/text": {"body": "Plain {{1}}"},
                "whatsapp/card": {"body": "Hi {{1}}, {{2}} starts {{3}}. Details: {{4}}."},
            },
            "approval_requests": {"name": "mlai_event_reminder", "category": "UTILITY", "status": "approved"},
        })
        self.assertEqual(template.sid, "HX1")
        self.assertEqual(template.name, "mlai_event_reminder")
        self.assertEqual(template.body, "Hi {{1}}, {{2}} starts {{3}}. Details: {{4}}.")
        self.assertEqual(template.category, "utility")
        self.assertTrue(template.approved)
        self.assertEqual(template.variable_indexes, (1, 2, 3, 4))

    def test_parse_nested_whatsapp_approval_and_text_body(self):
        template = twilio.parse_content_template({
            "sid": "HX2",
            "types": {"twilio/text": {"body": "Thanks {{ 1 }}"}},
            "approval_requests": {"whatsapp": {"name": "thanks", "category": "MARKETING", "status": "pending"}},
        })
        self.assertEqual(template.name, "thanks")
        self.assertEqual(template.body, "Thanks {{ 1 }}")
        self.assertEqual(template.category, "marketing")
        self.assertFalse(template.approved)
        self.assertEqual(template.variable_indexes, (1,))

    def test_parse_without_approval_or_sid(self):
        self.assertIsNone(twilio.parse_content_template({"friendly_name": "x"}))
        template = twilio.parse_content_template({"sid": "HX3", "types": {}})
        self.assertEqual((template.name, template.body, template.category), ("HX3", "", ""))
        self.assertFalse(template.approved)


class StatusOrderTests(SimpleTestCase):
    def test_moves_forward_only(self):
        self.assertEqual(next_status("sent", "delivered"), "delivered")
        self.assertEqual(next_status("delivered", "read"), "read")
        self.assertEqual(next_status("sent", "read"), "read")
        self.assertEqual(next_status("submitting", "sent"), "sent")
        self.assertIsNone(next_status("delivered", "sent"))
        self.assertIsNone(next_status("read", "delivered"))
        self.assertIsNone(next_status("delivered", "delivered"))

    def test_failures_override_sent_but_not_delivered(self):
        self.assertEqual(next_status("sent", "failed"), "failed")
        self.assertEqual(next_status("sent", "undelivered"), "undelivered")
        self.assertEqual(next_status("submitting", "failed"), "failed")
        self.assertIsNone(next_status("delivered", "failed"))
        self.assertIsNone(next_status("read", "undelivered"))

    def test_failures_are_terminal(self):
        for current in ("failed", "undelivered", "bounced", "unsubscribed"):
            for incoming in ("sent", "delivered", "read", "failed", "undelivered"):
                with self.subTest(current=current, incoming=incoming):
                    self.assertIsNone(next_status(current, incoming))
        self.assertIsNone(next_status("sent", None))

    def test_price(self):
        self.assertEqual(_price("-0.05150"), Decimal("0.05150"))
        self.assertIsNone(_price(""))
        self.assertIsNone(_price("abc"))
        self.assertIsNone(_price("NaN"))


class MessagingConfigTests(SimpleTestCase):
    @override_settings(DEFAULT_BACKEND_URL="https://api.mlai.au/")
    def test_status_callback_url(self):
        self.assertEqual(status_callback_url(), "https://api.mlai.au/api/v1/crm/webhooks/twilio/status/")

    @override_settings(**TWILIO, CRM_SMS_ENABLED=False, CRM_WHATSAPP_ENABLED=True)
    def test_channel_block(self):
        self.assertEqual(channel_block("sms"), "channel_disabled")
        self.assertIsNone(channel_block("whatsapp"))
        with override_settings(TWILIO_WHATSAPP_FROM=""):
            self.assertEqual(channel_block("whatsapp"), "not_configured")


class WhatsAppMessageTests(SimpleTestCase):
    def blast(self, **template_fields):
        template = SimpleNamespace(**{
            "content_sid": "HX1",
            "approved": True,
            "enabled": True,
            "variables": [
                {"index": 1, "source": "first_name", "label": "Name"},
                {"index": 2, "source": "event_name", "label": "Event"},
                {"index": 3, "source": "event_start_local", "label": "When"},
                {"index": 4, "source": "custom", "label": "Details"},
            ],
            **template_fields,
        })
        return SimpleNamespace(
            whatsapp_template=template,
            whatsapp_variables={"4": "Level 3,\n  room   B"},
            event=meetup(),
        )

    def test_maps_sources_and_custom_text(self):
        content_sid, values = whatsapp_message(self.blast(), person(display_name="Ada Lovelace"))
        self.assertEqual(content_sid, "HX1")
        self.assertEqual(
            values, {"1": "Ada", "2": "MLAI Meetup", "3": "Fri 30 Oct, 6:30pm", "4": "Level 3, room B"}
        )

    def test_refuses_unusable_templates(self):
        for fields in ({"approved": False}, {"enabled": False}):
            with self.subTest(fields=fields):
                with self.assertRaises(DeliveryRefused) as caught:
                    whatsapp_message(self.blast(**fields), person())
                self.assertEqual(caught.exception.code, "template_unavailable")
        blast = self.blast()
        blast.whatsapp_template = None
        with self.assertRaises(DeliveryRefused):
            whatsapp_message(blast, person())

    def test_refuses_an_empty_variable(self):
        blast = self.blast()
        blast.whatsapp_variables = {}
        with self.assertRaises(DeliveryRefused) as caught:
            whatsapp_message(blast, person())
        self.assertEqual(caught.exception.code, "missing_template_variable")

    def test_unsupported_source(self):
        blast = self.blast(variables=[{"index": 1, "source": "last_name"}])
        with self.assertRaises(merge_tags.UnsupportedMergeTag):
            whatsapp_message(blast, person())


class InboundParsingTests(SimpleTestCase):
    def test_keywords_are_the_whole_message(self):
        for body in ("STOP", " stop ", "StopAll", "UNSUBSCRIBE", "cancel", "END", "quit", "PAUSE"):
            with self.subTest(body=body):
                self.assertEqual(keyword_action("sms", body), "opt_out")
                self.assertEqual(keyword_action("whatsapp", body), "opt_out")
        for body in ("START", "unstop"):
            self.assertEqual(keyword_action("sms", body), "opt_in")
        self.assertEqual(keyword_action("whatsapp", "join"), "opt_in")
        self.assertEqual(keyword_action("sms", "JOIN"), "reply")
        self.assertEqual(keyword_action("sms", "please stop"), "reply")
        self.assertEqual(keyword_action("sms", "Stop it."), "reply")

    def test_twilio_opt_out_type_wins(self):
        self.assertEqual(keyword_action("sms", "Stop texting me", "STOP"), "opt_out")
        self.assertEqual(keyword_action("sms", "Yes again", "START"), "opt_in")
        self.assertEqual(keyword_action("sms", "help", "HELP"), "reply")

    def test_sender_normalisation(self):
        self.assertEqual(sender_e164("whatsapp:+61412345678"), "+61412345678")
        self.assertEqual(sender_e164("61412345678"), "+61412345678")
        self.assertEqual(sender_e164("447700900123"), "+447700900123")
        self.assertEqual(sender_e164("+61412345678"), "+61412345678")
        self.assertEqual(sender_e164("MLAI"), "")
        self.assertEqual(sender_e164(""), "")


def signed(path, params, token="twilio-auth-token"):
    url = "https://api.test" + path + "".join(key + params[key] for key in sorted(params))
    return base64.b64encode(hmac.new(token.encode(), url.encode(), hashlib.sha1).digest()).decode()


@override_settings(**TWILIO)
class WebhookTests(SimpleTestCase):
    status_path = "/api/v1/crm/webhooks/twilio/status/"
    inbound_path = "/api/v1/crm/webhooks/twilio/sms-inbound/"

    def post(self, view, path, params, signature=None):
        headers = {} if signature is None else {"HTTP_X_TWILIO_SIGNATURE": signature}
        request = APIRequestFactory().post(path, params, **headers)
        return view.as_view()(request)

    def test_routes(self):
        from django.urls import resolve

        self.assertIs(resolve(self.status_path).func.view_class, TwilioStatusWebhookView)
        self.assertIs(resolve(self.inbound_path).func.view_class, TwilioSmsInboundWebhookView)

    @patch("crm.webhooks.record_status_callback")
    @patch("crm.webhooks.handle_inbound")
    def test_unsigned_or_badly_signed_requests_are_forbidden(self, inbound, record):
        params = {"MessageSid": "SM1", "MessageStatus": "delivered"}
        for signature in (None, "", "bad", signed(self.status_path, params, token="other")):
            with self.subTest(signature=signature):
                self.assertEqual(self.post(TwilioStatusWebhookView, self.status_path, params, signature).status_code, 403)
                self.assertEqual(self.post(TwilioSmsInboundWebhookView, self.inbound_path, params, signature).status_code, 403)
        record.assert_not_called()
        inbound.assert_not_called()

    @patch("crm.webhooks.record_status_callback", return_value={"status": "updated"})
    def test_an_unset_auth_token_fails_closed_outside_debug(self, record):
        params = {"MessageSid": "SM1", "MessageStatus": "delivered"}
        with override_settings(TWILIO_AUTH_TOKEN="", DEBUG=False):
            self.assertEqual(self.post(TwilioStatusWebhookView, self.status_path, params).status_code, 403)
        with override_settings(TWILIO_AUTH_TOKEN="", DEBUG=True):
            self.assertEqual(self.post(TwilioStatusWebhookView, self.status_path, params).status_code, 200)
        record.assert_called_once_with(params)

    @patch("crm.webhooks.record_status_callback", return_value={"status": "updated"})
    def test_signed_status_callback(self, record):
        params = {"MessageSid": "SM1", "MessageStatus": "delivered", "ErrorCode": ""}
        response = self.post(TwilioStatusWebhookView, self.status_path, params, signed(self.status_path, params))
        self.assertEqual(response.status_code, 200)
        record.assert_called_once_with(params)

    @patch("crm.webhooks.record_status_callback", side_effect=RuntimeError("boom"))
    def test_a_failing_status_callback_still_answers_200(self, record):
        params = {"MessageSid": "SM1", "MessageStatus": "delivered"}
        with self.assertLogs("crm.webhooks", level="WARNING"):
            response = self.post(
                TwilioStatusWebhookView, self.status_path, params, signed(self.status_path, params)
            )
        self.assertEqual(response.status_code, 200)

    @patch("crm.webhooks.handle_inbound")
    def test_signed_sms_inbound(self, inbound):
        params = {"MessageSid": "SM9", "From": "+61412345678", "Body": "STOP", "OptOutType": "STOP"}
        response = self.post(
            TwilioSmsInboundWebhookView, self.inbound_path, params, signed(self.inbound_path, params)
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content.decode(), EMPTY_TWIML)
        self.assertEqual(response["Content-Type"], "text/xml")
        inbound.assert_called_once_with(
            channel="sms", from_address="+61412345678", body="STOP", provider_id="SM9", opt_out_type="STOP"
        )


class WhatsAppAdapterHookTests(SimpleTestCase):
    payload = {
        "MessageSid": "SM-in-1",
        "From": "whatsapp:+61400000000",
        "WaId": "61400000000",
        "Body": "STOP",
    }

    @patch("crm.inbound.handle_inbound")
    @patch.object(notification_adapters, "_handle_whatsapp_inbound_message", return_value={"opted_out": 1})
    def test_crm_runs_after_research_handling(self, research, crm_inbound):
        result = notification_adapters.handle_whatsapp_webhook(dict(self.payload))

        self.assertEqual(result, {"status": "received", "opted_out": 1, "approved": 0, "replied": 0})
        research.assert_called_once_with({"from": "61400000000", "text": {"body": "STOP"}})
        crm_inbound.assert_called_once_with(
            channel="whatsapp", from_address="whatsapp:+61400000000", body="STOP", provider_id="SM-in-1"
        )

    @patch("crm.inbound.handle_inbound", side_effect=RuntimeError("crm down"))
    @patch.object(notification_adapters, "_handle_whatsapp_inbound_message", return_value={"replied": 1})
    def test_a_crm_failure_never_changes_the_result(self, research, crm_inbound):
        with self.assertLogs("integrations.services.notification_adapters", level="WARNING"):
            result = notification_adapters.handle_whatsapp_webhook(dict(self.payload))
        self.assertEqual(result, {"status": "received", "opted_out": 0, "approved": 0, "replied": 1})

    @patch("crm.inbound.handle_inbound")
    @patch.object(notification_adapters, "_handle_whatsapp_inbound_message", side_effect=RuntimeError("x"))
    def test_crm_still_runs_when_research_handling_fails(self, research, crm_inbound):
        with self.assertLogs("integrations.services.notification_adapters", level="WARNING"):
            result = notification_adapters.handle_whatsapp_webhook({"WaId": "61400000000", "Body": "JOIN"})
        self.assertEqual(result["opted_out"], 0)
        crm_inbound.assert_called_once_with(
            channel="whatsapp", from_address="61400000000", body="JOIN", provider_id=""
        )

    @patch("crm.inbound.handle_inbound")
    @patch.object(notification_adapters, "_handle_whatsapp_inbound_message", return_value={})
    def test_button_taps_pass_the_visible_text(self, research, crm_inbound):
        notification_adapters.handle_whatsapp_webhook({**self.payload, "Body": "", "ButtonPayload": "2"})
        self.assertEqual(crm_inbound.call_args.kwargs["body"], "2")
        notification_adapters.handle_whatsapp_webhook({**self.payload, "Body": "Topic 2", "ButtonPayload": "2"})
        self.assertEqual(crm_inbound.call_args.kwargs["body"], "Topic 2")
