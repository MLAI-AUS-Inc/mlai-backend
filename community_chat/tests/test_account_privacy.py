"""Privacy API and dispatch boundaries, using disposable synthetic accounts."""

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework.exceptions import AuthenticationFailed

from community_chat.account_sessions import rotate_account_session
from community_chat.models import AccountDeletionRequest, AiConsentRecord, CommunityChatAccountSession
from community_chat.privacy import ai_disclosure, has_ai_consent, set_ai_consent
from community_chat.tests.test_account_profiles import credentials_for, ORIGIN
from integrations.services.slack_dm_mirror import _verify_ai_recipients_before_send, SlackDmMirrorAuthorizationError

PROVIDERS = [{"name": "Test provider", "purpose": "Answer Roo requests",
              "data": "Messages, attachments and conversation context", "privacy_url": "https://example.com/privacy"}]


@override_settings(
    COMMUNITY_CHAT_AI_PROVIDERS=PROVIDERS,
    COMMUNITY_CHAT_AI_DISCLOSURE_VERSION="test-v1",
    COMMUNITY_CHAT_AI_CONSENT_REQUIRED=True,
    COMMUNITY_CHAT_DELETION_TIMEFRAME="Within 30 days (test fixture)",
    COMMUNITY_CHAT_DELETION_CONTACT="privacy@example.com",
    COMMUNITY_CHAT_ALLOWED_ORIGINS=[ORIGIN, "mlaichat://callback"],
    COMMUNITY_CHAT_RELAY_URL="wss://chat.mlai.au",
    COMMUNITY_CHAT_ROO_SLACK_WORKSPACE_ID="TTEST",
    COMMUNITY_CHAT_ROO_SLACK_USER_ID="UROO",
)
class AccountPrivacyTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(email="privacy@example.com")
        self.credentials = credentials_for(self.user)
        self.client = APIClient()
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.credentials.access_token}")
        self.consent_url = reverse("community_chat_ai_consent")
        self.deletion_url = reverse("community_chat_account_deletion")

    def grant(self):
        disclosure = self.client.get(self.consent_url).data
        return self.client.put(self.consent_url, {
            "granted": True, "version": disclosure["version"], "provider_digest": disclosure["provider_digest"],
        }, format="json")

    def deletion(self, **changes):
        return self.client.post(self.deletion_url, {
            "scope": "shared_mlai_account", "confirmed": True,
            "policy_version": "2026-09-20", **changes,
        }, format="json")

    def test_unauthenticated_requests_are_rejected(self):
        client = APIClient()
        for url in (self.consent_url, self.deletion_url):
            self.assertEqual(client.get(url).status_code, 401)
            self.assertEqual(client.post(url, {}, format="json").status_code, 401)

    def test_grant_withdraw_and_regrant_are_account_scoped(self):
        self.assertFalse(self.client.get(self.consent_url).data["granted"])
        self.assertEqual(self.grant().status_code, 200)
        self.assertTrue(has_ai_consent(self.user.pk))
        other = get_user_model().objects.create_user(email="other@example.com")
        self.assertFalse(has_ai_consent(other.pk))
        self.assertEqual(self.client.put(self.consent_url, {"granted": False}, format="json").status_code, 200)
        self.assertFalse(has_ai_consent(self.user.pk))
        self.assertEqual(self.grant().status_code, 200)
        self.assertEqual(AiConsentRecord.objects.filter(user=self.user).count(), 1)
        self.assertTrue(has_ai_consent(self.user.pk))

    def test_provider_or_version_change_invalidates_consent(self):
        self.grant()
        with override_settings(COMMUNITY_CHAT_AI_DISCLOSURE_VERSION="test-v2"):
            self.assertFalse(has_ai_consent(self.user.pk))
        with override_settings(COMMUNITY_CHAT_AI_PROVIDERS=[{**PROVIDERS[0], "name": "New provider"}]):
            self.assertFalse(has_ai_consent(self.user.pk))

    def test_stale_or_unconfigured_disclosure_cannot_be_accepted(self):
        response = self.client.put(self.consent_url, {"granted": True, "version": "old", "provider_digest": "x"}, format="json")
        self.assertEqual(response.status_code, 400)
        with override_settings(COMMUNITY_CHAT_AI_PROVIDERS=[]):
            self.assertFalse(ai_disclosure()["available"])
            self.assertEqual(self.grant().status_code, 400)
        self.assertFalse(AiConsentRecord.objects.exists())

    def test_withdrawal_does_not_need_current_disclosure(self):
        self.grant()
        with override_settings(COMMUNITY_CHAT_AI_PROVIDERS=[]):
            response = self.client.put(self.consent_url, {"granted": False}, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertIsNotNone(AiConsentRecord.objects.get(user=self.user).withdrawn_at)

    def test_old_rotated_session_cannot_change_privacy(self):
        rotate_account_session(self.credentials.refresh_token)
        with self.assertRaises(AuthenticationFailed):
            set_ai_consent(authenticated_session=self.credentials.session,
                           granted=False, version="", provider_digest="")

    def test_deletion_is_idempotent_and_never_pretends_to_complete(self):
        first, second = self.deletion(), self.deletion()
        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.data["request"]["id"], second.data["request"]["id"])
        self.assertEqual(first.data["request"]["status"], "requested")
        self.assertIsNone(first.data["request"]["completed_at"])
        self.assertEqual(AccountDeletionRequest.objects.count(), 1)
        self.user.refresh_from_db()
        self.assertTrue(self.user.is_active)

    def test_new_deletion_requires_recent_sign_in_not_token_refresh(self):
        CommunityChatAccountSession.objects.filter(pk=self.credentials.session.pk).update(created_at=timezone.now()-timedelta(minutes=11))
        self.assertEqual(self.deletion().status_code, 403)
        self.assertFalse(AccountDeletionRequest.objects.exists())

    def test_deletion_requires_confirmation_and_operator_configuration(self):
        self.assertEqual(self.deletion(confirmed=False).status_code, 400)
        self.assertEqual(self.deletion(policy_version="old").status_code, 400)
        with override_settings(COMMUNITY_CHAT_DELETION_TIMEFRAME=""):
            self.assertFalse(self.client.get(self.deletion_url).data["available"])
            self.assertEqual(self.deletion().status_code, 400)
        self.assertFalse(AccountDeletionRequest.objects.exists())

    def test_receipts_and_writes_cannot_target_another_account(self):
        other = get_user_model().objects.create_user(email="other@example.com")
        AccountDeletionRequest.objects.create(user=other, scope="chat_data", policy_version="2026-09-20")
        self.assertEqual(self.client.get(self.deletion_url).data["requests"], [])
        self.assertEqual(self.deletion(user_id=other.pk).status_code, 400)
        self.assertEqual(self.client.put(self.consent_url, {"granted": False, "user_id": other.pk}, format="json").status_code, 400)

    def test_cookie_writes_require_bound_origin(self):
        from community_chat.account_cookies import ACCESS_COOKIE
        credentials = credentials_for(self.user, web=True)
        client = APIClient()
        client.cookies[ACCESS_COOKIE] = credentials.access_token
        self.assertEqual(client.put(self.consent_url, {"granted": False}, format="json", HTTP_ORIGIN="https://evil.example").status_code, 401)
        self.assertEqual(client.put(self.consent_url, {"granted": False}, format="json", HTTP_ORIGIN=ORIGIN).status_code, 200)

    def delivery(self, operation="create"):
        return SimpleNamespace(operation=operation, conversation=SimpleNamespace(
            slack_conversation_id="DTEST", grant=SimpleNamespace(user_id=self.user.pk, slack_workspace_id="TTEST"),
        ))

    def test_queued_dm_thread_and_media_writes_fail_after_withdrawal(self):
        self.grant()
        client = Mock()
        client.conversations_members.return_value = {"members": ["UOWNER", "UROO"]}
        _verify_ai_recipients_before_send(self.delivery(), client)
        self.client.put(self.consent_url, {"granted": False}, format="json")
        for operation in ("create", "edit", "reaction_add"):
            with self.subTest(operation=operation), self.assertRaises(SlackDmMirrorAuthorizationError):
                _verify_ai_recipients_before_send(self.delivery(operation), client)
        client.chat_postMessage.assert_not_called()

    def test_deletion_and_human_only_conversations_remain_available(self):
        client = Mock()
        client.conversations_members.return_value = {"members": ["UOWNER", "UHUMAN"]}
        _verify_ai_recipients_before_send(self.delivery(), client)
        _verify_ai_recipients_before_send(self.delivery("delete"), client)

    def test_unknown_membership_fails_closed(self):
        client = Mock()
        client.conversations_members.return_value = {}
        with self.assertRaises(SlackDmMirrorAuthorizationError):
            _verify_ai_recipients_before_send(self.delivery(), client)
