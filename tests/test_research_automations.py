import hmac
import hashlib
import json
from datetime import datetime, timezone as dt_timezone
from urllib.parse import parse_qs, urlparse
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.urls import reverse
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
)
from core.models import User
from integrations.services.notification_adapters import (
    build_action_url,
    notification_context_for_run,
)
from integrations.services.research_automations import (
    create_or_update_research_automation,
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
        self.assertNotIn("slack_user_id", payload)
        self.assertEqual(payload["notification_context"]["automation_id"], str(automation.id))
        self.assertEqual(payload["notification_context"]["automation_run_id"], str(run.id))
        self.assertEqual(payload["notification_context"]["channel_type"], "email")
        self.assertEqual(payload["notification_context"]["channel_route_id"], str(automation.notification_channel_id))


@override_settings(
    DEFAULT_BACKEND_URL="https://api.test",
    RESEND_API_KEY="resend-key",
    RESEND_FROM_EMAIL="Roo <roo@example.com>",
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


@override_settings(WHATSAPP_APP_SECRET="test-secret")
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

    def test_stop_reply_opts_out_channel_with_signature_verification(self):
        payload = {
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "messages": [
                                    {"from": "61400000000", "text": {"body": "STOP"}},
                                ]
                            }
                        }
                    ]
                }
            ]
        }
        body = json.dumps(payload).encode()
        signature = "sha256=" + hmac.new(b"test-secret", body, hashlib.sha256).hexdigest()

        response = self.client.post(
            reverse("content_factory_whatsapp_webhook"),
            data=body,
            content_type="application/json",
            HTTP_X_HUB_SIGNATURE_256=signature,
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.channel.refresh_from_db()
        self.assertEqual(self.channel.consent_state, NotificationConsentState.OPTED_OUT)
