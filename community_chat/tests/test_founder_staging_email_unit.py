"""Fail-closed fixture profile/provider controls; no DB, network or mail."""
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
import uuid

from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase, override_settings
from django.utils import timezone

from community_chat import founder_staging_email as provider
from community_chat.tests.founder_staging_fixtures import DOMAIN, SETTINGS, WRITER


@override_settings(**SETTINGS)
class FounderStagingEmailUnitTests(SimpleTestCase):
    def setUp(self):
        self.delivery_id = uuid.uuid4()
        self.email = "founder-acceptance-" + str(uuid.uuid4()) + "@" + DOMAIN
        issued = timezone.now() - timedelta(seconds=5)
        self.challenge = SimpleNamespace(origin=provider.FRONTEND, client_id=provider.CLIENT,
            created_at=issued, expires_at=issued + timedelta(seconds=600))
        self.recipient = SimpleNamespace(email=self.email)
        self.session = MagicMock()
        self.session.__enter__.return_value = self.session
        self.session.post.return_value = SimpleNamespace(status_code=202,
            json=lambda: {"accepted": True, "deliveryId": str(self.delivery_id)})

    def send(self):
        return provider.send_fixture_code(provider.fixture_scope(), delivery_id=self.delivery_id,
            challenge=self.challenge, recipient=self.recipient, code="004200")

    def test_every_existing_profile_retains_ordinary_delivery(self):
        for module in ("mlai.settings", "mlai.settings_staging_chat", "mlai.settings_staging_local", None):
            with self.subTest(module=module), override_settings(SETTINGS_MODULE=module):
                self.assertIsNone(provider.fixture_scope())

    def test_invalid_fixture_configuration_fails_before_a_provider_is_created(self):
        invalid = {"APP_ENV": "production", "FOUNDER_STAGING_ENABLED": False,
            "FOUNDER_STAGING_INBOX_PROVIDER_ENABLED": False, "FOUNDER_STAGING_EMAIL_DOMAIN": "mlai.au",
            "FOUNDER_STAGING_INBOX_WRITE_TOKEN": "", "FOUNDER_STAGING_INBOX_PROVIDER_URL": "https://api.mlai.au/deliver",
            "COMMUNITY_CHAT_FRONTEND_URL": "https://chat-staging.mlai.au", "COMMUNITY_CHAT_API_AUDIENCE": "https://api.mlai.au",
            "COMMUNITY_CHAT_STAGING_PASSWORD_ENABLED": True, "COMMUNITY_CHAT_FEDERATION_STAGING_ENABLED": True,
            "COMMUNITY_CHAT_SIGNUP_ENABLED": False, "COMMUNITY_CHAT_EMAIL_CODE_AUTH_ENABLED": False,
            "STAGING_CAPABILITIES": {"email": "sandbox", "account_creation": "local"}}
        for name, value in invalid.items():
            with self.subTest(setting=name), override_settings(SETTINGS_MODULE=provider.PROFILE, **{name: value}), \
                    patch.object(provider.requests, "Session") as session:
                with self.assertRaises(ImproperlyConfigured):
                    self.send()
                session.assert_not_called()

    def test_each_notification_gate_must_remain_off(self):
        for name in ("ARTICLES_DRAFT_READY_NOTIFICATIONS_ENABLED", "ARTICLES_GOAL_NOTIFICATIONS_ENABLED",
                     "MY_STARTUP_TRANSITION_NOTIFICATIONS_ENABLED", "MY_STARTUP_TURN_REMINDERS_ENABLED"):
            with self.subTest(setting=name), override_settings(SETTINGS_MODULE=provider.PROFILE, **{name: True}), \
                    self.assertRaises(ImproperlyConfigured):
                provider.fixture_scope()

    def test_scope_repr_omits_the_provider_credential(self):
        self.assertNotIn(WRITER, repr(provider.fixture_scope()))

    def test_provider_receives_original_immutable_challenge_times_and_outbox_identity(self):
        with patch.object(provider.requests, "Session", return_value=self.session):
            self.assertEqual(self.send(), {"delivery_id": str(self.delivery_id)})
        args, kwargs = self.session.post.call_args
        self.assertEqual(args, (provider.PROVIDER,))
        self.assertEqual(kwargs["json"], {"deliveryId": str(self.delivery_id), "email": self.email, "code": "004200",
            "issuedAt": self.challenge.created_at.isoformat(), "expiresAt": self.challenge.expires_at.isoformat()})
        self.assertFalse(kwargs["allow_redirects"])
        self.assertEqual(kwargs["timeout"], (2, 5))
        self.assertFalse(self.session.trust_env)

    def test_foreign_profile_or_recipient_cannot_reach_the_provider(self):
        for field, value in (("origin", "https://chat-staging.mlai.au"), ("client_id", "mlai-chat-ios")):
            original = getattr(self.challenge, field)
            setattr(self.challenge, field, value)
            with self.subTest(field=field), patch.object(provider.requests, "Session") as session:
                with self.assertRaises(ValueError):
                    self.send()
                session.assert_not_called()
            setattr(self.challenge, field, original)
        self.recipient.email = "sam@mlai.au"
        with patch.object(provider.requests, "Session") as session, self.assertRaises(ValueError):
            self.send()
        session.assert_not_called()

    def test_expired_or_future_source_challenges_are_not_retimestamped(self):
        now = timezone.now()
        for issued, expires in ((now - timedelta(seconds=301), now + timedelta(seconds=299)),
                (now + timedelta(seconds=10), now + timedelta(seconds=600)),
                (now - timedelta(seconds=1), now + timedelta(seconds=1200))):
            self.challenge.created_at, self.challenge.expires_at = issued, expires
            with self.subTest(kind="invalid source"), patch.object(provider.requests, "Session") as session:
                with self.assertRaises(ValueError):
                    self.send()
                session.assert_not_called()

    def test_redirect_failure_or_wrong_receipt_never_becomes_delivery_success(self):
        for status, receipt in ((302, {"accepted": True, "deliveryId": str(self.delivery_id)}),
                (503, {}), (202, {"accepted": True, "deliveryId": str(uuid.uuid4())}), (202, {"accepted": False})):
            self.session.post.return_value = SimpleNamespace(status_code=status, json=lambda: receipt)
            with self.subTest(status=status), patch.object(provider.requests, "Session", return_value=self.session), \
                    self.assertRaises((ValueError, RuntimeError)):
                self.send()

    def test_direct_legacy_sender_is_refused_instead_of_falling_back_to_live_mail(self):
        from community_chat.email_delivery import send_community_chat_email_code
        with patch("community_chat.email_delivery.APIClient") as legacy, self.assertRaises(RuntimeError):
            send_community_chat_email_code(self.recipient, "004200")
        legacy.assert_not_called()
