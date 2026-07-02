"""Win 4: connector surfaces honour an explicit per-request company_id.

The Data Sources page previously operated on whatever active_company happened
to be pinned server-side — a founder viewing company B while another tab
switched back to A would read (and disconnect!) A's connections. These tests
pin the new contract: company_id scopes status/sync/previews/disconnect and
the OAuth connect start, with ownership enforced.
"""

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from founder_tools.models import VibeRaisingCompany, VibeRaisingProfile
from integrations.models import (
    ExternalServiceConnection,
    ExternalServiceProvider,
    GoogleConnection,
)
from integrations.services.external_connectors import _upsert_connection
from organizations.models import Organization

User = get_user_model()

STATUS_URL = "/api/v1/integrations/sources/status"


class ConnectorCompanyScopingTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            email="scoped-conn@example.com",
            password="password",
            first_name="Scoped",
            last_name="Conn",
            role="participant",
        )
        self.client.force_authenticate(user=self.user)
        self.org_a = Organization.objects.create(name="Alpha", domain="alpha.example")
        self.org_b = Organization.objects.create(name="Beta", domain="beta.example")
        self.profile = VibeRaisingProfile.objects.create(
            user=self.user, role=VibeRaisingProfile.ROLE_FOUNDER
        )
        self.company_a = VibeRaisingCompany.objects.create(
            profile=self.profile, name="Alpha", domain="alpha.example", organization=self.org_a
        )
        self.company_b = VibeRaisingCompany.objects.create(
            profile=self.profile, name="Beta", domain="beta.example", organization=self.org_b
        )
        self.profile.active_company = self.company_a
        self.profile.save(update_fields=["active_company", "updated_at"])

    def _connect_notion(self, organization, token):
        return _upsert_connection(
            user=self.user,
            provider=ExternalServiceProvider.NOTION,
            organization=organization,
            access_token=token,
            refresh_token="",
            token_type="bearer",
            token_expires_at=None,
            scopes=[],
            external_account_id="",
            account_label=organization.name,
        )

    def _notion_status(self, payload):
        for source in payload["sources"]:
            if source["provider"] == ExternalServiceProvider.NOTION:
                return source
        raise AssertionError("notion source missing from status payload")

    def test_status_scopes_to_requested_company(self):
        self._connect_notion(self.org_b, "token-b")

        # Active company (A) has no Notion; explicit company_id=B sees B's.
        default_status = self._notion_status(self.client.get(STATUS_URL).data)
        self.assertEqual(default_status["status"], "not_connected")

        scoped = self.client.get(STATUS_URL, {"company_id": str(self.company_b.id)})
        self.assertEqual(scoped.status_code, 200)
        self.assertEqual(self._notion_status(scoped.data)["status"], "connected")

    def test_status_rejects_a_company_the_user_does_not_own(self):
        stranger = User.objects.create_user(
            email="stranger-conn@example.com",
            password="password",
            first_name="Str",
            last_name="Anger",
            role="participant",
        )
        stranger_profile = VibeRaisingProfile.objects.create(
            user=stranger, role=VibeRaisingProfile.ROLE_FOUNDER
        )
        stranger_org = Organization.objects.create(name="Gamma", domain="gamma.example")
        stranger_company = VibeRaisingCompany.objects.create(
            profile=stranger_profile, name="Gamma", domain="gamma.example", organization=stranger_org
        )

        response = self.client.get(STATUS_URL, {"company_id": str(stranger_company.id)})
        self.assertEqual(response.status_code, 404)

    def test_sync_scopes_to_requested_company(self):
        self._connect_notion(self.org_a, "token-a")
        connection_b = self._connect_notion(self.org_b, "token-b")

        response = self.client.post(
            "/api/v1/integrations/sources/sync",
            {"providers": ["notion"], "company_id": str(self.company_b.id)},
            format="json",
        )
        self.assertEqual(response.status_code, 202)
        synced_ids = {run["connectionId"] for run in response.data["syncRuns"]}
        self.assertEqual(synced_ids, {connection_b.id})

    def test_gmail_disconnect_targets_requested_company_mailbox(self):
        connection_a = GoogleConnection.objects.create(
            user=self.user,
            organization=self.org_a,
            google_email="a@example.com",
            refresh_token="token-a",
            scope="",
        )
        GoogleConnection.objects.create(
            user=self.user,
            organization=self.org_b,
            google_email="b@example.com",
            refresh_token="token-b",
            scope="",
        )

        response = self.client.delete(
            f"/api/v1/integrations/gmail/connection?company_id={self.company_b.id}",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["googleAccount"], "b@example.com")

        remaining = GoogleConnection.objects.filter(user=self.user)
        self.assertEqual(list(remaining.values_list("id", flat=True)), [connection_a.id])

    def test_google_connect_stamps_requested_company_org_in_session(self):
        self.client.force_login(self.user)
        response = self.client.get(
            "/integrations/connect/google",
            {"company_id": str(self.company_b.id)},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.client.session.get("google_oauth_org_id"), self.org_b.id)

    def test_google_connect_rejects_foreign_company(self):
        self.client.force_login(self.user)
        stranger = User.objects.create_user(
            email="stranger2@example.com",
            password="password",
            first_name="Str",
            last_name="Anger",
            role="participant",
        )
        stranger_profile = VibeRaisingProfile.objects.create(
            user=stranger, role=VibeRaisingProfile.ROLE_FOUNDER
        )
        stranger_company = VibeRaisingCompany.objects.create(
            profile=stranger_profile, name="Delta", domain="delta.example"
        )

        response = self.client.get(
            "/integrations/connect/google",
            {"company_id": str(stranger_company.id)},
        )
        self.assertEqual(response.status_code, 400)
