"""Configured ops DMs retain provider acknowledgement and replay fences."""
from contextlib import nullcontext
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4

from django.test import SimpleTestCase, override_settings
from django.utils import timezone

from . import founder_journey_alerts as founder, reconciliation
from . import website_operational_alert_delivery as website


DM = "D0123456789"
SLACK = "integrations.services.slack.SlackService.get_client"
CONTEXT = {"org_id": 7, "domain": "fixture.test", "company_id": "", "run_id": "fixture-run",
           "workflow": "direct_generate", "run_status": "failed", "stop_code": "GENERATION_FAILED",
           "step_attempts": 1, "staff_link": ""}


class OpsDirectMessageDeliveryTests(SimpleTestCase):
    def founder_scope(self, response):
        now = timezone.now()
        receipt = {"state": "pending", "alert_id": str(uuid4()),
                   "alert_reason": "run_terminal", "queued_at": now.isoformat()}
        target = SimpleNamespace(receipt=receipt, organization_id=7, key="fixture")
        row, container = object(), {}
        target.lock = lambda: (row, container, deepcopy(target.receipt))
        target.write = lambda unused_row, unused_container, value: setattr(target, "receipt", deepcopy(value))
        client = Mock()
        client.chat_postMessage.return_value = response
        return now, target, client

    @override_settings(CONTENT_FACTORY_OPS_SLACK_CHANNEL_ID=DM)
    def test_founder_dm_acknowledgement_is_saved_and_replay_does_not_send(self):
        now, target, client = self.founder_scope({"ok": True, "ts": "1.2"})
        original_id = target.receipt["alert_id"]
        with patch.object(founder, "_pending_targets", side_effect=lambda: [(target, target.receipt)]), \
                patch.object(founder.transaction, "atomic", side_effect=lambda: nullcontext()), \
                patch.object(founder, "_alert_context", return_value=CONTEXT), \
                patch.object(founder, "_mirror_settled"), patch.object(founder, "_log_alert"), \
                patch(SLACK, return_value=client):
            self.assertEqual(founder._deliver(now=now, limit=1)["sent"], 1)
            self.assertEqual(founder._deliver(now=now, limit=1)["sent"], 0)
        self.assertEqual(client.chat_postMessage.call_count, 1)
        self.assertEqual(client.chat_postMessage.call_args.kwargs["channel"], DM)
        self.assertEqual(client.chat_postMessage.call_args.kwargs["client_msg_id"], original_id)
        self.assertFalse(client.chat_postMessage.call_args.kwargs["unfurl_links"])
        self.assertEqual(target.receipt["state"], "sent")
        self.assertEqual(target.receipt["message_id"], "1.2")
        self.assertEqual(target.receipt["channel_id"], DM)
        self.assertEqual(target.receipt["attempts"], 1)

    @override_settings(CONTENT_FACTORY_OPS_SLACK_CHANNEL_ID=DM)
    def test_unacknowledged_dm_stays_pending_and_honours_retry_lease(self):
        now, target, client = self.founder_scope({"ok": True})
        with patch.object(founder, "_pending_targets", side_effect=lambda: [(target, target.receipt)]), \
                patch.object(founder.transaction, "atomic", side_effect=lambda: nullcontext()), \
                patch.object(founder, "_alert_context", return_value=CONTEXT), \
                patch.object(founder, "_mirror_settled"), patch.object(founder, "_log_alert"), \
                patch(SLACK, return_value=client):
            self.assertEqual(founder._deliver(now=now, limit=1)["undelivered"], 1)
            self.assertEqual(founder._deliver(now=now + timedelta(seconds=1), limit=1)["sent"], 0)
        self.assertEqual(client.chat_postMessage.call_count, 1)
        self.assertEqual(target.receipt["state"], "pending")
        self.assertNotIn("message_id", target.receipt)

    @override_settings(CONTENT_FACTORY_OPS_SLACK_CHANNEL_ID=DM)
    def test_refusal_dm_acknowledgement_is_saved_once(self):
        now = timezone.now()
        run = SimpleNamespace(pk=1, run_id="fixture-run", workflow="article_generation", save=Mock(),
            result={"refusal_alert_reported": now.isoformat(),
                    "authority_refusals": [{"code": "website_connection_changed"}] * 4})
        query = Mock()
        query.get.return_value = run
        query.filter.return_value.first.return_value = run
        with patch.object(reconciliation.transaction, "atomic", side_effect=lambda: nullcontext()), \
                patch.object(reconciliation.ContentFactoryRun.objects, "filter", return_value=[run]), \
                patch.object(reconciliation.ContentFactoryRun.objects, "select_for_update", return_value=query), \
                patch(SLACK) as factory:
            factory.return_value.chat_postMessage.return_value = {"ok": True, "ts": "2.3"}
            self.assertEqual(reconciliation._deliver_pending_refusal_alerts(limit=1, now=now), 1)
            self.assertEqual(reconciliation._deliver_pending_refusal_alerts(limit=1, now=now), 0)
        self.assertEqual(factory.return_value.chat_postMessage.call_count, 1)
        self.assertEqual(factory.return_value.chat_postMessage.call_args.kwargs["channel"], DM)
        self.assertEqual(run.result["refusal_alert_delivery"]["message_id"], "2.3")

    def test_website_dm_and_existing_channels_are_configured_but_user_ids_are_not(self):
        for destination in (DM, "C0123456789", "G0123456789"):
            with self.subTest(destination=destination), override_settings(CONTENT_FACTORY_OPS_SLACK_CHANNEL_ID=destination):
                self.assertEqual(website.channel(), destination)
                self.assertTrue(founder.SLACK_CHANNEL_PATTERN.fullmatch(destination))
        for destination in ("", "U0123456789", "#ops", "D123", "D0123456789/extra", "https://example.test"):
            with self.subTest(destination=destination), override_settings(CONTENT_FACTORY_OPS_SLACK_CHANNEL_ID=destination), \
                    patch(SLACK) as factory:
                self.assertIsNone(website.channel())
                self.assertFalse(founder.SLACK_CHANNEL_PATTERN.fullmatch(destination))
                self.assertEqual(reconciliation._deliver_pending_refusal_alerts(limit=1, now=timezone.now()), 0)
                factory.assert_not_called()
