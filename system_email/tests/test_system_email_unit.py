"""No database or network: rendering, Resend transport, routing and webhook parsing."""

import base64
import hmac
import json
from unittest.mock import Mock, patch

from django.test import SimpleTestCase, override_settings

from system_email import api, health, transport_resend
from system_email.fixtures import SAMPLE_CONTEXT
from system_email.registry import MESSAGES, UnknownMessageError, get_spec
from system_email.render import RenderedEmail, SystemEmailContextError, render
from system_email.webhooks import parse_event, verify_signature

KEY = b"synthetic-system-email-secret-32b!"
SECRET = "whsec_" + base64.b64encode(KEY).decode()
SYSTEM_FROM = "MLAI <notifications@notify.mlai.au>"


def response(status, body):
    mock = Mock(status_code=status)
    mock.json.return_value = body
    return mock


class RenderTests(SimpleTestCase):
    def test_every_templated_message_renders_from_sample_context(self):
        for key, spec in MESSAGES.items():
            if spec.prerendered:
                continue
            with self.subTest(key=key):
                rendered = render(spec, SAMPLE_CONTEXT[key])
                self.assertTrue(rendered.subject)
                self.assertNotIn("\n", rendered.subject)
                self.assertIn("<html", rendered.html)
                self.assertTrue(rendered.text.strip())
                self.assertNotIn("{{", rendered.html + rendered.text)
                self.assertNotIn("{%", rendered.html + rendered.text)

    def test_code_and_links_appear_in_both_parts(self):
        code = render(get_spec("chat_sign_in_code"), SAMPLE_CONTEXT["chat_sign_in_code"])
        self.assertIn("123456", code.subject)
        self.assertIn("123456", code.html)
        self.assertIn("123456", code.text)
        reset = render(get_spec("password_reset"), SAMPLE_CONTEXT["password_reset"])
        self.assertIn("reset-password?token=sample", reset.text)
        self.assertIn("password", reset.subject.lower())
        self.assertNotIn("sign in to", reset.text.lower())

    def test_monthly_reminders_keep_their_distinct_copy(self):
        seven = render(get_spec("monthly_update_7d"), SAMPLE_CONTEXT["monthly_update_7d"])
        one = render(get_spec("monthly_update_1d"), SAMPLE_CONTEXT["monthly_update_1d"])
        self.assertIn("another week", seven.text)
        self.assertIn("expires tomorrow", one.text)
        self.assertIn("Example Co", one.html)
        self.assertIn("31 July 2026 at 12:00 PM AEST", seven.text)

    def test_missing_context_names_keys_but_not_values(self):
        with self.assertRaises(SystemEmailContextError) as caught:
            render(get_spec("magic_link"), {"magic_link": "https://secret.example/token"})
        self.assertIn("app_name", str(caught.exception))
        self.assertNotIn("secret.example", str(caught.exception))

    def test_html_escapes_context(self):
        context = dict(SAMPLE_CONTEXT["magic_link"], first_name="<script>x</script>")
        rendered = render(get_spec("magic_link"), context)
        self.assertNotIn("<script>", rendered.html)

    def test_prerendered_requires_subject_and_body(self):
        spec = get_spec("article_review_ready")
        with self.assertRaises(SystemEmailContextError):
            render(spec, None, None)
        copy = RenderedEmail(subject="Ready", html="<p>x</p>", text="x")
        self.assertIs(render(spec, None, copy), copy)

    def test_unknown_key(self):
        with self.assertRaises(UnknownMessageError):
            get_spec("nope")


@override_settings(
    SYSTEM_EMAIL_RESEND_API_KEY="re_test_key",
    MLAI_SYSTEM_FROM_EMAIL=SYSTEM_FROM,
    SYSTEM_EMAIL_REPLY_TO="team@mlai.au",
    SYSTEM_EMAIL_UNSUBSCRIBE_MAILTO="unsubscribe@notify.mlai.au",
)
class TransportTests(SimpleTestCase):
    rendered = RenderedEmail(subject="Hi", html="<p>Hi</p>", text="Hi\n")

    def test_payload_headers_and_unsubscribe(self):
        payload = transport_resend.build_payload(
            to="a@example.com", rendered=self.rendered, message_key="article_review_ready",
            entity_ref="ref-1", unsubscribe=True, unsubscribe_url="https://mlai.au/u?t=1",
        )
        self.assertEqual(payload["from"], SYSTEM_FROM)
        self.assertEqual(payload["to"], ["a@example.com"])
        self.assertEqual(payload["reply_to"], "team@mlai.au")
        self.assertEqual(payload["headers"]["X-Entity-Ref-ID"], "ref-1")
        self.assertIn("<https://mlai.au/u?t=1>", payload["headers"]["List-Unsubscribe"])
        self.assertIn("mailto:unsubscribe@notify.mlai.au", payload["headers"]["List-Unsubscribe"])
        self.assertEqual(payload["headers"]["List-Unsubscribe-Post"], "List-Unsubscribe=One-Click")
        auth = transport_resend.build_payload(
            to="a@example.com", rendered=self.rendered, message_key="magic_link", entity_ref="r",
        )
        self.assertNotIn("List-Unsubscribe", auth["headers"])
        mailto_only = transport_resend.build_payload(
            to="a@example.com", rendered=self.rendered, message_key="monthly_update_7d", entity_ref="r",
            unsubscribe=True,
        )
        self.assertIn("mailto:", mailto_only["headers"]["List-Unsubscribe"])
        self.assertNotIn("List-Unsubscribe-Post", mailto_only["headers"])

    @patch("system_email.transport_resend.http_client.post")
    def test_success_sends_idempotency_key_and_returns_id(self, post):
        post.return_value = response(200, {"id": "abc"})
        self.assertEqual(transport_resend.send({"from": SYSTEM_FROM}, idempotency_key="k1"), "abc")
        headers = post.call_args.kwargs["headers"]
        self.assertEqual(headers["Idempotency-Key"], "k1")
        self.assertEqual(headers["Authorization"], "Bearer re_test_key")
        self.assertEqual(post.call_args.args[0], "https://api.resend.com/emails")

    @patch("system_email.transport_resend.http_client.post")
    def test_retryable_and_rejected_statuses(self, post):
        for status, body in ((429, {}), (500, {}), (408, {}), (409, {"name": "concurrent_idempotent_requests"})):
            post.return_value = response(status, body)
            with self.subTest(status=status), self.assertRaises(transport_resend.SystemEmailRetryable):
                transport_resend.send({"from": SYSTEM_FROM}, idempotency_key="k")
        for status, body in ((422, {"name": "validation_error", "message": "a@example.com is bad"}),
                             (403, {}), (200, {})):
            post.return_value = response(status, body)
            with self.subTest(status=status), self.assertRaises(transport_resend.SystemEmailRejected) as caught:
                transport_resend.send({"from": SYSTEM_FROM}, idempotency_key="k")
            self.assertNotIn("example.com", caught.exception.code)

    @patch("system_email.transport_resend.http_client.post")
    def test_network_error_is_retryable(self, post):
        from integrations import http_client

        post.side_effect = http_client.exceptions.ConnectTimeout("boom")
        with self.assertRaises(transport_resend.SystemEmailRetryable):
            transport_resend.send({"from": SYSTEM_FROM}, idempotency_key="k")

    @override_settings(SYSTEM_EMAIL_RESEND_API_KEY="", RESEND_API_KEY="re_crm_key")
    @patch("system_email.transport_resend.http_client.post")
    def test_never_falls_back_to_crm_key(self, post):
        with self.assertRaises(transport_resend.SystemEmailRejected) as caught:
            transport_resend.send({"from": SYSTEM_FROM}, idempotency_key="k")
        self.assertEqual(caught.exception.code, "system_email_unconfigured")
        post.assert_not_called()


class RoutingTests(SimpleTestCase):
    @override_settings(SYSTEM_EMAIL_RESEND_MESSAGES=())
    def test_default_routes_nothing(self):
        for key in MESSAGES:
            self.assertFalse(api.routes_to_resend(key))

    @override_settings(SYSTEM_EMAIL_RESEND_MESSAGES=("magic_link",))
    def test_listed_key_only(self):
        self.assertTrue(api.routes_to_resend("magic_link"))
        self.assertFalse(api.routes_to_resend("chat_sign_in_code"))

    @override_settings(SYSTEM_EMAIL_RESEND_MESSAGES=("all",))
    def test_all(self):
        self.assertTrue(all(api.routes_to_resend(key) for key in MESSAGES))

    def test_unknown_key_is_an_error(self):
        with self.assertRaises(UnknownMessageError):
            api.routes_to_resend("typo")


def signed(body, stamp=1800000000, msg="msg_1"):
    signature = base64.b64encode(hmac.digest(KEY, f"{msg}.{stamp}.".encode() + body, "sha256")).decode()
    return {"svix-id": msg, "svix-timestamp": str(stamp), "svix-signature": f"v1,{signature}"}


def event(kind="email.bounced", sender=SYSTEM_FROM, bounce="Permanent"):
    return {
        "type": kind,
        "created_at": "2026-11-06T00:00:00.000Z",
        "data": {"email_id": "re_1", "from": sender, "to": ["Person@Example.com"],
                 "bounce": {"type": bounce}},
    }


@override_settings(MLAI_SYSTEM_FROM_EMAIL=SYSTEM_FROM)
class WebhookParseTests(SimpleTestCase):
    def test_signature(self):
        body = json.dumps(event()).encode()
        self.assertTrue(verify_signature(body, signed(body), SECRET, now=1800000000))
        self.assertFalse(verify_signature(body + b" ", signed(body), SECRET, now=1800000000))
        self.assertFalse(verify_signature(body, signed(body), SECRET, now=1800000400))

    def test_only_system_domain_events_are_read(self):
        parsed = parse_event(event())
        self.assertEqual(parsed["recipient"], "person@example.com")
        self.assertEqual(parsed["bounce_type"], "permanent")
        self.assertIsNone(parse_event(event(sender="Sam <hello@mlai-aus.com>")))
        self.assertIsNone(parse_event(event(sender="MLAI <notifications@mlai.au>")))
        self.assertIsNone(parse_event({"type": "contact.created", "data": {}}))

    def test_crm_feedback_ignores_system_domain(self):
        from crm.resend_feedback import parse_event as crm_parse_event

        with override_settings(CRM_RESEND_MONITORED_SENDERS=("hello@mlai-aus.com",)):
            self.assertIsNone(crm_parse_event(event()))


class HealthTests(SimpleTestCase):
    def test_thresholds(self):
        quiet = health.CategoryHealth("auth", sends=4, failures=4, hard_bounces=0, complaints=0,
                                      p95_delivery_seconds=10)
        self.assertEqual(quiet.problems(), [])
        bad = health.CategoryHealth("auth", sends=20, failures=2, hard_bounces=0, complaints=1,
                                    p95_delivery_seconds=300)
        self.assertEqual(len(bad.problems()), 3)
        self.assertEqual(health.p95([1, 2, 3, 4, 100]), 100)
        self.assertIsNone(health.p95([]))


class CallerRoutingTests(SimpleTestCase):
    """Each legacy caller stays on Customer.io until its key is listed."""

    def _result(self):
        return api.SendResult(provider_message_id="re_123", log_id="log-1")

    @override_settings(SYSTEM_EMAIL_RESEND_MESSAGES=("chat_sign_in_code",), COMMUNITY_CHAT_EMAIL_CODE_TTL_SECONDS=600)
    @patch("community_chat.email_delivery.APIClient")
    @patch("system_email.api.send")
    def test_chat_code_routed(self, send, cio):
        from types import SimpleNamespace

        from community_chat.email_delivery import send_community_chat_email_code

        send.return_value = self._result()
        user = SimpleNamespace(id=7, email="a@example.com", first_name="Sam", full_name="Sam D")
        self.assertEqual(send_community_chat_email_code(user, "654321", delivery_id=42), {"id": "re_123"})
        cio.assert_not_called()
        kwargs = send.call_args.kwargs
        self.assertEqual(send.call_args.args[0], "chat_sign_in_code")
        self.assertEqual(kwargs["idempotency_key"], "chat-code/42")
        self.assertEqual(kwargs["context"]["verification_code"], "654321")
        self.assertEqual(kwargs["context"]["expires_minutes"], 10)

    @override_settings(SYSTEM_EMAIL_RESEND_MESSAGES=(), CUSTOMERIO_API_KEY="cio",
                       CUSTOMERIO_COMMUNITY_CHAT_CODE_MESSAGE_ID="mlai_chat_sign_in_code",
                       COMMUNITY_CHAT_EMAIL_CODE_TTL_SECONDS=600)
    @patch("community_chat.email_delivery.APIClient")
    @patch("system_email.api.send")
    def test_chat_code_default_stays_on_customerio(self, send, cio):
        from types import SimpleNamespace

        from community_chat.email_delivery import send_community_chat_email_code

        user = SimpleNamespace(id=7, email="a@example.com", first_name="Sam", full_name="Sam D")
        send_community_chat_email_code(user, "654321", delivery_id=42)
        send.assert_not_called()
        cio.return_value.send_email.assert_called_once()

    @override_settings(SYSTEM_EMAIL_RESEND_MESSAGES=("magic_link", "password_reset"), PASSWORD_RESET_TTL_SECONDS=3600)
    @patch("core.email_utils.APIClient")
    @patch("system_email.api.send")
    def test_magic_link_and_reset_routed(self, send, cio):
        from types import SimpleNamespace

        from core.email_utils import send_magic_link_email, send_password_reset_email

        send.return_value = self._result()
        user = SimpleNamespace(id=3, email="a@example.com", first_name="Sam", full_name="Sam D")
        send_magic_link_email(user, "https://chat.mlai.au/verify-email?token=t", app_context="community-chat")
        self.assertEqual(send.call_args.kwargs["context"]["app_name"], "MLAI Chat")
        self.assertTrue(send.call_args.kwargs["idempotency_key"].startswith("magic/"))
        send_password_reset_email(user, "https://mlai.au/reset?token=t", delivery_id=9)
        self.assertEqual(send.call_args.args[0], "password_reset")
        self.assertEqual(send.call_args.kwargs["idempotency_key"], "pwreset/9")
        self.assertEqual(send.call_args.kwargs["context"]["expires_minutes"], 60)
        cio.assert_not_called()

    @override_settings(SYSTEM_EMAIL_RESEND_MESSAGES=("article_review_ready",), CUSTOMERIO_API_KEY="cio")
    @patch("integrations.services.notification_adapters._send_email_via_customerio")
    @patch("system_email.api.send")
    def test_notification_email_routed_by_key(self, send, cio):
        from types import SimpleNamespace

        from integrations.services.notification_adapters import _send_email

        send.return_value = self._result()
        channel = SimpleNamespace(route_id="a@example.com", user_id=5)
        ok, provider_id, _ = _send_email(channel, subject="S", text="T", html_body="<p>T</p>",
                                         idempotency_key="k", message_key="article_review_ready",
                                         unsubscribe_url="https://mlai.au/u")
        self.assertTrue(ok)
        self.assertEqual(provider_id, "re_123")
        self.assertEqual(send.call_args.kwargs["unsubscribe_url"], "https://mlai.au/u")
        cio.assert_not_called()
        cio.return_value = (True, "cio-1", {})
        _send_email(channel, subject="S", text="T", html_body=None, idempotency_key="k2",
                    message_key="article_content_ready")
        cio.assert_called_once()

    @override_settings(SYSTEM_EMAIL_RESEND_MESSAGES=("article_review_ready",))
    @patch("system_email.api.send")
    def test_notification_failure_reports_code(self, send):
        from types import SimpleNamespace

        from integrations.services.notification_adapters import _send_email

        send.side_effect = transport_resend.SystemEmailRetryable("resend_429")
        ok, _, payload = _send_email(SimpleNamespace(route_id="a@example.com", user_id=None), subject="S",
                                     text="T", html_body=None, idempotency_key="k",
                                     message_key="article_review_ready")
        self.assertFalse(ok)
        self.assertEqual(payload, {"error": "resend_429", "retryable": True})
