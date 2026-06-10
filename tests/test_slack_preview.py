from django.contrib.auth import get_user_model
from django.test import TestCase

from integrations.models import ExternalServiceConnection, ExternalServiceProvider
from integrations.services.external_connectors import serialize_slack_preview
from organizations.models import Organization

User = get_user_model()


class SlackPreviewSerializationTests(TestCase):
    def test_preview_without_connection_reports_not_connected(self):
        user = User.objects.create_user(email="founder@example.com", password="password")

        payload = serialize_slack_preview(user, limit=5)

        self.assertIn("Slack is not connected.", payload["warnings"])
        self.assertEqual(payload["messages"], [])

    def test_preview_with_connection_serializes_without_error(self):
        # Regression: the connected branch crashed with NameError after a stray
        # **scope_status spread was copied in from the Gmail serializer.
        user = User.objects.create_user(email="founder2@example.com", password="password")
        organization = Organization.objects.create(name="Acme Inc.", domain="acme.com")
        ExternalServiceConnection.objects.create(
            user=user,
            organization=organization,
            provider=ExternalServiceProvider.SLACK,
            external_account_id="T123",
            account_label="Acme Slack",
        )

        payload = serialize_slack_preview(user, limit=5)

        self.assertEqual(payload["teamId"], "T123")
        self.assertEqual(payload["messages"], [])
        self.assertIn("Select at least one Slack channel before syncing.", payload["warnings"])
