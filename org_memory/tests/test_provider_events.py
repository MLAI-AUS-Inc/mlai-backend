import base64
import hashlib
import hmac
import json
import time
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from integrations.models import ExternalServiceConnection, GoogleConnection
from organizations.models import Organization
from org_memory.models import (
    GmailMailboxWatch,
    GmailWatchStatus,
    MemoryConnectionConfiguration,
    MemoryConnectionState,
    MemoryProviderEventReceipt,
    MemoryScopeStatus,
    MemorySourceScope,
)
from startup_updates.models import SlackThreadArtifact


@override_settings(
    ORG_MEMORY_LINEAR_WEBHOOK_SECRET="linear-secret",
    ORG_MEMORY_LINEAR_WEBHOOK_MAX_AGE_SECONDS=60,
    ORG_MEMORY_LINEAR_DEBOUNCE_SECONDS=60,
    ORG_MEMORY_SLACK_SIGNING_SECRET="slack-secret",
    ORG_MEMORY_SLACK_WEBHOOK_MAX_AGE_SECONDS=300,
    ORG_MEMORY_SLACK_THREAD_QUIET_SECONDS=900,
    ORG_MEMORY_NOTION_WEBHOOK_VERIFICATION_TOKEN="notion-verification-token",
    ORG_MEMORY_NOTION_WEBHOOK_MAX_AGE_SECONDS=90000,
    ORG_MEMORY_NOTION_DEBOUNCE_SECONDS=60,
    ORG_MEMORY_XERO_WEBHOOK_KEY="xero-webhook-key",
    ORG_MEMORY_STRUCTURED_DEBOUNCE_SECONDS=60,
    ORG_MEMORY_GMAIL_PUBSUB_AUDIENCE="https://api.mlai.test/gmail-push",
    ORG_MEMORY_GMAIL_PUBSUB_SERVICE_ACCOUNT_EMAIL="gmail-push@mlai.iam.gserviceaccount.com",
    ORG_MEMORY_GMAIL_PUBSUB_MAX_AGE_SECONDS=86400,
    ORG_MEMORY_GMAIL_DEBOUNCE_SECONDS=60,
)
class ProviderEventWebhookTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.organization = Organization.objects.create(
            name="Webhook Org",
            domain="webhooks.mlai.test",
        )
        self.user = get_user_model().objects.create_user(email="webhooks@mlai.test")
        self.linear = self._configuration(
            "linear", "linear-org-1", "project", "project-1"
        )
        self.slack = self._configuration("slack", "T123", "channel", "C123")
        self.notion = self._configuration(
            "notion", "notion-workspace-1", "page_root", "root-page"
        )
        self.xero = self._configuration(
            "xero", "tenant-xero-1", "aggregate", "mrr"
        )
        self.gmail_connection = GoogleConnection.objects.create(
            user=self.user,
            google_email="memory-mailbox@mlai.test",
            refresh_token="gmail-refresh-secret",
            scope="https://www.googleapis.com/auth/gmail.readonly",
        )
        self.gmail = MemoryConnectionConfiguration.objects.create(
            organization=self.organization,
            provider="gmail",
            google_connection=self.gmail_connection,
            lifecycle_state=MemoryConnectionState.ACTIVE,
            next_scheduled_sync_at=timezone.now() + timedelta(days=1),
            created_by=self.user,
        )
        MemorySourceScope.objects.create(
            configuration=self.gmail,
            scope_type="label",
            external_id="Label_Leadership",
            selected=True,
            status=MemoryScopeStatus.SELECTED,
            metadata={"label_type": "user"},
        )
        self.gmail_watch = GmailMailboxWatch.objects.create(
            configuration=self.gmail,
            email_address=self.gmail_connection.google_email,
            topic_name="projects/mlai/topics/gmail-memory",
            label_ids=["Label_Leadership"],
            history_id="100",
            status=GmailWatchStatus.ACTIVE,
        )

    def _configuration(self, provider, account_id, scope_type, scope_id):
        connection = ExternalServiceConnection.objects.create(
            provider=provider,
            user=self.user,
            organization=self.organization,
            external_account_id=account_id,
            account_label=account_id,
        )
        configuration = MemoryConnectionConfiguration.objects.create(
            organization=self.organization,
            provider=provider,
            external_connection=connection,
            lifecycle_state=MemoryConnectionState.ACTIVE,
            next_scheduled_sync_at=timezone.now() + timedelta(days=1),
            created_by=self.user,
        )
        MemorySourceScope.objects.create(
            configuration=configuration,
            scope_type=scope_type,
            external_id=scope_id,
            selected=True,
            status=MemoryScopeStatus.SELECTED,
        )
        return configuration

    @staticmethod
    def _json_bytes(payload):
        return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")

    @staticmethod
    def _linear_signature(raw):
        return hmac.new(b"linear-secret", raw, hashlib.sha256).hexdigest()

    @staticmethod
    def _slack_headers(raw, timestamp=None):
        timestamp = timestamp or int(time.time())
        base = b"v0:" + str(timestamp).encode("ascii") + b":" + raw
        digest = hmac.new(b"slack-secret", base, hashlib.sha256).hexdigest()
        return {
            "HTTP_X_SLACK_REQUEST_TIMESTAMP": str(timestamp),
            "HTTP_X_SLACK_SIGNATURE": f"v0={digest}",
        }

    @staticmethod
    def _notion_signature(raw):
        digest = hmac.new(
            b"notion-verification-token", raw, hashlib.sha256
        ).hexdigest()
        return f"sha256={digest}"

    @staticmethod
    def _xero_signature(raw):
        digest = hmac.new(b"xero-webhook-key", raw, hashlib.sha256).digest()
        return base64.b64encode(digest).decode("ascii")

    def test_xero_signature_replay_metadata_only_and_tenant_wake(self):
        raw = self._json_bytes(
            {
                "events": [
                    {
                        "resourceUrl": "https://api.xero.com/api.xro/2.0/Invoices/private-id",
                        "resourceId": "private-invoice-id",
                        "eventDateUtc": timezone.now().isoformat(),
                        "eventType": "UPDATE",
                        "eventCategory": "INVOICE",
                        "tenantId": "tenant-xero-1",
                        "tenantType": "ORGANISATION",
                        "eventSequence": 42,
                    }
                ],
                "firstEventSequence": 42,
                "lastEventSequence": 42,
                "entropy": "must-not-be-stored",
            }
        )
        started = timezone.now()
        accepted = self.client.post(
            "/api/v1/org-memory/webhooks/xero/events",
            data=raw,
            content_type="application/json",
            HTTP_X_XERO_SIGNATURE=self._xero_signature(raw),
        )

        self.assertEqual(accepted.status_code, 200)
        self.assertEqual(accepted.json(), {"status": "accepted", "wake_scheduled": 1})
        self.xero.refresh_from_db()
        self.assertGreaterEqual(
            self.xero.next_scheduled_sync_at,
            started + timedelta(seconds=55),
        )
        receipt = MemoryProviderEventReceipt.objects.get(provider="xero")
        self.assertEqual(receipt.external_account_id, "tenant-xero-1")
        self.assertEqual(receipt.metadata["event_count"], 1)
        self.assertEqual(receipt.metadata["categories"], ["INVOICE"])
        self.assertFalse(receipt.metadata["content_in_receipt"])
        self.assertNotIn("private-invoice-id", repr(receipt.metadata))
        self.assertNotIn("must-not-be-stored", repr(receipt.metadata))

        duplicate = self.client.post(
            "/api/v1/org-memory/webhooks/xero/events",
            data=raw,
            content_type="application/json",
            HTTP_X_XERO_SIGNATURE=self._xero_signature(raw),
        )
        self.assertEqual(duplicate.status_code, 200)
        self.assertEqual(duplicate.json(), {"status": "duplicate", "wake_scheduled": 0})
        self.assertEqual(MemoryProviderEventReceipt.objects.filter(provider="xero").count(), 1)

    def test_xero_intent_to_receive_and_invalid_signature(self):
        raw = self._json_bytes(
            {"events": [], "firstEventSequence": 0, "lastEventSequence": 0, "entropy": ""}
        )
        valid = self.client.post(
            "/api/v1/org-memory/webhooks/xero/events",
            data=raw,
            content_type="application/json",
            HTTP_X_XERO_SIGNATURE=self._xero_signature(raw),
        )
        self.assertEqual(valid.status_code, 200)
        self.assertEqual(valid.json(), {"status": "ignored", "wake_scheduled": 0})

        forged = self.client.post(
            "/api/v1/org-memory/webhooks/xero/events",
            data=self._json_bytes({"events": [{"tenantId": "tenant-xero-1"}]}),
            content_type="application/json",
            HTTP_X_XERO_SIGNATURE="forged",
        )
        self.assertEqual(forged.status_code, 401)
        self.assertEqual(MemoryProviderEventReceipt.objects.filter(provider="xero").count(), 1)

    @staticmethod
    def _gmail_push_bytes(*, message_id="pubsub-1", history_id="101", age_seconds=0):
        notification = json.dumps(
            {
                "emailAddress": "memory-mailbox@mlai.test",
                "historyId": history_id,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        encoded = base64.urlsafe_b64encode(notification).decode("ascii").rstrip("=")
        return ProviderEventWebhookTests._json_bytes(
            {
                "message": {
                    "messageId": message_id,
                    "publishTime": (timezone.now() - timedelta(seconds=age_seconds)).isoformat(),
                    "data": encoded,
                },
                "subscription": "projects/mlai/subscriptions/gmail-memory",
            }
        )

    @patch("org_memory.provider_events.google_id_token.verify_oauth2_token")
    def test_gmail_push_identity_replay_metadata_only_and_debounce(self, verify_token):
        verify_token.return_value = {
            "email": "gmail-push@mlai.iam.gserviceaccount.com",
            "email_verified": True,
        }
        raw = self._gmail_push_bytes()
        started = timezone.now()
        accepted = self.client.post(
            "/api/v1/org-memory/webhooks/gmail/push",
            data=raw,
            content_type="application/json",
            HTTP_AUTHORIZATION="Bearer signed-google-token",
        )

        self.assertEqual(accepted.status_code, 200)
        self.assertEqual(accepted.json(), {"status": "accepted", "wake_scheduled": 1})
        verify_token.assert_called_with(
            "signed-google-token",
            verify_token.call_args.args[1],
            audience="https://api.mlai.test/gmail-push",
        )
        self.gmail.refresh_from_db()
        self.assertGreaterEqual(
            self.gmail.next_scheduled_sync_at,
            started + timedelta(seconds=55),
        )
        self.assertLess(
            self.gmail.next_scheduled_sync_at,
            started + timedelta(seconds=65),
        )
        self.gmail_watch.refresh_from_db()
        self.assertEqual(self.gmail_watch.last_notification_history_id, "101")
        self.assertEqual(self.gmail_watch.last_pubsub_message_id, "pubsub-1")
        self.assertIsNotNone(self.gmail_watch.last_notification_at)

        replay = self.client.post(
            "/api/v1/org-memory/webhooks/gmail/push",
            data=raw,
            content_type="application/json",
            HTTP_AUTHORIZATION="Bearer signed-google-token",
        )
        self.assertEqual(replay.json(), {"status": "duplicate", "wake_scheduled": 0})
        receipt = MemoryProviderEventReceipt.objects.get(provider="gmail")
        self.assertEqual(receipt.scheduled_configuration_count, 1)
        self.assertTrue(receipt.metadata["history_id_present"])
        self.assertFalse(receipt.metadata["content_in_receipt"])
        self.assertNotIn("historyId", repr(receipt.metadata))
        self.assertNotIn("data", receipt.metadata)

    @patch("org_memory.provider_events.google_id_token.verify_oauth2_token")
    def test_gmail_push_rejects_wrong_identity_and_stale_notification(self, verify_token):
        verify_token.return_value = {
            "email": "wrong-service-account@mlai.iam.gserviceaccount.com",
            "email_verified": True,
        }
        wrong_identity = self.client.post(
            "/api/v1/org-memory/webhooks/gmail/push",
            data=self._gmail_push_bytes(),
            content_type="application/json",
            HTTP_AUTHORIZATION="Bearer signed-google-token",
        )
        verify_token.return_value = {
            "email": "gmail-push@mlai.iam.gserviceaccount.com",
            "email_verified": True,
        }
        stale = self.client.post(
            "/api/v1/org-memory/webhooks/gmail/push",
            data=self._gmail_push_bytes(message_id="pubsub-old", age_seconds=90000),
            content_type="application/json",
            HTTP_AUTHORIZATION="Bearer signed-google-token",
        )

        self.assertEqual(wrong_identity.status_code, 401)
        self.assertEqual(stale.status_code, 401)
        self.assertFalse(MemoryProviderEventReceipt.objects.filter(provider="gmail").exists())

    def test_linear_signature_replay_receipt_scope_and_debounce(self):
        raw = self._json_bytes(
            {
                "action": "update",
                "data": {"id": "issue-1", "projectId": "project-1"},
                "organizationId": "linear-org-1",
                "type": "Issue",
                "webhookId": "linear-event-1",
                "webhookTimestamp": int(time.time() * 1000),
            }
        )
        started = timezone.now()
        response = self.client.post(
            "/api/v1/org-memory/webhooks/linear/events",
            data=raw,
            content_type="application/json",
            HTTP_LINEAR_SIGNATURE=self._linear_signature(raw),
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "accepted", "wake_scheduled": 1})
        self.linear.refresh_from_db()
        self.assertGreaterEqual(self.linear.next_scheduled_sync_at, started + timedelta(seconds=55))
        self.assertLess(self.linear.next_scheduled_sync_at, started + timedelta(seconds=65))

        replay = self.client.post(
            "/api/v1/org-memory/webhooks/linear/events",
            data=raw,
            content_type="application/json",
            HTTP_LINEAR_SIGNATURE=self._linear_signature(raw),
        )
        self.assertEqual(replay.json()["status"], "duplicate")
        receipt = MemoryProviderEventReceipt.objects.get(provider="linear")
        self.assertEqual(receipt.scheduled_configuration_count, 1)
        self.assertEqual(receipt.external_scope_id, "project-1")
        self.assertNotIn("data", receipt.metadata)
        self.assertEqual(len(receipt.payload_hash), 64)

    def test_invalid_and_stale_linear_events_fail_closed(self):
        payload = {
            "organizationId": "linear-org-1",
            "webhookTimestamp": int((time.time() - 120) * 1000),
        }
        raw = self._json_bytes(payload)
        invalid = self.client.post(
            "/api/v1/org-memory/webhooks/linear/events",
            data=raw,
            content_type="application/json",
            HTTP_LINEAR_SIGNATURE="invalid",
        )
        stale = self.client.post(
            "/api/v1/org-memory/webhooks/linear/events",
            data=raw,
            content_type="application/json",
            HTTP_LINEAR_SIGNATURE=self._linear_signature(raw),
        )
        self.assertEqual(invalid.status_code, 401)
        self.assertEqual(stale.status_code, 401)
        self.assertFalse(MemoryProviderEventReceipt.objects.exists())

    def test_slack_signature_challenge_replay_dm_exclusion_and_debounce(self):
        challenge_raw = self._json_bytes(
            {"type": "url_verification", "challenge": "challenge-value"}
        )
        challenge = self.client.post(
            "/api/v1/org-memory/webhooks/slack/events",
            data=challenge_raw,
            content_type="application/json",
            **self._slack_headers(challenge_raw),
        )
        self.assertEqual(challenge.status_code, 200)
        self.assertEqual(challenge.json(), {"challenge": "challenge-value"})

        raw = self._json_bytes(
            {
                "type": "event_callback",
                "team_id": "T123",
                "event_id": "Ev1",
                "event": {"type": "message", "channel": "C123", "ts": "1.1"},
            }
        )
        started = timezone.now()
        accepted = self.client.post(
            "/api/v1/org-memory/webhooks/slack/events",
            data=raw,
            content_type="application/json",
            **self._slack_headers(raw),
        )
        self.assertEqual(accepted.json(), {"status": "accepted", "wake_scheduled": 1})
        self.slack.refresh_from_db()
        self.assertGreaterEqual(self.slack.next_scheduled_sync_at, started + timedelta(seconds=895))
        self.assertLess(self.slack.next_scheduled_sync_at, started + timedelta(seconds=905))

        replay = self.client.post(
            "/api/v1/org-memory/webhooks/slack/events",
            data=raw,
            content_type="application/json",
            **self._slack_headers(raw),
        )
        self.assertEqual(replay.json()["status"], "duplicate")

        dm_raw = self._json_bytes(
            {
                "type": "event_callback",
                "team_id": "T123",
                "event_id": "EvDM",
                "event": {"type": "message", "channel": "D123", "ts": "2.1"},
            }
        )
        dm = self.client.post(
            "/api/v1/org-memory/webhooks/slack/events",
            data=dm_raw,
            content_type="application/json",
            **self._slack_headers(dm_raw),
        )
        self.assertEqual(dm.json(), {"status": "ignored", "wake_scheduled": 0})
        receipt = MemoryProviderEventReceipt.objects.get(provider="slack", external_scope_id="D123")
        self.assertTrue(receipt.metadata["dm_excluded"])
        self.assertEqual(MemoryProviderEventReceipt.objects.filter(provider="slack").count(), 2)

    def test_slack_invalid_signature_and_old_timestamp_fail_closed(self):
        raw = self._json_bytes({"type": "event_callback", "team_id": "T123", "event": {}})
        invalid = self.client.post(
            "/api/v1/org-memory/webhooks/slack/events",
            data=raw,
            content_type="application/json",
            HTTP_X_SLACK_REQUEST_TIMESTAMP=str(int(time.time())),
            HTTP_X_SLACK_SIGNATURE="v0=invalid",
        )
        stale = self.client.post(
            "/api/v1/org-memory/webhooks/slack/events",
            data=raw,
            content_type="application/json",
            **self._slack_headers(raw, int(time.time()) - 600),
        )
        self.assertEqual(invalid.status_code, 401)
        self.assertEqual(stale.status_code, 401)
        self.assertFalse(MemoryProviderEventReceipt.objects.exists())

    def test_durable_artifact_save_schedules_the_same_quiet_wake(self):
        started = timezone.now()
        with self.captureOnCommitCallbacks(execute=True):
            SlackThreadArtifact.objects.create(
                organization=self.organization,
                connection=self.slack.external_connection,
                channel_id="C123",
                channel_name="leadership",
                thread_ts="1770000000.000100",
                cleaned_text="A durable thread was refreshed.",
                latest_message_at=started,
            )
        self.slack.refresh_from_db()
        self.assertGreaterEqual(
            self.slack.next_scheduled_sync_at,
            started + timedelta(seconds=895),
        )
        self.assertLess(
            self.slack.next_scheduled_sync_at,
            started + timedelta(seconds=905),
        )

    def test_notion_verification_signature_replay_and_workspace_wake(self):
        verification = self.client.post(
            "/api/v1/org-memory/webhooks/notion/events",
            data=self._json_bytes(
                {"verification_token": "notion-verification-token"}
            ),
            content_type="application/json",
        )
        self.assertEqual(
            verification.json(),
            {"status": "verification_received", "wake_scheduled": 0},
        )
        self.assertFalse(MemoryProviderEventReceipt.objects.filter(provider="notion").exists())

        raw = self._json_bytes(
            {
                "id": "notion-event-1",
                "timestamp": timezone.now().isoformat(),
                "workspace_id": "notion-workspace-1",
                "type": "page.content_updated",
                "entity": {"id": "descendant-page", "type": "page"},
                "data": {"parent": {"id": "root-page", "type": "page"}},
            }
        )
        started = timezone.now()
        accepted = self.client.post(
            "/api/v1/org-memory/webhooks/notion/events",
            data=raw,
            content_type="application/json",
            HTTP_X_NOTION_SIGNATURE=self._notion_signature(raw),
        )
        self.assertEqual(accepted.json(), {"status": "accepted", "wake_scheduled": 1})
        self.notion.refresh_from_db()
        self.assertGreaterEqual(
            self.notion.next_scheduled_sync_at,
            started + timedelta(seconds=55),
        )
        self.assertLess(
            self.notion.next_scheduled_sync_at,
            started + timedelta(seconds=65),
        )
        replay = self.client.post(
            "/api/v1/org-memory/webhooks/notion/events",
            data=raw,
            content_type="application/json",
            HTTP_X_NOTION_SIGNATURE=self._notion_signature(raw),
        )
        self.assertEqual(replay.json()["status"], "duplicate")
        receipt = MemoryProviderEventReceipt.objects.get(provider="notion")
        self.assertEqual(receipt.external_scope_id, "descendant-page")
        self.assertEqual(receipt.scheduled_configuration_count, 1)
        self.assertFalse(receipt.metadata["content_in_receipt"])
        self.assertNotIn("data", receipt.metadata)

    def test_notion_invalid_signature_and_mismatched_verification_fail_closed(self):
        mismatched = self.client.post(
            "/api/v1/org-memory/webhooks/notion/events",
            data=self._json_bytes({"verification_token": "wrong-token"}),
            content_type="application/json",
        )
        raw = self._json_bytes(
            {
                "id": "bad-event",
                "timestamp": timezone.now().isoformat(),
                "workspace_id": "notion-workspace-1",
                "type": "page.deleted",
                "entity": {"id": "root-page", "type": "page"},
            }
        )
        invalid = self.client.post(
            "/api/v1/org-memory/webhooks/notion/events",
            data=raw,
            content_type="application/json",
            HTTP_X_NOTION_SIGNATURE="sha256=invalid",
        )
        self.assertEqual(mismatched.status_code, 401)
        self.assertEqual(invalid.status_code, 401)
        self.assertFalse(MemoryProviderEventReceipt.objects.filter(provider="notion").exists())
