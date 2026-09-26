"""Legacy account-switch regression tests without database or provider access."""

from contextlib import nullcontext
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIRequestFactory

from community_chat.account_sessions import (
    ACCESS_TOKEN_PREFIX,
    REFRESH_TOKEN_PREFIX,
    InvalidAccountSession,
    authenticate_access_token,
    revoke_account_session,
    rotate_account_session,
)
from community_chat.authentication import CommunityChatAccountAuthentication
from community_chat.models import DeviceBindingStatus
from community_chat.slack_views import SlackDmMirrorView


@override_settings(
    COMMUNITY_CHAT_SESSION_ACCESS_TTL_SECONDS=900,
    COMMUNITY_CHAT_SESSION_REFRESH_TTL_DAYS=30,
)
class SessionDeviceBindingTests(SimpleTestCase):
    def setUp(self):
        now = timezone.now()
        self.session = SimpleNamespace(
            id=1,
            user_id=2,
            user=SimpleNamespace(is_active=True, auth_version=1),
            public_key="a" * 64,
            revoked_at=None,
            expires_at=now + timedelta(days=30),
            access_expires_at=now + timedelta(minutes=15),
            auth_version=1,
            origin="mlaichat://callback",
            save=Mock(),
        )
        sessions = patch("community_chat.account_sessions.CommunityChatAccountSession.objects")
        self.sessions = sessions.start()
        self.addCleanup(sessions.stop)
        self.sessions.select_related.return_value.filter.return_value.first.return_value = self.session
        self.sessions.select_for_update.return_value.select_related.return_value.filter.return_value.first.return_value = self.session
        devices = patch("community_chat.account_sessions.CommunityChatDevice.objects")
        self.devices = devices.start()
        self.addCleanup(devices.stop)
        self.conflicts = self.devices.filter.return_value.exclude.return_value
        self.conflicts.exists.return_value = False
        atomic = patch("community_chat.account_sessions.transaction.atomic", side_effect=nullcontext)
        atomic.start()
        self.addCleanup(atomic.stop)

    def test_foreign_device_rejects_access_without_marking_session_used(self):
        self.conflicts.exists.return_value = True
        with self.assertRaises(InvalidAccountSession):
            authenticate_access_token(ACCESS_TOKEN_PREFIX + "synthetic")
        self.sessions.filter.return_value.update.assert_not_called()

    def test_foreign_device_rejects_refresh_without_rotating_credentials(self):
        self.conflicts.exists.return_value = True
        with self.assertRaises(InvalidAccountSession):
            rotate_account_session(REFRESH_TOKEN_PREFIX + "synthetic")
        self.session.save.assert_not_called()

    def test_only_active_bindings_to_a_different_account_are_conflicts(self):
        self.assertIs(
            authenticate_access_token(ACCESS_TOKEN_PREFIX + "synthetic"),
            self.session,
        )
        self.devices.filter.assert_called_once_with(
            public_key=self.session.public_key,
            status__in=(DeviceBindingStatus.PENDING, DeviceBindingStatus.VERIFIED),
            revoked_at__isnull=True,
        )
        self.devices.filter.return_value.exclude.assert_called_once_with(user_id=2)

    def test_new_installation_without_a_device_can_refresh_before_enrollment(self):
        credentials = rotate_account_session(REFRESH_TOKEN_PREFIX + "synthetic")
        self.assertIs(credentials.session, self.session)
        self.assertTrue(credentials.access_token.startswith(ACCESS_TOKEN_PREFIX))
        self.session.save.assert_called_once()

    def test_mixed_session_can_still_be_signed_out(self):
        self.conflicts.exists.return_value = True
        self.assertIs(
            revoke_account_session(REFRESH_TOKEN_PREFIX + "synthetic"),
            self.session,
        )
        self.assertIsNotNone(self.session.revoked_at)

    def test_slack_read_and_resume_require_sign_in_before_reaching_the_wrong_account(self):
        self.conflicts.exists.return_value = True
        factory = APIRequestFactory()
        for method in ("get", "patch"):
            with self.subTest(method=method), patch(
                "community_chat.slack_views.status_payload"
            ) as payload, patch("community_chat.slack_views.resume_grant") as resume:
                request = getattr(factory, method)(
                    "/api/v1/community-chat/slack/",
                    {"action": "resume"} if method == "patch" else {},
                    format="json",
                    HTTP_AUTHORIZATION="Bearer " + ACCESS_TOKEN_PREFIX + "synthetic",
                )
                response = SlackDmMirrorView.as_view(
                    authentication_classes=(CommunityChatAccountAuthentication,),
                    throttle_classes=(),
                )(request)
                self.assertEqual(response.status_code, 401)
                payload.assert_not_called()
                resume.assert_not_called()
