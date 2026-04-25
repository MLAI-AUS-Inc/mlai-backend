from unittest.mock import MagicMock, patch
import urllib.parse

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from organizations.models import Organization
from workflow_runs.models import ContentFactoryRun
from integrations.models import GoogleConnection
from startup_updates.models import UserStartupBinding
from vibe_raising.models import VibeRaisingCompany, VibeRaisingProfile


User = get_user_model()


def _json_response(payload):
    response = MagicMock()
    response.json.return_value = payload
    response.raise_for_status.return_value = None
    return response


@override_settings(
    GOOGLE_OAUTH_CLIENT_ID="google-client-id",
    GOOGLE_OAUTH_CLIENT_SECRET="google-client-secret",
    GOOGLE_OAUTH_REDIRECT_URI="http://localhost:8000/integrations/callback/google",
    DEFAULT_FRONTEND_URL="http://localhost:5173",
    MEDHACK_URL="http://localhost:3000",
    ESAFETY_URL="http://localhost:3001",
    VIBE_RAISING_URL="http://localhost:5173",
)
class GoogleOAuthViewTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_user(email="founder@example.com", role="participant")

    def _login_and_seed_oauth_state(self, *, next_url=None):
        self.client.force_login(self.user)
        session = self.client.session
        session["google_oauth_state"] = "google-state"
        if next_url is not None:
            session["google_oauth_next"] = next_url
        else:
            session.pop("google_oauth_next", None)
        session.save()

    def _set_access_token_cookie(self, user=None):
        refresh = RefreshToken.for_user(user or self.user)
        self.client.cookies["access_token"] = str(refresh.access_token)

    def test_google_connect_stores_state_and_validated_next(self):
        self.client.force_login(self.user)

        response = self.client.get(
            reverse("google_connect"),
            {"next": "http://localhost:5173/settings?from=gmail"},
        )

        self.assertEqual(response.status_code, 302)
        params = urllib.parse.parse_qs(urllib.parse.urlparse(response.url).query)
        self.assertEqual(params["client_id"], ["google-client-id"])
        self.assertEqual(params["redirect_uri"], ["http://localhost:8000/integrations/callback/google"])

        session = self.client.session
        self.assertEqual(
            session.get("google_oauth_next"),
            "http://localhost:5173/settings?from=gmail",
        )
        self.assertEqual(params["state"], [session["google_oauth_state"]])

    def test_google_connect_accepts_vibe_raising_next_url(self):
        self.client.force_login(self.user)

        response = self.client.get(
            reverse("google_connect"),
            {"next": "http://localhost:5173/vibe-raising/create-update?email_draft=1"},
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            self.client.session.get("google_oauth_next"),
            "http://localhost:5173/vibe-raising/create-update?email_draft=1",
        )

    def test_google_connect_accepts_platform_jwt_cookie_without_session(self):
        self._set_access_token_cookie()

        response = self.client.get(
            reverse("google_connect"),
            {"next": "http://localhost:5173/vibe-raising/create-update?email_draft=1"},
        )

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.url.startswith("https://accounts.google.com/o/oauth2/v2/auth?"))
        session = self.client.session
        self.assertEqual(str(self.user.id), session.get("_auth_user_id"))
        self.assertEqual(
            session.get("google_oauth_next"),
            "http://localhost:5173/vibe-raising/create-update?email_draft=1",
        )
        self.assertTrue(session.get("google_oauth_state"))

    def test_google_connect_redirects_unauthenticated_users_to_platform_login(self):
        response = self.client.get(
            reverse("google_connect"),
            {"next": "http://localhost:5173/vibe-raising/create-update?email_draft=1"},
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response.url,
            "http://localhost:5173/platform/login?app=vibe-raising&next=%2Fvibe-raising%2Fcreate-update%3Femail_draft%3D1",
        )

    def test_google_connect_ignores_invalid_next(self):
        self.client.force_login(self.user)

        response = self.client.get(
            reverse("google_connect"),
            {"next": "https://evil.example/phish"},
        )

        self.assertEqual(response.status_code, 302)
        self.assertNotIn("google_oauth_next", self.client.session)

    def test_google_callback_creates_connection_and_redirects_to_next(self):
        self._login_and_seed_oauth_state(next_url="/app/settings")

        with patch(
            "integrations.views.requests.post",
            return_value=_json_response(
                {
                    "access_token": "access-token",
                    "refresh_token": "refresh-token",
                    "scope": "https://www.googleapis.com/auth/gmail.readonly openid",
                }
            ),
        ), patch(
            "integrations.views.requests.get",
            return_value=_json_response({"email": "founder@gmail.com"}),
        ):
            with self.captureOnCommitCallbacks(execute=True):
                response = self.client.get(
                    reverse("google_callback"),
                    {"state": "google-state", "code": "oauth-code"},
                )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, "/app/settings")

        connection = GoogleConnection.objects.get(user=self.user)
        self.assertEqual(connection.google_email, "founder@gmail.com")
        self.assertEqual(connection.refresh_token, "refresh-token")
        self.assertEqual(
            connection.scope,
            "https://www.googleapis.com/auth/gmail.readonly openid",
        )
        self.assertNotIn("google_oauth_state", self.client.session)
        self.assertNotIn("google_oauth_next", self.client.session)

    def test_google_callback_saves_connection_and_redirects_to_vibe_raising_next(self):
        self._login_and_seed_oauth_state(
            next_url="http://localhost:5173/vibe-raising/create-update?email_draft=1",
        )

        with patch(
            "integrations.views.requests.post",
            return_value=_json_response(
                {
                    "access_token": "access-token",
                    "refresh_token": "refresh-token",
                    "scope": "https://www.googleapis.com/auth/gmail.readonly openid",
                }
            ),
        ), patch(
            "integrations.views.requests.get",
            return_value=_json_response({"email": "founder@gmail.com"}),
        ):
            response = self.client.get(
                reverse("google_callback"),
                {"state": "google-state", "code": "oauth-code"},
            )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response.url,
            "http://localhost:5173/vibe-raising/create-update?email_draft=1",
        )
        self.assertTrue(GoogleConnection.objects.filter(user=self.user, google_email="founder@gmail.com").exists())

    def test_google_callback_does_not_fetch_subject_preview(self):
        self._login_and_seed_oauth_state()

        with patch(
            "integrations.services.gmail.fetch_recent_subject_lines",
        ) as mock_fetch, patch(
            "integrations.views.requests.post",
            return_value=_json_response(
                {
                    "access_token": "access-token",
                    "refresh_token": "refresh-token",
                    "scope": "https://www.googleapis.com/auth/gmail.readonly openid",
                }
            ),
        ), patch(
            "integrations.views.requests.get",
            return_value=_json_response({"email": "founder@gmail.com"}),
        ):
            response = self.client.get(
                reverse("google_callback"),
                {"state": "google-state", "code": "oauth-code"},
            )

        self.assertEqual(response.status_code, 302)
        mock_fetch.assert_not_called()

    def test_google_callback_uses_default_frontend_when_next_missing(self):
        self._login_and_seed_oauth_state()

        with patch(
            "integrations.views.requests.post",
            return_value=_json_response(
                {
                    "access_token": "access-token",
                    "refresh_token": "refresh-token",
                    "scope": "https://www.googleapis.com/auth/gmail.readonly",
                }
            ),
        ), patch(
            "integrations.views.requests.get",
            return_value=_json_response({"email": "founder@gmail.com"}),
        ):
            response = self.client.get(
                reverse("google_callback"),
                {"state": "google-state", "code": "oauth-code"},
            )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, "http://localhost:3000/settings?gmail_connected=true")

    @override_settings(
        GOOGLE_OAUTH_REDIRECT_URI="http://localhost:8000/integrations/callback/google",
        DEFAULT_FRONTEND_URL="",
        MEDHACK_URL="",
        ESAFETY_URL="",
        VIBE_RAISING_URL="http://localhost:5173",
    )
    def test_google_callback_uses_vibe_raising_frontend_when_other_defaults_missing(self):
        self._login_and_seed_oauth_state()

        with patch(
            "integrations.views.requests.post",
            return_value=_json_response(
                {
                    "access_token": "access-token",
                    "refresh_token": "refresh-token",
                    "scope": "https://www.googleapis.com/auth/gmail.readonly",
                }
            ),
        ), patch(
            "integrations.views.requests.get",
            return_value=_json_response({"email": "founder@gmail.com"}),
        ):
            response = self.client.get(
                reverse("google_callback"),
                {"state": "google-state", "code": "oauth-code"},
            )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, "http://localhost:5173/settings?gmail_connected=true")

    @override_settings(
        GOOGLE_OAUTH_REDIRECT_URI="https://api.mlai.au/integrations/callback/google",
        DEFAULT_FRONTEND_URL="https://mlai.au",
        MEDHACK_URL="https://app.mlai.au",
        ESAFETY_URL="https://esafety.mlai.au",
    )
    def test_google_callback_uses_prod_redirect_uri_and_frontend_origin(self):
        self._login_and_seed_oauth_state()

        with patch(
            "integrations.views.requests.post",
            return_value=_json_response(
                {
                    "access_token": "access-token",
                    "refresh_token": "refresh-token",
                    "scope": "https://www.googleapis.com/auth/gmail.readonly",
                }
            ),
        ) as mock_post, patch(
            "integrations.views.requests.get",
            return_value=_json_response({"email": "founder@gmail.com"}),
        ):
            response = self.client.get(
                reverse("google_callback"),
                {"state": "google-state", "code": "oauth-code"},
            )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, "https://app.mlai.au/settings?gmail_connected=true")
        self.assertEqual(
            mock_post.call_args.kwargs["data"]["redirect_uri"],
            "https://api.mlai.au/integrations/callback/google",
        )

    def test_google_callback_updates_default_binding_without_starting_run(self):
        organization = Organization.objects.create(name="Acme", domain="acme.com")
        binding = UserStartupBinding.objects.create(
            user=self.user,
            organization=organization,
            is_default_for_gmail=True,
        )
        self._login_and_seed_oauth_state()

        with patch(
            "integrations.views.requests.post",
            return_value=_json_response(
                {
                    "access_token": "access-token",
                    "refresh_token": "refresh-token",
                    "scope": "https://www.googleapis.com/auth/gmail.readonly",
                }
            ),
        ), patch(
            "integrations.views.requests.get",
            return_value=_json_response({"email": "founder@gmail.com"}),
        ):
            with self.captureOnCommitCallbacks(execute=False) as callbacks:
                response = self.client.get(
                    reverse("google_callback"),
                    {"state": "google-state", "code": "oauth-code"},
                )

        self.assertEqual(response.status_code, 302)
        self.assertFalse(ContentFactoryRun.objects.filter(workflow="startup_monthly_update", domain="acme.com").exists())
        self.assertTrue(GoogleConnection.objects.filter(user=self.user, google_email="founder@gmail.com").exists())
        self.assertEqual(callbacks, [])

    def test_email_draft_start_creates_run_after_google_callback(self):
        founder_profile = VibeRaisingProfile.objects.create(
            user=self.user,
            role=VibeRaisingProfile.ROLE_FOUNDER,
        )
        company = VibeRaisingCompany.objects.create(
            profile=founder_profile,
            name="Acme",
            domain="acme.com",
            registered=True,
        )
        founder_profile.active_company = company
        founder_profile.save(update_fields=["active_company", "updated_at"])

        organization = Organization.objects.create(name="Acme", domain="acme.com")
        UserStartupBinding.objects.create(
            user=self.user,
            organization=organization,
            is_default_for_gmail=True,
        )
        self._login_and_seed_oauth_state()

        with patch(
            "vibe_raising.views.notify_valley_run_created",
        ) as mock_notify, patch(
            "integrations.views.requests.post",
            return_value=_json_response(
                {
                    "access_token": "access-token",
                    "refresh_token": "refresh-token",
                    "scope": "https://www.googleapis.com/auth/gmail.readonly",
                }
            ),
        ), patch(
            "integrations.views.requests.get",
            return_value=_json_response({"email": "founder@gmail.com"}),
        ):
            callback_response = self.client.get(
                reverse("google_callback"),
                {"state": "google-state", "code": "oauth-code"},
            )
            self.assertEqual(ContentFactoryRun.objects.filter(workflow="startup_monthly_update").count(), 0)
            api_client = APIClient()
            api_client.force_authenticate(user=self.user)
            with self.captureOnCommitCallbacks(execute=True):
                start_response = api_client.post(
                    "/api/v1/vibe-raising/email-draft/start/",
                    {},
                    format="json",
                )

        self.assertEqual(callback_response.status_code, 302)
        self.assertEqual(start_response.status_code, 201)
        self.assertEqual(ContentFactoryRun.objects.filter(workflow="startup_monthly_update").count(), 1)
        run = ContentFactoryRun.objects.get(workflow="startup_monthly_update", domain="acme.com")
        self.assertEqual(start_response.data["runId"], run.run_id)
        self.assertEqual(start_response.data["state"], "queued")
        mock_notify.assert_called_once_with(run.run_id)

    def test_google_callback_does_not_create_run_without_binding(self):
        self._login_and_seed_oauth_state()

        with patch(
            "integrations.views.requests.post",
            return_value=_json_response(
                {
                    "access_token": "access-token",
                    "refresh_token": "refresh-token",
                    "scope": "https://www.googleapis.com/auth/gmail.readonly",
                }
            ),
        ), patch(
            "integrations.views.requests.get",
            return_value=_json_response({"email": "founder@gmail.com"}),
        ):
            response = self.client.get(
                reverse("google_callback"),
                {"state": "google-state", "code": "oauth-code"},
            )

        self.assertEqual(response.status_code, 302)
        self.assertFalse(ContentFactoryRun.objects.filter(workflow="startup_monthly_update").exists())

    def test_google_callback_preserves_existing_refresh_token(self):
        GoogleConnection.objects.create(
            user=self.user,
            google_email="founder@gmail.com",
            refresh_token="stored-refresh-token",
            scope="https://www.googleapis.com/auth/gmail.readonly",
        )
        self._login_and_seed_oauth_state()

        with patch(
            "integrations.views.requests.post",
            return_value=_json_response(
                {
                    "access_token": "access-token",
                    "scope": "https://www.googleapis.com/auth/gmail.readonly openid",
                }
            ),
        ), patch(
            "integrations.views.requests.get",
            return_value=_json_response({"email": "founder+updated@gmail.com"}),
        ):
            response = self.client.get(
                reverse("google_callback"),
                {"state": "google-state", "code": "oauth-code"},
            )

        self.assertEqual(response.status_code, 302)
        connection = GoogleConnection.objects.get(user=self.user)
        self.assertEqual(connection.refresh_token, "stored-refresh-token")
        self.assertEqual(connection.google_email, "founder+updated@gmail.com")
        self.assertEqual(
            connection.scope,
            "https://www.googleapis.com/auth/gmail.readonly openid",
        )

    def test_google_callback_requires_refresh_token_for_first_connection(self):
        self._login_and_seed_oauth_state()

        with patch(
            "integrations.views.requests.post",
            return_value=_json_response({"access_token": "access-token"}),
        ):
            response = self.client.get(
                reverse("google_callback"),
                {"state": "google-state", "code": "oauth-code"},
            )

        self.assertEqual(response.status_code, 400)
        self.assertIn(b"refresh token", response.content)
        self.assertFalse(GoogleConnection.objects.filter(user=self.user).exists())

    def test_google_callback_rejects_invalid_state(self):
        self._login_and_seed_oauth_state()

        response = self.client.get(
            reverse("google_callback"),
            {"state": "wrong-state", "code": "oauth-code"},
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn(b"Invalid state", response.content)

    def test_google_callback_rejects_missing_code(self):
        self._login_and_seed_oauth_state()

        response = self.client.get(
            reverse("google_callback"),
            {"state": "google-state"},
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn(b"Missing code", response.content)

    def test_google_callback_rejects_provider_error(self):
        self._login_and_seed_oauth_state()

        response = self.client.get(
            reverse("google_callback"),
            {"state": "google-state", "error": "access_denied"},
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn(b"OAuth error: access_denied", response.content)
