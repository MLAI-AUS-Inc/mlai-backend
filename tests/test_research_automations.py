import base64
import hmac
import hashlib
import json
from datetime import datetime, timedelta, timezone as dt_timezone
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
    send_review_ready,
    send_topic_selection,
)
from integrations.services.article_generation import InsufficientRooPointsError
from integrations.services.research_automations import (
    MANUAL_SLOT_BASE,
    STUCK_RUN_TIMEOUT_SECONDS,
    create_or_update_research_automation,
    ensure_due_automation_runs,
    fail_stuck_automation_runs,
    run_research_automation_scheduler,
    start_manual_automation_run,
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
        # mlai.au is the only free-listed domain (billing skipped entirely).
        self.free_org = Organization.objects.create(
            name="MLAI",
            domain="mlai.au",
            competitors=["competitor.example.com"],
            seed_keywords=["automation keyword"],
        )
        self.user = User.objects.create_user(email="writer@example.com")
        self.user.slack_id = "U_WRITER"
        self.user.save(update_fields=["slack_id"])

    @patch("integrations.services.research_automations._require_content_factory_ai_agent_points", return_value=(None, 10))
    @patch("integrations.services.research_automations._post_content_factory_queue_request")
    def test_scheduler_dispatches_due_email_run_with_notification_context_once(self, mock_post, mock_gate):
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
        # Billing actor (wallet owner) threaded as requested_by, NOT as the Slack
        # delivery route. A deferred hold is recorded so topic approval can charge.
        self.assertEqual(payload["requested_by_slack_user_id"], "U_WRITER")
        self.assertNotIn("slack_user_id", payload)
        self.assertEqual(mock_gate.call_args.kwargs["resolved_domain"], "automation.example.com")
        self.assertTrue(
            ContentFactoryJob.objects.filter(job_id="discovery-run-1", billing_status="deferred").exists()
        )
        self.assertEqual(payload["notification_context"]["automation_id"], str(automation.id))
        self.assertEqual(payload["notification_context"]["automation_run_id"], str(run.id))
        self.assertEqual(payload["notification_context"]["channel_type"], "email")
        self.assertEqual(payload["notification_context"]["channel_route_id"], str(automation.notification_channel_id))

    @patch("integrations.services.research_automations._post_content_factory_queue_request")
    def test_scheduler_dispatches_free_domain_run_without_user(self, mock_post):
        # mlai.au is free-listed, so billing is skipped and a channel with no
        # attached user still dispatches. Also guards the
        # select_for_update(of=("self",)) fix: automation.user and
        # notification_channel.user are both NULL, so the dispatch query's
        # select_related spans nullable FKs and an unqualified FOR UPDATE would
        # raise NotSupportedError on Postgres the moment a run is dispatchable.
        mock_post.return_value = _Response(202, {"run_id": "discovery-free-1"})
        automation = create_or_update_research_automation(
            domain="mlai.au",
            channel_type=NotificationChannelType.WHATSAPP,
            route_id="+61401099433",
            user=None,
            timezone_name="Australia/Melbourne",
            frequency_per_day=1,
            local_send_times=["08:00"],
            consent_state=NotificationConsentState.ACTIVE,
        )
        self.assertIsNone(automation.user_id)
        self.assertIsNone(automation.notification_channel.user_id)

        now = datetime(2026, 3, 23, 21, 5, tzinfo=dt_timezone.utc)
        result = run_research_automation_scheduler(now=now)

        self.assertEqual(result["failed"], 0)
        self.assertEqual(result["queued"], 1)
        run = AutomationRun.objects.get(automation=automation)
        self.assertEqual(run.status, AutomationRunStatus.QUEUED)
        self.assertEqual(run.content_factory_run_id, "discovery-free-1")
        payload = mock_post.call_args.kwargs["payload"]
        self.assertEqual(payload["domain"], "mlai.au")
        # Free path: no billing actor required and no deferred hold recorded.
        self.assertNotIn("requested_by_slack_user_id", payload)
        self.assertNotIn("user_email", payload)
        self.assertNotIn("slack_user_id", payload)
        self.assertFalse(ContentFactoryJob.objects.filter(job_id="discovery-free-1").exists())
        self.assertEqual(payload["notification_context"]["channel_type"], "whatsapp")

    @patch("integrations.services.research_automations._post_content_factory_queue_request")
    def test_paying_domain_without_billing_identity_fails(self, mock_post):
        # Paying domain + a channel with no Slack-linked user => no wallet owner.
        # Fail fast with a clear reason instead of letting content-factory return
        # an opaque ROO_POINTS_UNAVAILABLE. This is the theproductbus.com case.
        automation = create_or_update_research_automation(
            domain="automation.example.com",
            channel_type=NotificationChannelType.WHATSAPP,
            route_id="+61466255612",
            user=None,
            timezone_name="Australia/Melbourne",
            frequency_per_day=1,
            local_send_times=["08:00"],
            consent_state=NotificationConsentState.ACTIVE,
        )

        now = datetime(2026, 3, 23, 21, 5, tzinfo=dt_timezone.utc)
        result = run_research_automation_scheduler(now=now)

        self.assertEqual(result["queued"], 0)
        self.assertEqual(result["failed"], 1)
        mock_post.assert_not_called()
        run = AutomationRun.objects.get(automation=automation)
        self.assertEqual(run.status, AutomationRunStatus.FAILED)
        self.assertIn("billing_identity_missing", run.last_error)

    @patch(
        "integrations.services.research_automations._require_content_factory_ai_agent_points",
        side_effect=InsufficientRooPointsError({"message": "This user does not have enough Roo points."}),
    )
    @patch("integrations.services.research_automations._post_content_factory_queue_request")
    def test_paying_domain_insufficient_points_fails(self, mock_post, mock_gate):
        automation = create_or_update_research_automation(
            domain="automation.example.com",
            channel_type=NotificationChannelType.WHATSAPP,
            route_id="+61466255612",
            user=self.user,
            timezone_name="Australia/Melbourne",
            frequency_per_day=1,
            local_send_times=["08:00"],
            consent_state=NotificationConsentState.ACTIVE,
        )

        now = datetime(2026, 3, 23, 21, 5, tzinfo=dt_timezone.utc)
        result = run_research_automation_scheduler(now=now)

        self.assertEqual(result["queued"], 0)
        self.assertEqual(result["failed"], 1)
        # Gate rejects before the discovery POST; no CF request is made.
        mock_post.assert_not_called()
        run = AutomationRun.objects.get(automation=automation)
        self.assertEqual(run.status, AutomationRunStatus.FAILED)
        self.assertIn("insufficient_roo_points", run.last_error)

    def test_watchdog_fails_stuck_machine_waiting_runs_only(self):
        # A dropped content-factory callback strands a run in QUEUED/GENERATING
        # forever (this happened in prod when the web container was recreated
        # mid-flight). The watchdog fails those, but leaves user-waiting states
        # (a founder may approve a topic hours later) and fresh in-flight runs.
        automation = create_or_update_research_automation(
            domain="mlai.au",
            channel_type=NotificationChannelType.WHATSAPP,
            route_id="+61401099433",
            user=None,
            timezone_name="Australia/Melbourne",
            frequency_per_day=1,
            local_send_times=["08:00"],
            consent_state=NotificationConsentState.ACTIVE,
        )
        now = datetime(2026, 3, 23, 12, 0, tzinfo=dt_timezone.utc)
        stale = now - timedelta(seconds=STUCK_RUN_TIMEOUT_SECONDS + 60)
        fresh = now - timedelta(seconds=60)

        def _make_run(status, updated_at, slot):
            run = AutomationRun.objects.create(
                automation=automation,
                scheduled_for_at=stale,
                local_date=stale.date(),
                slot_index=slot,
                status=status,
                idempotency_key=f"watchdog:{slot}",
            )
            # updated_at is auto_now; .update() bypasses it to backdate the row.
            AutomationRun.objects.filter(pk=run.pk).update(updated_at=updated_at)
            return run

        stuck_queued = _make_run(AutomationRunStatus.QUEUED, stale, 1)
        stuck_generating = _make_run(AutomationRunStatus.GENERATING, stale, 2)
        fresh_queued = _make_run(AutomationRunStatus.QUEUED, fresh, 3)
        user_waiting = _make_run(AutomationRunStatus.TOPIC_SELECTION_SENT, stale, 4)

        failed_count = fail_stuck_automation_runs(now=now)

        self.assertEqual(failed_count, 2)
        for run in (stuck_queued, stuck_generating, fresh_queued, user_waiting):
            run.refresh_from_db()
        self.assertEqual(stuck_queued.status, AutomationRunStatus.FAILED)
        self.assertIn("content_factory_timeout", stuck_queued.last_error)
        self.assertEqual(stuck_generating.status, AutomationRunStatus.FAILED)
        self.assertEqual(fresh_queued.status, AutomationRunStatus.QUEUED)
        self.assertEqual(user_waiting.status, AutomationRunStatus.TOPIC_SELECTION_SENT)


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
        review_url = (
            "https://app.test/founder-tools/marketing/runs/article-run-1"
            "?articleStep=review&reviewMode=expanded"
        )
        self.assertIn(review_url, email_payload["text"])
        self.assertIn("Review article", email_payload["html"])
        self.assertIn("Manage notifications", email_payload["html"])
        self.assertNotIn("https://preview.test/run/article-run-1", email_payload["text"])

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

    def _post_message(self, body_text, sender="61400000000", extra_params=None):
        params = {
            "MessageSid": "SM-inbound-1",
            "From": f"whatsapp:+{sender}",
            "WaId": sender,
            "Body": body_text,
        }
        params.update(extra_params or {})
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

    @patch("integrations.services.notification_adapters.send_whatsapp_text", return_value=(True, "SM-out", {}))
    @patch("integrations.services.notification_adapters.confirm_topic")
    def test_quick_reply_button_tap_approves_topic(self, mock_confirm, mock_send_text):
        mock_confirm.return_value = {"run_id": "article-run-10", "status": "queued"}
        run = self._automation_with_pending_run()

        # A quick-reply tap sends the visible title as Body and the button id
        # as ButtonPayload; the payload must win over the non-numeric title.
        response = self._post_message("Topic 2", extra_params={"ButtonPayload": "2", "ButtonText": "Topic 2"})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["approved"], 1)
        run.refresh_from_db()
        self.assertEqual(run.article_content_factory_run_id, "article-run-10")
        self.assertEqual(mock_confirm.call_args.kwargs["confirmed_keyword"], "second topic")

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
    TWILIO_WHATSAPP_REVIEW_CONTENT_SID="HX-review",
    FOUNDER_TOOLS_URL="https://app.test",
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

    @patch("integrations.services.notification_adapters.SlackService.send_dm", return_value=(True, "2.0"))
    @patch("integrations.services.notification_adapters.http_client.post")
    def test_review_ready_fans_out_expanded_review_actions(self, mock_post, mock_dm):
        mock_post.return_value = _Response(200, {"id": "email-review", "sid": "SM-review"})
        data = {
            "event_type": "article_review_ready",
            "job_id": "component-revision-4bb71d2a90703382",
            "domain": "mlai.au",
            "title": "Startup Business Investment Readiness for AI Founders",
            "preview_url": "https://preview.test/raw-preview",
            "notification_context": notification_context_for_run(self.run),
        }

        deliveries = send_review_ready(data)

        self.assertEqual(len(deliveries), 3)
        self.assertTrue(all(delivery.status == NotificationDeliveryStatus.SENT for delivery in deliveries))
        review_url = (
            "https://app.test/founder-tools/marketing/runs/component-revision-4bb71d2a90703382"
            "?articleStep=review&reviewMode=expanded"
        )

        slack_text = mock_dm.call_args.args[1]
        slack_blocks = mock_dm.call_args.kwargs["blocks"]
        self.assertIn(review_url, slack_text)
        self.assertEqual(slack_blocks[1]["elements"][0]["url"], review_url)

        email_call = next(call for call in mock_post.call_args_list if "resend.com" in call.args[0])
        self.assertEqual(email_call.kwargs["json"]["subject"], "Your article is ready to review")
        self.assertIn(
            review_url.replace("&", "&amp;"),
            email_call.kwargs["json"]["html"],
        )
        self.assertNotIn("https://preview.test/raw-preview", email_call.kwargs["json"]["text"])

        whatsapp_call = next(call for call in mock_post.call_args_list if "api.twilio.com" in call.args[0])
        whatsapp_payload = whatsapp_call.kwargs["data"]
        self.assertEqual(whatsapp_payload["ContentSid"], "HX-review")
        self.assertEqual(
            json.loads(whatsapp_payload["ContentVariables"]),
            {
                "1": "Startup Business Investment Readiness for AI Founders",
                "2": "mlai.au",
                "3": review_url,
            },
        )

    @patch("integrations.services.notification_adapters.SlackService.send_dm", return_value=(True, "4.0"))
    @patch("integrations.services.notification_adapters.http_client.post")
    def test_review_ready_recovers_via_article_job_id_without_context(self, mock_post, _mock_dm):
        # content-factory's deferred hosted-preview callback can emit
        # article_review_ready WITHOUT notification_context; the run must still
        # be resolved by its article job id so the review link is delivered.
        # Simulate the real prod state: the sweep already flipped the run to
        # FAILED (content_factory_timeout) before the late callback arrived, so
        # recovery must also clear that stale error.
        mock_post.return_value = _Response(200, {"id": "email-review", "sid": "SM-review"})
        self.run.article_content_factory_run_id = "art-run-123"
        self.run.status = AutomationRunStatus.FAILED
        self.run.last_error = "content_factory_timeout: no terminal callback within the timeout window"
        self.run.save(update_fields=["article_content_factory_run_id", "status", "last_error"])

        deliveries = send_review_ready(
            {
                "event_type": "article_review_ready",
                "job_id": "art-run-123",
                "domain": "mlai.au",
                "title": "Recovered Without Context",
                # deliberately no notification_context
            }
        )

        self.assertEqual(len(deliveries), 3)
        self.assertTrue(all(d.status == NotificationDeliveryStatus.SENT for d in deliveries))
        self.run.refresh_from_db()
        self.assertEqual(self.run.status, AutomationRunStatus.COMPLETED)
        self.assertEqual(self.run.article_content_factory_run_id, "art-run-123")
        self.assertEqual(self.run.last_error, "")
        self.assertTrue(any("api.twilio.com" in call.args[0] for call in mock_post.call_args_list))

    @patch("integrations.services.notification_adapters.SlackService.send_dm", return_value=(True, "5.0"))
    @patch("integrations.services.notification_adapters.http_client.post")
    def test_review_ready_without_context_or_matching_run_skips_and_logs(self, mock_post, _mock_dm):
        mock_post.return_value = _Response(200, {"id": "x", "sid": "y"})

        with self.assertLogs("integrations.services.notification_adapters", level="INFO") as logs:
            deliveries = send_review_ready(
                {
                    "event_type": "article_review_ready",
                    "job_id": "unmatched-run-999",
                    "domain": "mlai.au",
                    "title": "No Home",
                }
            )

        self.assertEqual(deliveries, [])
        self.assertEqual(NotificationDelivery.objects.filter(event_type="review_ready").count(), 0)
        self.assertTrue(any("skipping channel fan-out" in line for line in logs.output))
        mock_post.assert_not_called()

    @patch("integrations.services.notification_adapters.SlackService.send_dm", return_value=(True, "6.0"))
    @patch("integrations.services.notification_adapters.http_client.post")
    def test_review_ready_does_not_overwrite_existing_article_run_id(self, mock_post, _mock_dm):
        # A later revision/child callback can resolve this same run via its
        # inherited notification_context; it must not repoint the automation at
        # the revision run (the prod symptom where an automation's article id
        # became a component-revision-* id).
        mock_post.return_value = _Response(200, {"id": "email-review", "sid": "SM-review"})
        self.run.article_content_factory_run_id = "original-article-run"
        self.run.status = AutomationRunStatus.GENERATING
        self.run.save(update_fields=["article_content_factory_run_id", "status"])

        send_review_ready(
            {
                "event_type": "article_review_ready",
                "job_id": "component-revision-deadbeef",
                "domain": "mlai.au",
                "title": "Revision Draft",
                "notification_context": notification_context_for_run(self.run),
            }
        )

        self.run.refresh_from_db()
        self.assertEqual(self.run.article_content_factory_run_id, "original-article-run")
        self.assertEqual(self.run.status, AutomationRunStatus.COMPLETED)

    @override_settings(TWILIO_WHATSAPP_REVIEW_CONTENT_SID="")
    @patch("integrations.services.notification_adapters.SlackService.send_dm", return_value=(True, "3.0"))
    @patch("integrations.services.notification_adapters.http_client.post")
    def test_review_ready_whatsapp_fallback_includes_stop_copy(self, mock_post, _mock_dm):
        mock_post.return_value = _Response(200, {"id": "email-review", "sid": "SM-review"})
        data = {
            "event_type": "article_review_ready",
            "job_id": "article-run-stop-copy",
            "domain": "mlai.au",
            "title": "Startup Business Investment Readiness for AI Founders",
            "notification_context": notification_context_for_run(self.run),
        }

        send_review_ready(data)

        whatsapp_call = next(call for call in mock_post.call_args_list if "api.twilio.com" in call.args[0])
        review_url = (
            "https://app.test/founder-tools/marketing/runs/article-run-stop-copy"
            "?articleStep=review&reviewMode=expanded"
        )
        self.assertEqual(
            whatsapp_call.kwargs["data"]["Body"],
            (
                "Startup Business Investment Readiness for AI Founders is ready for mlai.au."
                f"\n\nReview and approve: {review_url}"
                "\n\nReply STOP to opt out."
            ),
        )

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


@override_settings(
    TWILIO_ACCOUNT_SID="AC-test",
    TWILIO_AUTH_TOKEN="twilio-auth-token",
    TWILIO_WHATSAPP_FROM="+61480000000",
    TWILIO_WHATSAPP_REVIEW_CONTENT_SID="HX-review",
    FOUNDER_TOOLS_URL="https://app.test",
    ROO_API_KEY="test-roo-key",
    INTERNAL_API_KEY="test-roo-key",
)
class ReviewReadyCallbackFallbackTests(TestCase):
    """The article_review_ready HTTP callback must notify the automation's
    channel even when content-factory omits notification_context — the exact
    prod failure where daily-topics runs completed silently and then timed out.
    """

    def setUp(self):
        self.client = APIClient()
        self.client.credentials(HTTP_X_API_KEY="test-roo-key")
        self.org = Organization.objects.create(name="Review Co", domain="review.example.com")
        self.whatsapp = NotificationChannel.objects.create(
            organization=self.org,
            channel_type=NotificationChannelType.WHATSAPP,
            route_id="+61400000000",
            consent_state=NotificationConsentState.ACTIVE,
        )
        self.automation = ResearchAutomation.objects.create(
            organization=self.org,
            notification_channel=self.whatsapp,
            status=ResearchAutomationStatus.ACTIVE,
        )
        self.run = AutomationRun.objects.create(
            automation=self.automation,
            scheduled_for_at=timezone.now(),
            local_date=timezone.now().date(),
            slot_index=0,
            status=AutomationRunStatus.GENERATING,
            article_content_factory_run_id="review-art-run-77",
            idempotency_key="research-automation:review-fallback:0",
        )

    @patch("integrations.services.notification_adapters.http_client.post")
    def test_context_less_review_ready_notifies_via_job_id(self, mock_post):
        mock_post.return_value = _Response(200, {"sid": "SM-review"})

        response = self.client.post(
            reverse("content_factory_callback"),
            {
                "event_type": "article_review_ready",
                "job_id": "review-art-run-77",
                "run_id": "review-art-run-77",
                "domain": "review.example.com",
                "title": "Context-less Review",
                # no notification_context — mirrors cf's deferred preview callback
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.run.refresh_from_db()
        self.assertEqual(self.run.status, AutomationRunStatus.COMPLETED)
        delivery = NotificationDelivery.objects.get(
            automation_run=self.run, event_type="review_ready"
        )
        self.assertEqual(delivery.status, NotificationDeliveryStatus.SENT)
        self.assertTrue(any("api.twilio.com" in call.args[0] for call in mock_post.call_args_list))

    @patch("content_factory.service_views.ContentFactoryCallbackView._send_job_message")
    @patch("integrations.services.notification_adapters.http_client.post")
    def test_context_less_generation_pr_opened_notifies_via_job_id(self, mock_post, mock_job_msg):
        # The publish_code (PR) terminal outcome has the same deferred-callback
        # gap: generation_pr_opened can arrive without notification_context and
        # must still resolve the automation run by article job id, notify the
        # channel, and suppress the legacy manual Slack job-thread message.
        mock_post.return_value = _Response(200, {"sid": "SM-review"})

        response = self.client.post(
            reverse("content_factory_callback"),
            {
                "event_type": "generation_pr_opened",
                "job_id": "review-art-run-77",
                "run_id": "review-art-run-77",
                "domain": "review.example.com",
                "title": "Context-less PR Opened",
                "pr_url": "https://github.com/acme/site/pull/12",
                "pr_number": 12,
                "review_required": True,
                # no notification_context — mirrors cf's deferred callback
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.run.refresh_from_db()
        self.assertEqual(self.run.status, AutomationRunStatus.COMPLETED)
        delivery = NotificationDelivery.objects.get(
            automation_run=self.run, event_type="review_ready"
        )
        self.assertEqual(delivery.status, NotificationDeliveryStatus.SENT)
        self.assertTrue(any("api.twilio.com" in call.args[0] for call in mock_post.call_args_list))
        # The automation branch handled it — the legacy manual Slack job-thread
        # message must not also fire.
        mock_job_msg.assert_not_called()

    @patch("content_factory.service_views.ContentFactoryCallbackView._send_job_message", return_value=True)
    @patch("integrations.services.notification_adapters.http_client.post")
    def test_generation_pr_opened_without_automation_uses_legacy_slack(self, mock_post, mock_job_msg):
        # A manual/web run (no automation owns this job id) must still take the
        # legacy Slack job-thread path and send no channel review delivery.
        mock_post.return_value = _Response(200, {"sid": "SM-x"})

        response = self.client.post(
            reverse("content_factory_callback"),
            {
                "event_type": "generation_pr_opened",
                "job_id": "manual-run-no-automation",
                "run_id": "manual-run-no-automation",
                "domain": "review.example.com",
                "title": "Manual PR Opened",
                "pr_url": "https://github.com/acme/site/pull/13",
                "pr_number": 13,
                "review_required": True,
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        mock_job_msg.assert_called_once()
        self.assertFalse(
            NotificationDelivery.objects.filter(event_type="review_ready").exists()
        )


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


@override_settings(
    CONTENT_FACTORY_URL="https://content-factory.test",
    CONTENT_FACTORY_API_KEY="cf-key",
    DEFAULT_BACKEND_URL="https://api.test",
)
class ManualRunNowTests(TestCase):
    def setUp(self):
        # mlai.au is free-listed, so dispatch skips billing (no gate mock needed).
        self.org = Organization.objects.create(
            name="MLAI",
            domain="mlai.au",
            competitors=["competitor.example.com"],
            seed_keywords=["automation keyword"],
        )
        self.user = User.objects.create_user(email="founder@mlai.au")

    def _automation_with_channel(self, *, delivery_enabled=True):
        channel = NotificationChannel.objects.create(
            organization=self.org,
            channel_type=NotificationChannelType.WHATSAPP,
            route_id="+61400000000",
            consent_state=NotificationConsentState.ACTIVE,
            delivery_enabled=delivery_enabled,
            user=self.user,
        )
        automation = ResearchAutomation.objects.create(
            organization=self.org,
            notification_channel=channel,
            user=self.user,
            status=ResearchAutomationStatus.ACTIVE,
        )
        return automation, channel

    @patch("integrations.services.research_automations._post_content_factory_queue_request")
    def test_run_now_creates_manual_slot_and_dispatches(self, mock_post):
        mock_post.return_value = _Response(202, {"run_id": "cf-manual-1"})
        automation, _ = self._automation_with_channel()

        result = start_manual_automation_run(self.org, requested_by_user_id=self.user.id)

        self.assertEqual(result["status"], "queued")
        self.assertEqual(mock_post.call_count, 1)
        run = AutomationRun.objects.get(automation=automation)
        self.assertGreaterEqual(run.slot_index, MANUAL_SLOT_BASE)
        self.assertEqual(run.status, AutomationRunStatus.QUEUED)
        self.assertEqual(run.content_factory_run_id, "cf-manual-1")
        # Same pipeline as 8am → top-3 topics requested.
        self.assertEqual(mock_post.call_args.kwargs["payload"]["requested_topic_count"], 3)

    @patch("integrations.services.research_automations._post_content_factory_queue_request")
    def test_run_now_reuses_in_flight_run(self, mock_post):
        mock_post.return_value = _Response(202, {"run_id": "cf-manual-1"})
        self._automation_with_channel()

        first = start_manual_automation_run(self.org, requested_by_user_id=self.user.id)
        second = start_manual_automation_run(self.org, requested_by_user_id=self.user.id)

        self.assertEqual(first["status"], "queued")
        self.assertEqual(second["status"], "reused")
        self.assertEqual(second["automation_run_id"], first["automation_run_id"])
        self.assertEqual(mock_post.call_count, 1)  # no second content-factory discovery
        self.assertEqual(
            AutomationRun.objects.filter(slot_index__gte=MANUAL_SLOT_BASE).count(), 1
        )

    def test_run_now_without_automation_returns_sentinel(self):
        self.assertEqual(start_manual_automation_run(self.org)["status"], "no_automation")

    def test_run_now_without_enabled_channel_returns_sentinel(self):
        self._automation_with_channel(delivery_enabled=False)
        self.assertEqual(
            start_manual_automation_run(self.org)["status"], "no_delivery_channels"
        )

    @patch("integrations.services.research_automations._post_content_factory_queue_request")
    def test_run_now_independent_of_consumed_scheduled_slot(self, mock_post):
        mock_post.return_value = _Response(202, {"run_id": "cf-manual-1"})
        automation, _ = self._automation_with_channel()
        # Today's 8am scheduled slot (index 0) already ran and completed.
        AutomationRun.objects.create(
            automation=automation,
            local_date=timezone.now().date(),
            slot_index=0,
            scheduled_for_at=timezone.now(),
            status=AutomationRunStatus.COMPLETED,
            idempotency_key="scheduled-slot-0",
        )

        result = start_manual_automation_run(self.org)

        self.assertEqual(result["status"], "queued")
        self.assertTrue(
            AutomationRun.objects.filter(
                automation=automation, slot_index__gte=MANUAL_SLOT_BASE
            ).exists()
        )
