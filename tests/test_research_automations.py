import base64
import hmac
import hashlib
import json
from datetime import datetime, timezone as dt_timezone
from urllib.parse import parse_qs, urlparse
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from content_factory.models import (
    AutomationRun,
    AutomationRunStatus,
    ContentFactoryJob,
    NotificationChannel,
    NotificationChannelType,
    NotificationConsentState,
    NotificationDelivery,
    NotificationDeliveryStatus,
    OrganizationContentConfig,
    ResearchAutomation,
    ResearchAutomationStatus,
)
from core.models import User
from integrations.services.notification_adapters import (
    build_action_url,
    notification_context_for_run,
    send_topic_selection,
)
from integrations.services.research_automations import (
    create_or_update_research_automation,
    ensure_due_automation_runs,
    run_research_automation_scheduler,
)
from organizations.models import Organization


class _Response:
    def __init__(self, status_code=202, payload=None):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = json.dumps(self._payload)

    def json(self):
        return self._payload


@override_settings(
    CONTENT_FACTORY_URL="https://content-factory.test",
    CONTENT_FACTORY_API_KEY="cf-key",
    DEFAULT_BACKEND_URL="https://api.test",
)
class ResearchAutomationSchedulerTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(
            name="Automation Co",
            domain="automation.example.com",
            competitors=["competitor.example.com"],
            seed_keywords=["automation keyword"],
        )
        self.user = User.objects.create_user(email="writer@example.com")

    @patch("integrations.services.research_automations._post_content_factory_queue_request")
    def test_scheduler_dispatches_due_email_run_with_notification_context_once(self, mock_post):
        mock_post.return_value = _Response(202, {"run_id": "discovery-run-1"})
        automation = create_or_update_research_automation(
            domain="automation.example.com",
            channel_type=NotificationChannelType.EMAIL,
            route_id="writer@example.com",
            user=self.user,
            timezone_name="Australia/Melbourne",
            frequency_per_day=2,
            local_send_times=["08:00", "16:00"],
            consent_state=NotificationConsentState.ACTIVE,
        )

        now = datetime(2026, 3, 23, 21, 5, tzinfo=dt_timezone.utc)
        first = run_research_automation_scheduler(now=now)
        second = run_research_automation_scheduler(now=now)

        self.assertEqual(first["queued"], 1)
        self.assertEqual(second["queued"], 0)
        self.assertEqual(mock_post.call_count, 1)

        run = AutomationRun.objects.get(automation=automation)
        self.assertEqual(run.status, AutomationRunStatus.QUEUED)
        self.assertEqual(run.content_factory_run_id, "discovery-run-1")
        payload = mock_post.call_args.kwargs["payload"]
        self.assertEqual(payload["domain"], "automation.example.com")
        self.assertEqual(payload["user_email"], "writer@example.com")
        self.assertEqual(payload["requested_topic_count"], 3)
        self.assertNotIn("slack_user_id", payload)
        self.assertEqual(payload["notification_context"]["automation_id"], str(automation.id))
        self.assertEqual(payload["notification_context"]["automation_run_id"], str(run.id))
        self.assertEqual(payload["notification_context"]["channel_type"], "email")
        self.assertEqual(payload["notification_context"]["channel_route_id"], str(automation.notification_channel_id))


@override_settings(
    DEFAULT_BACKEND_URL="https://api.test",
    RESEND_API_KEY="resend-key",
    RESEND_FROM_EMAIL="Roo <roo@example.com>",
    CUSTOMERIO_API_KEY="",
    FOUNDER_TOOLS_URL="https://app.test",
)
class ResearchAutomationCallbackTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.api_key = "test-roo-key"
        self.client.credentials(HTTP_X_API_KEY=self.api_key)
        self.override = override_settings(ROO_API_KEY=self.api_key, INTERNAL_API_KEY=self.api_key)
        self.override.enable()
        self.org = Organization.objects.create(
            name="Callback Co",
            domain="callback.example.com",
            competitors=["competitor.example.com"],
            seed_keywords=["callback keyword"],
        )
        self.user = User.objects.create_user(email="writer@example.com")
        self.automation = create_or_update_research_automation(
            domain="callback.example.com",
            channel_type=NotificationChannelType.EMAIL,
            route_id="writer@example.com",
            user=self.user,
            timezone_name="Australia/Melbourne",
            frequency_per_day=1,
            local_send_times=["08:00"],
            consent_state=NotificationConsentState.ACTIVE,
        )
        self.run = AutomationRun.objects.create(
            automation=self.automation,
            scheduled_for_at=datetime(2026, 3, 23, 21, 0, tzinfo=dt_timezone.utc),
            local_date=datetime(2026, 3, 24, tzinfo=dt_timezone.utc).date(),
            slot_index=0,
            status=AutomationRunStatus.QUEUED,
            content_factory_run_id="discovery-run-1",
            idempotency_key="research-automation:test:2026-03-24:0",
            request_payload={
                "domain": "callback.example.com",
                "user_email": "writer@example.com",
                "notification_context": {},
            },
        )

    def tearDown(self):
        self.override.disable()

    @patch("integrations.services.notification_adapters.http_client.post")
    def test_topic_selection_callback_routes_to_email_adapter(self, mock_post):
        mock_post.return_value = _Response(200, {"id": "email-1"})

        response = self.client.post(
            reverse("content_factory_callback"),
            {
                "event_type": "topic_selection",
                "job_id": "discovery-run-1",
                "domain": "callback.example.com",
                "notification_context": notification_context_for_run(self.run),
                "selection": {
                    "selected_keyword": "automation ideas",
                    "options": [
                        {
                            "keyword": "automation ideas",
                            "suggested_title": "Automation Ideas",
                            "opportunity_index": 91,
                        }
                    ],
                },
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.run.refresh_from_db()
        self.assertEqual(self.run.status, AutomationRunStatus.TOPIC_SELECTION_SENT)
        delivery = NotificationDelivery.objects.get(automation_run=self.run, event_type="topic_selection")
        self.assertEqual(delivery.status, NotificationDeliveryStatus.SENT)
        self.assertEqual(delivery.provider_message_id, "email-1")
        job = ContentFactoryJob.objects.get(job_id="discovery-run-1")
        self.assertEqual(job.request_meta["notification_context"]["automation_run_id"], str(self.run.id))
        self.assertEqual(job.request_meta["user_email"], "writer@example.com")
        email_payload = mock_post.call_args.kwargs["json"]
        self.assertEqual(email_payload["to"], ["writer@example.com"])
        self.assertIn("Approve this topic", email_payload["html"])

    @patch("integrations.services.notification_adapters.confirm_topic")
    def test_signed_approval_queues_article_with_same_notification_context(self, mock_confirm_topic):
        mock_confirm_topic.return_value = {"run_id": "article-run-1", "status": "queued"}
        self.run.callback_payload = {
            "job_id": "discovery-run-1",
            "domain": "callback.example.com",
            "selection": {
                "options": [
                    {"keyword": "first topic", "suggested_title": "First Topic"},
                    {"keyword": "second topic", "suggested_title": "Second Topic"},
                ]
            },
        }
        self.run.save(update_fields=["callback_payload"])
        ContentFactoryJob.objects.create(
            job_id="discovery-run-1",
            domain="callback.example.com",
            slack_user_id="",
            status="awaiting_confirmation",
            request_meta={
                "trigger_source": "research_automation",
                "user_email": "writer@example.com",
                "notification_context": notification_context_for_run(self.run),
            },
        )
        token = parse_qs(urlparse(build_action_url(self.run, "approve_topic", option_index=1)).query)["token"][0]

        response = self.client.get(reverse("content_factory_automation_action"), {"token": token})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.run.refresh_from_db()
        self.assertEqual(self.run.article_content_factory_run_id, "article-run-1")
        kwargs = mock_confirm_topic.call_args.kwargs
        self.assertEqual(kwargs["confirmed_keyword"], "second topic")
        self.assertEqual(kwargs["source_run_id"], "discovery-run-1")
        self.assertEqual(kwargs["notification_context"], notification_context_for_run(self.run))

    @patch("integrations.services.notification_adapters.http_client.post")
    def test_article_review_ready_callback_fans_out_review_link(self, mock_post):
        mock_post.return_value = _Response(200, {"id": "email-2"})

        response = self.client.post(
            reverse("content_factory_callback"),
            {
                "event_type": "article_review_ready",
                "job_id": "article-run-1",
                "domain": "callback.example.com",
                "title": "First Topic",
                "preview_url": "https://preview.test/run/article-run-1",
                "notification_context": notification_context_for_run(self.run),
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.run.refresh_from_db()
        self.assertEqual(self.run.status, AutomationRunStatus.COMPLETED)
        self.assertEqual(self.run.article_content_factory_run_id, "article-run-1")
        delivery = NotificationDelivery.objects.get(automation_run=self.run, event_type="review_ready")
        self.assertEqual(delivery.status, NotificationDeliveryStatus.SENT)
        email_payload = mock_post.call_args.kwargs["json"]
        self.assertIn(
            "https://app.test/founder-tools/marketing/runs/article-run-1",
            email_payload["text"],
        )
        self.assertIn("https://preview.test/run/article-run-1", email_payload["text"])

    @patch("integrations.services.notification_adapters.http_client.post")
    def test_content_ready_includes_review_link_and_skips_relative_pr_path(self, mock_post):
        mock_post.return_value = _Response(200, {"id": "email-3"})
        from integrations.services.notification_adapters import send_content_ready

        deliveries = send_content_ready(
            {
                "event_type": "content_ready",
                "job_id": "article-run-2",
                "domain": "callback.example.com",
                "title": "First Topic",
                "publish_pr_url": "/api/runs/article-run-2/publish-pr",
                "notification_context": notification_context_for_run(self.run),
            }
        )

        self.assertEqual(len(deliveries), 1)
        self.assertEqual(deliveries[0].status, NotificationDeliveryStatus.SENT)
        text = mock_post.call_args.kwargs["json"]["text"]
        self.assertIn("https://app.test/founder-tools/marketing/runs/article-run-2", text)
        self.assertNotIn("Pull request", text)
        self.run.refresh_from_db()
        self.assertEqual(self.run.status, AutomationRunStatus.COMPLETED)

    @patch("integrations.services.notification_adapters.set_article_delivery_mode")
    @patch("integrations.services.notification_adapters.confirm_topic")
    def test_approval_auto_resolves_delivery_mode_when_awaiting(self, mock_confirm, mock_set_mode):
        mock_confirm.return_value = {"run_id": "article-run-7", "status": "awaiting_delivery_mode"}
        mock_set_mode.return_value = {"status": "queued", "delivery_mode": "review_draft"}
        self.run.callback_payload = {
            "job_id": "discovery-run-1",
            "domain": "callback.example.com",
            "selection": {
                "options": [
                    {"keyword": "first topic", "suggested_title": "First Topic"},
                ]
            },
        }
        self.run.save(update_fields=["callback_payload"])
        token = parse_qs(urlparse(build_action_url(self.run, "approve_topic", option_index=0)).query)["token"][0]

        response = self.client.get(reverse("content_factory_automation_action"), {"token": token})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        mock_set_mode.assert_called_once_with("article-run-7")
        self.run.refresh_from_db()
        self.assertEqual(self.run.status, AutomationRunStatus.GENERATING)
        self.assertEqual(self.run.selected_delivery_mode, "review_draft")
        self.assertEqual(self.run.article_content_factory_run_id, "article-run-7")

    def test_stale_delivery_mode_prompt_is_suppressed(self):
        from integrations.services.notification_adapters import send_delivery_mode_required

        self.run.selected_delivery_mode = "review_draft"
        self.run.status = AutomationRunStatus.GENERATING
        self.run.save(update_fields=["selected_delivery_mode", "status"])

        deliveries = send_delivery_mode_required(
            {
                "event_type": "delivery_mode_required",
                "job_id": "article-run-1",
                "domain": "callback.example.com",
                "notification_context": notification_context_for_run(self.run),
            }
        )

        self.assertEqual(deliveries, [])
        self.assertFalse(
            NotificationDelivery.objects.filter(
                automation_run=self.run, event_type="delivery_mode_required"
            ).exists()
        )
        self.run.refresh_from_db()
        self.assertEqual(self.run.status, AutomationRunStatus.GENERATING)


@override_settings(TWILIO_AUTH_TOKEN="twilio-auth-token", DEFAULT_BACKEND_URL="https://api.test")
class WhatsAppAutomationWebhookTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.org = Organization.objects.create(name="WhatsApp Co", domain="wa.example.com")
        self.channel = NotificationChannel.objects.create(
            organization=self.org,
            channel_type=NotificationChannelType.WHATSAPP,
            route_id="+61400000000",
            consent_state=NotificationConsentState.ACTIVE,
        )

    def _post_message(self, body_text, sender="61400000000"):
        params = {
            "MessageSid": "SM-inbound-1",
            "From": f"whatsapp:+{sender}",
            "WaId": sender,
            "Body": body_text,
        }
        # Twilio signs the configured URL plus form params in key-sorted order.
        signed = "https://api.test" + reverse("content_factory_whatsapp_webhook")
        signed += "".join(key + params[key] for key in sorted(params))
        signature = base64.b64encode(
            hmac.new(b"twilio-auth-token", signed.encode(), hashlib.sha1).digest()
        ).decode()
        return self.client.post(
            reverse("content_factory_whatsapp_webhook"),
            data=params,
            HTTP_X_TWILIO_SIGNATURE=signature,
        )

    def test_stop_reply_opts_out_channel_with_signature_verification(self):
        response = self._post_message("STOP")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.channel.refresh_from_db()
        self.assertEqual(self.channel.consent_state, NotificationConsentState.OPTED_OUT)

    def test_bad_signature_rejected(self):
        response = self.client.post(
            reverse("content_factory_whatsapp_webhook"),
            data={"From": "whatsapp:+61400000000", "WaId": "61400000000", "Body": "STOP"},
            HTTP_X_TWILIO_SIGNATURE="not-a-real-signature",
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.channel.refresh_from_db()
        self.assertEqual(self.channel.consent_state, NotificationConsentState.ACTIVE)

    def test_stop_pauses_automation_when_last_active_channel(self):
        automation = ResearchAutomation.objects.create(
            organization=self.org,
            notification_channel=self.channel,
            status=ResearchAutomationStatus.ACTIVE,
        )
        config, _ = OrganizationContentConfig.objects.get_or_create(organization=self.org)
        config.daily_discovery_enabled = True
        config.save(update_fields=["daily_discovery_enabled"])

        response = self._post_message("STOP")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        automation.refresh_from_db()
        self.assertEqual(automation.status, ResearchAutomationStatus.PAUSED)
        config.refresh_from_db()
        self.assertFalse(config.daily_discovery_enabled)

    def _automation_with_pending_run(self):
        automation = ResearchAutomation.objects.create(
            organization=self.org,
            notification_channel=self.channel,
            status=ResearchAutomationStatus.ACTIVE,
        )
        return AutomationRun.objects.create(
            automation=automation,
            scheduled_for_at=timezone.now(),
            local_date=timezone.now().date(),
            slot_index=0,
            status=AutomationRunStatus.TOPIC_SELECTION_SENT,
            content_factory_run_id="discovery-run-9",
            idempotency_key="research-automation:webhook:0",
            callback_payload={
                "job_id": "discovery-run-9",
                "domain": "wa.example.com",
                "selection": {
                    "options": [
                        {"keyword": "first topic", "suggested_title": "First Topic"},
                        {"keyword": "second topic", "suggested_title": "Second Topic"},
                    ]
                },
            },
        )

    @patch("integrations.services.notification_adapters.send_whatsapp_text", return_value=(True, "wamid", {}))
    @patch("integrations.services.notification_adapters.confirm_topic")
    def test_numeric_reply_approves_topic(self, mock_confirm, mock_send_text):
        mock_confirm.return_value = {"run_id": "article-run-9", "status": "queued"}
        run = self._automation_with_pending_run()

        response = self._post_message("2")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["approved"], 1)
        run.refresh_from_db()
        self.assertEqual(run.article_content_factory_run_id, "article-run-9")
        self.assertEqual(mock_confirm.call_args.kwargs["confirmed_keyword"], "second topic")
        confirmation_text = mock_send_text.call_args.args[1]
        self.assertIn("Second Topic", confirmation_text)

        # A duplicate reply finds no pending run (status moved on) and only replies.
        followup = self._post_message("2")
        self.assertEqual(followup.data["approved"], 0)
        self.assertEqual(mock_confirm.call_count, 1)

    @patch("integrations.services.notification_adapters.send_whatsapp_text", return_value=(True, "wamid", {}))
    def test_numeric_reply_without_pending_run_gets_polite_reply(self, mock_send_text):
        response = self._post_message("1")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["approved"], 0)
        self.assertEqual(response.data["replied"], 1)
        self.assertIn("no topic selection waiting", mock_send_text.call_args.args[1])

    @patch("integrations.services.notification_adapters.send_whatsapp_text", return_value=(True, "wamid", {}))
    def test_non_command_text_from_known_sender_gets_help_reply(self, mock_send_text):
        response = self._post_message("hello?")

        self.assertEqual(response.data["replied"], 1)
        self.assertIn("Reply 1-3", mock_send_text.call_args.args[1])

    @patch("integrations.services.notification_adapters.send_whatsapp_text")
    def test_unknown_sender_gets_no_reply(self, mock_send_text):
        response = self._post_message("1", sender="61499999999")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["replied"], 0)
        mock_send_text.assert_not_called()


@override_settings(
    DEFAULT_BACKEND_URL="https://api.test",
    RESEND_API_KEY="resend-key",
    CUSTOMERIO_API_KEY="",
    TWILIO_ACCOUNT_SID="AC-test",
    TWILIO_AUTH_TOKEN="twilio-auth-token",
    TWILIO_WHATSAPP_FROM="+61480000000",
    TWILIO_WHATSAPP_TOPIC_CONTENT_SID="HX-topic",
)
class FanOutDeliveryTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="FanOut Co", domain="fanout.example.com")
        self.user = User.objects.create_user(email="writer@example.com")
        self.slack = NotificationChannel.objects.create(
            organization=self.org,
            channel_type=NotificationChannelType.SLACK,
            route_id="U123",
            consent_state=NotificationConsentState.ACTIVE,
        )
        self.email = NotificationChannel.objects.create(
            organization=self.org,
            channel_type=NotificationChannelType.EMAIL,
            route_id="writer@example.com",
            consent_state=NotificationConsentState.ACTIVE,
        )
        self.whatsapp = NotificationChannel.objects.create(
            organization=self.org,
            channel_type=NotificationChannelType.WHATSAPP,
            route_id="+61400000000",
            consent_state=NotificationConsentState.ACTIVE,
        )
        self.automation = ResearchAutomation.objects.create(
            organization=self.org,
            notification_channel=self.slack,
            status=ResearchAutomationStatus.ACTIVE,
        )
        self.run = AutomationRun.objects.create(
            automation=self.automation,
            scheduled_for_at=timezone.now(),
            local_date=timezone.now().date(),
            slot_index=0,
            status=AutomationRunStatus.QUEUED,
            idempotency_key="research-automation:fanout:0",
        )
        self.callback_data = {
            "event_type": "topic_selection",
            "job_id": "discovery-run-5",
            "domain": "fanout.example.com",
            "notification_context": notification_context_for_run(self.run),
            "selection": {
                "options": [
                    {"keyword": "topic one", "suggested_title": "Topic One"},
                    {"keyword": "topic two", "suggested_title": "Topic Two"},
                ]
            },
        }

    @patch("integrations.services.notification_adapters.SlackService.send_dm", return_value=(True, "1.0"))
    @patch("integrations.services.notification_adapters.http_client.post")
    def test_topic_selection_fans_out_to_all_active_channels(self, mock_post, mock_dm):
        mock_post.return_value = _Response(200, {"id": "email-1", "sid": "SM-1"})

        deliveries = send_topic_selection(self.callback_data)

        self.assertEqual(len(deliveries), 3)
        keys = {delivery.idempotency_key for delivery in deliveries}
        self.assertEqual(
            keys,
            {
                f"{self.run.id}:{self.slack.id}:topic_selection",
                f"{self.run.id}:{self.email.id}:topic_selection",
                f"{self.run.id}:{self.whatsapp.id}:topic_selection",
            },
        )
        self.assertTrue(all(d.status == NotificationDeliveryStatus.SENT for d in deliveries))
        mock_dm.assert_called_once()

        urls = [call.args[0] for call in mock_post.call_args_list]
        self.assertTrue(any("resend.com" in url for url in urls))
        whatsapp_calls = [
            call for call in mock_post.call_args_list if "api.twilio.com" in call.args[0]
        ]
        self.assertEqual(len(whatsapp_calls), 1)
        self.assertEqual(whatsapp_calls[0].kwargs["auth"], ("AC-test", "twilio-auth-token"))
        payload = whatsapp_calls[0].kwargs["data"]
        self.assertEqual(payload["From"], "whatsapp:+61480000000")
        self.assertEqual(payload["To"], "whatsapp:+61400000000")
        self.assertEqual(payload["ContentSid"], "HX-topic")
        variables = json.loads(payload["ContentVariables"])
        self.assertEqual(
            variables,
            {"1": "fanout.example.com", "2": "Topic One", "3": "Topic Two", "4": "-"},
        )

        # Re-delivering the same callback sends nothing new.
        repeat = send_topic_selection(self.callback_data)
        self.assertEqual(len(repeat), 3)
        self.assertEqual(mock_dm.call_count, 1)
        self.assertEqual(len(mock_post.call_args_list), 2)

    @override_settings(CUSTOMERIO_API_KEY="cio-key")
    @patch("integrations.services.notification_adapters.SlackService.send_dm", return_value=(True, "1.0"))
    @patch("integrations.services.notification_adapters._customerio_client")
    @patch("integrations.services.notification_adapters.http_client.post")
    def test_topic_email_routes_via_customerio_when_configured(self, mock_post, mock_cio, mock_dm):
        from unittest.mock import MagicMock

        mock_post.return_value = _Response(200, {"sid": "SM-1"})
        client = MagicMock()
        client.send_email.return_value = {"delivery_id": "dl-9"}
        mock_cio.return_value = client
        self.email.user = self.user
        self.email.save(update_fields=["user"])

        deliveries = send_topic_selection(self.callback_data)

        email_delivery = next(d for d in deliveries if d.channel_id == self.email.id)
        self.assertEqual(email_delivery.status, NotificationDeliveryStatus.SENT)
        self.assertEqual(email_delivery.provider_message_id, "dl-9")
        request_body = client.send_email.call_args.args[0]
        self.assertEqual(request_body["to"], "writer@example.com")
        self.assertEqual(request_body["identifiers"], {"id": str(self.user.id)})
        self.assertIn("Approve this topic", request_body["body"])
        # Resend was never used for the email channel; only WhatsApp hit http_client.
        self.assertFalse(any("resend.com" in call.args[0] for call in mock_post.call_args_list))

    @override_settings(CUSTOMERIO_API_KEY="cio-key", CUSTOMERIO_TOPIC_TEMPLATE_ID="tmpl-77")
    @patch("integrations.services.notification_adapters.SlackService.send_dm", return_value=(True, "1.0"))
    @patch("integrations.services.notification_adapters._customerio_client")
    @patch("integrations.services.notification_adapters.http_client.post")
    def test_topic_email_renders_through_customerio_template(self, mock_post, mock_cio, mock_dm):
        from unittest.mock import MagicMock

        mock_post.return_value = _Response(200, {"sid": "SM-1"})
        client = MagicMock()
        client.send_email.return_value = {"delivery_id": "dl-template"}
        mock_cio.return_value = client
        self.email.user = self.user
        self.email.save(update_fields=["user"])

        deliveries = send_topic_selection(self.callback_data)

        email_delivery = next(d for d in deliveries if d.channel_id == self.email.id)
        self.assertEqual(email_delivery.status, NotificationDeliveryStatus.SENT)
        self.assertEqual(email_delivery.provider_message_id, "dl-template")

        request_body = client.send_email.call_args.args[0]
        # Template branch: render through Customer.io, not a raw HTML body.
        self.assertEqual(request_body["transactional_message_id"], "tmpl-77")
        self.assertNotIn("body", request_body)
        self.assertNotIn("subject", request_body)
        self.assertEqual(request_body["to"], "writer@example.com")
        self.assertEqual(request_body["identifiers"], {"id": str(self.user.id)})

        message_data = request_body["message_data"]
        self.assertEqual(message_data["domain"], "fanout.example.com")
        topics = message_data["topics"]
        self.assertEqual(len(topics), 2)
        self.assertEqual(topics[0]["display_title"], "Topic One")
        self.assertEqual(topics[0]["rank"], 1)
        # Each topic carries its own signed one-click approve URL.
        self.assertIn("token=", topics[0]["confirm_url"])
        self.assertNotEqual(topics[0]["confirm_url"], topics[1]["confirm_url"])

    @patch("integrations.services.notification_adapters.SlackService.send_dm", return_value=(True, "1.0"))
    @patch("integrations.services.notification_adapters.http_client.post")
    def test_opted_out_channel_is_skipped(self, mock_post, mock_dm):
        mock_post.return_value = _Response(200, {"id": "email-1", "sid": "SM-1"})
        self.whatsapp.consent_state = NotificationConsentState.OPTED_OUT
        self.whatsapp.save(update_fields=["consent_state"])

        deliveries = send_topic_selection(self.callback_data)

        self.assertEqual(len(deliveries), 2)
        self.assertFalse(
            NotificationDelivery.objects.filter(channel=self.whatsapp).exists()
        )

    @patch("integrations.services.notification_adapters.SlackService.send_dm", return_value=(True, "1.0"))
    @patch("integrations.services.notification_adapters.http_client.post")
    def test_per_channel_unsubscribe_only_opts_out_that_channel(self, mock_post, mock_dm):
        mock_post.return_value = _Response(200, {"id": "email-1", "sid": "SM-1"})
        send_topic_selection(self.callback_data)

        client = APIClient()
        token = parse_qs(
            urlparse(build_action_url(self.run, "unsubscribe", channel_id=str(self.email.id))).query
        )["token"][0]
        response = client.get(reverse("content_factory_automation_action"), {"token": token})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.email.refresh_from_db()
        self.slack.refresh_from_db()
        self.assertEqual(self.email.consent_state, NotificationConsentState.OPTED_OUT)
        self.assertEqual(self.slack.consent_state, NotificationConsentState.ACTIVE)
        self.automation.refresh_from_db()
        self.assertEqual(self.automation.status, ResearchAutomationStatus.ACTIVE)

        # Legacy token without channel_id targets the primary channel; once the
        # last two channels go, the automation pauses.
        legacy_token = parse_qs(urlparse(build_action_url(self.run, "unsubscribe")).query)["token"][0]
        client.get(reverse("content_factory_automation_action"), {"token": legacy_token})
        self.slack.refresh_from_db()
        self.assertEqual(self.slack.consent_state, NotificationConsentState.OPTED_OUT)
        self.automation.refresh_from_db()
        self.assertEqual(self.automation.status, ResearchAutomationStatus.ACTIVE)

        whatsapp_token = parse_qs(
            urlparse(build_action_url(self.run, "unsubscribe", channel_id=str(self.whatsapp.id))).query
        )["token"][0]
        client.get(reverse("content_factory_automation_action"), {"token": whatsapp_token})
        self.automation.refresh_from_db()
        self.assertEqual(self.automation.status, ResearchAutomationStatus.PAUSED)


class SchedulerChannelFilterTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Filter Co", domain="filter.example.com")

    def test_runs_fire_when_primary_opted_out_but_other_channel_active(self):
        slack = NotificationChannel.objects.create(
            organization=self.org,
            channel_type=NotificationChannelType.SLACK,
            route_id="U1",
            consent_state=NotificationConsentState.OPTED_OUT,
        )
        NotificationChannel.objects.create(
            organization=self.org,
            channel_type=NotificationChannelType.EMAIL,
            route_id="writer@example.com",
            consent_state=NotificationConsentState.ACTIVE,
        )
        automation = ResearchAutomation.objects.create(
            organization=self.org,
            notification_channel=slack,
            status=ResearchAutomationStatus.ACTIVE,
            timezone="Australia/Melbourne",
            local_send_times=["08:00"],
        )

        now = datetime(2026, 3, 23, 21, 5, tzinfo=dt_timezone.utc)
        runs = ensure_due_automation_runs(now=now)
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0].automation_id, automation.id)

    def test_no_runs_when_no_active_channels(self):
        slack = NotificationChannel.objects.create(
            organization=self.org,
            channel_type=NotificationChannelType.SLACK,
            route_id="U1",
            consent_state=NotificationConsentState.OPTED_OUT,
        )
        ResearchAutomation.objects.create(
            organization=self.org,
            notification_channel=slack,
            status=ResearchAutomationStatus.ACTIVE,
            timezone="Australia/Melbourne",
            local_send_times=["08:00"],
        )

        now = datetime(2026, 3, 23, 21, 5, tzinfo=dt_timezone.utc)
        self.assertEqual(ensure_due_automation_runs(now=now), [])


class LegacyDailyDiscoveryGateTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(
            name="Legacy Co",
            domain="legacy.example.com",
            competitors=["competitor.example.com"],
        )
        self.config, _ = OrganizationContentConfig.objects.get_or_create(organization=self.org)
        self.config.daily_discovery_enabled = True
        self.config.connected_slack_user_id = "U777"
        self.config.scan_summary = "scan complete"
        self.config.save()
        self.channel = NotificationChannel.objects.create(
            organization=self.org,
            channel_type=NotificationChannelType.SLACK,
            route_id="U777",
            consent_state=NotificationConsentState.ACTIVE,
        )

    def test_org_with_active_automation_excluded_from_legacy_targets(self):
        from integrations.services.daily_discovery import _eligible_daily_discovery_targets

        automation = ResearchAutomation.objects.create(
            organization=self.org,
            notification_channel=self.channel,
            status=ResearchAutomationStatus.ACTIVE,
        )

        targets, stats = _eligible_daily_discovery_targets()
        self.assertEqual(stats["skipped_active_automation"], 1)
        self.assertNotIn("legacy.example.com", [target.domain for target in targets])

        automation.status = ResearchAutomationStatus.PAUSED
        automation.save(update_fields=["status"])
        targets, stats = _eligible_daily_discovery_targets()
        self.assertEqual(stats["skipped_active_automation"], 0)
        self.assertIn("legacy.example.com", [target.domain for target in targets])
