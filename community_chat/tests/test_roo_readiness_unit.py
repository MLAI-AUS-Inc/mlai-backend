"""Roo request readiness, with mocked links and consent; no database.

Run: python scripts/test_without_database.py community_chat.tests.test_roo_readiness_unit
"""
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

from community_chat.roo_readiness import roo_request_readiness

LINKS = "integrations.models.CommunityBridgeIdentityLink.objects"


@override_settings(
    COMMUNITY_CHAT_RELAY_URL="wss://chat.mlai.au",
    COMMUNITY_CHAT_ROO_SLACK_WORKSPACE_ID="TMLAI",
    COMMUNITY_CHAT_ROO_SLACK_USER_ID="UROO",
    COMMUNITY_CHAT_AI_CONSENT_REQUIRED=True,
)
class RooRequestReadinessTests(SimpleTestCase):
    def setUp(self):
        self.user = SimpleNamespace(pk=42)
        self.links = self.enterContext(patch(LINKS))
        self.exists = self.links.filter.return_value.exclude.return_value.exists
        self.exists.return_value = True
        self.consent = self.enterContext(
            patch("community_chat.privacy.has_ai_consent", return_value=True))

    def test_linked_and_consented_member_is_ready(self):
        self.assertEqual(roo_request_readiness(self.user), "ready")
        query = self.links.filter.call_args.kwargs
        self.assertEqual(query, {"user_id": 42, "slack_workspace_id": "TMLAI", "revoked_at__isnull": True})
        self.consent.assert_called_once_with(42)

    def test_chat_only_member_needs_a_slack_link_first(self):
        self.exists.return_value = False
        self.assertEqual(roo_request_readiness(self.user), "slack_not_linked")
        self.consent.assert_not_called()

    def test_linked_member_without_consent_needs_ai_sharing(self):
        self.consent.return_value = False
        self.assertEqual(roo_request_readiness(self.user), "ai_sharing_off")

    @override_settings(COMMUNITY_CHAT_AI_CONSENT_REQUIRED=False)
    def test_consent_is_not_checked_when_not_required(self):
        self.consent.return_value = False
        self.assertEqual(roo_request_readiness(self.user), "ready")

    @override_settings(COMMUNITY_CHAT_RELAY_URL="wss://other.example.test")
    def test_unconfigured_roo_reports_nothing(self):
        self.assertIsNone(roo_request_readiness(self.user))
        self.links.filter.assert_not_called()
