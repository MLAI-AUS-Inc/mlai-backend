"""Browser session and handoff regressions without database setup or migrations."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.http import HttpResponse
from django.test import SimpleTestCase, override_settings
from rest_framework.test import APIRequestFactory

from community_chat import account_sessions, views
from community_chat.account_cookies import set_account_session_cookies


@override_settings(
    DEBUG=False,
    COMMUNITY_CHAT_DEVICE_AUTH_ENABLED=True,
    COMMUNITY_CHAT_SESSION_ACCESS_TTL_SECONDS=900,
    COMMUNITY_CHAT_SESSION_REFRESH_TTL_DAYS=30,
)
class BrowserAuthRecoveryTests(SimpleTestCase):
    def test_status_rejects_used_and_expired_links_without_account_disclosure(self):
        now = datetime(2026, 9, 29, tzinfo=timezone.utc)
        for record in (
            None,
            SimpleNamespace(
                expires_at=now - timedelta(days=30),
                consumed_at=now - timedelta(days=30),
            ),
            SimpleNamespace(expires_at=now - timedelta(seconds=1), consumed_at=None),
            SimpleNamespace(expires_at=now + timedelta(minutes=5), consumed_at=now),
        ):
            with self.subTest(record=record), patch.object(
                views.CommunityChatDeviceAuthRequest, "objects"
            ) as manager, patch.object(views, "enforce_bootstrap_limits"), patch.object(
                views.timezone, "now", return_value=now
            ):
                manager.filter.return_value.only.return_value.first.return_value = (
                    record
                )
                response = views.DeviceAuthStartView.as_view()(
                    APIRequestFactory().get(
                        "/",
                        {"request_id": "550e8400-e29b-41d4-a716-446655440000"},
                    )
                )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.data, {"status": "unavailable"})
                self.assertEqual(response["Cache-Control"], "no-store")
                manager.create.assert_not_called()

    def test_live_status_returns_only_lifetime_without_authorizing(self):
        expires = datetime(2099, 1, 1, tzinfo=timezone.utc)
        with patch.object(
            views.CommunityChatDeviceAuthRequest, "objects"
        ) as manager, patch.object(views, "enforce_bootstrap_limits"):
            record = SimpleNamespace(expires_at=expires, consumed_at=None, save=Mock())
            manager.filter.return_value.only.return_value.first.return_value = record
            response = views.DeviceAuthStartView.as_view()(
                APIRequestFactory().get(
                    "/",
                    {"request_id": "550e8400-e29b-41d4-a716-446655440000"},
                )
            )
            self.assertEqual(
                response.data, {"status": "pending", "expires_at": expires}
            )
            record.save.assert_not_called()

    def test_invalid_status_id_never_reads_a_request(self):
        with patch.object(views.CommunityChatDeviceAuthRequest, "objects") as manager:
            response = views.DeviceAuthStartView.as_view()(
                APIRequestFactory().get("/", {"request_id": "bad"})
            )
            self.assertEqual(response.status_code, 400)
            manager.filter.assert_not_called()

    def test_failed_refresh_cannot_erase_another_response_newer_cookies(self):
        with patch.object(
            views,
            "rotate_account_session",
            side_effect=account_sessions.InvalidAccountSession,
        ):
            request = APIRequestFactory().post(
                "/",
                {},
                format="json",
                HTTP_ORIGIN="https://chat.mlai.au",
                HTTP_COOKIE="mlai_chat_refresh=old-token",
            )
            response = views.AccountSessionRefreshView.as_view()(request)
            self.assertEqual(response.status_code, 401)
            self.assertFalse(response.cookies)
            self.assertEqual(response["Cache-Control"], "no-store")

    def test_login_cookie_persists_for_thirty_days_and_tokens_are_http_only(self):
        credentials = SimpleNamespace(
            access_token="test-access", refresh_token="test-refresh"
        )
        response = set_account_session_cookies(HttpResponse(), credentials)
        self.assertEqual(response.cookies["mlai_chat_refresh"]["max-age"], 30 * 86400)
        self.assertEqual(response.cookies["mlai_chat_access"]["max-age"], 900)
        for cookie in response.cookies.values():
            self.assertTrue(cookie["httponly"])
            self.assertTrue(cookie["secure"])

    def test_new_login_scopes_revocation_to_its_account_and_installation(self):
        now = datetime(2026, 9, 29, tzinfo=timezone.utc)
        user = SimpleNamespace(pk=123, is_active=True, auth_version=1)
        challenge = SimpleNamespace(
            client_id="mlai-chat-web",
            installation_id="safari-only",
            public_key="test-key",
            origin="https://chat.mlai.au",
            platform="web",
            device_name="Safari",
        )
        with patch.object(account_sessions.transaction, "atomic"), patch.object(
            account_sessions, "get_user_model"
        ) as users, patch.object(
            account_sessions.CommunityChatAccountSession, "objects"
        ) as sessions, patch.object(
            account_sessions.timezone, "now", return_value=now
        ):
            users.return_value.objects.select_for_update.return_value.get.return_value = (
                user
            )
            account_sessions.issue_account_session(user, challenge)
            sessions.filter.assert_called_once_with(
                user=user,
                client_id="mlai-chat-web",
                installation_id="safari-only",
                revoked_at__isnull=True,
            )
            self.assertEqual(
                sessions.create.call_args.kwargs["expires_at"], now + timedelta(days=30)
            )

    def test_valid_day_29_session_rotates_for_another_thirty_days(self):
        now = datetime(2026, 9, 29, tzinfo=timezone.utc)
        session = SimpleNamespace(
            revoked_at=None,
            expires_at=now + timedelta(days=1),
            user=SimpleNamespace(is_active=True, auth_version=1),
            auth_version=1,
            origin="https://chat.mlai.au",
            save=Mock(),
        )
        with patch.object(account_sessions.transaction, "atomic"), patch.object(
            account_sessions.CommunityChatAccountSession, "objects"
        ) as manager, patch.object(
            account_sessions, "_validate_device_owner"
        ), patch.object(
            account_sessions.timezone, "now", return_value=now
        ):
            manager.select_for_update.return_value.select_related.return_value.filter.return_value.first.return_value = (
                session
            )
            result = account_sessions.rotate_account_session(
                "mlai_session_refresh_test", required_origin="https://chat.mlai.au"
            )
            self.assertEqual(result.session.expires_at, now + timedelta(days=30))
            self.assertEqual(
                result.session.access_expires_at, now + timedelta(seconds=900)
            )
            self.assertNotEqual(result.refresh_token, "mlai_session_refresh_test")
            session.expires_at = now
            with self.assertRaises(account_sessions.InvalidAccountSession):
                account_sessions.rotate_account_session(result.refresh_token)
