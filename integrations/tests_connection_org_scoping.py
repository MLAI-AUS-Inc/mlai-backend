from django.contrib.auth import get_user_model
from django.test import TestCase

from founder_tools.models import VibeRaisingCompany, VibeRaisingProfile
from integrations.models import (
    ExternalServiceConnection,
    ExternalServiceProvider,
)
from integrations.services.external_connectors import (
    _upsert_connection,
    active_organization_for_user,
    serialize_source_status,
)
from organizations.models import Organization

User = get_user_model()


class ConnectionOrgScopingTests(TestCase):
    """Phase 1: each startup (Organization) holds its own connection per provider;
    connecting for one startup must not overwrite a sibling's, and reads/status
    scope to the active startup."""

    def setUp(self):
        self.user = User.objects.create_user(
            email="multi-conn@example.com",
            password="password",
            first_name="Multi",
            last_name="Conn",
            role="participant",
        )
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
        self._set_active(self.company_a)

    def _set_active(self, company):
        self.profile.active_company = company
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
            provider_metadata={},
        )

    def test_active_organization_follows_active_company(self):
        self.assertEqual(active_organization_for_user(self.user), self.org_a)
        self._set_active(self.company_b)
        self.assertEqual(active_organization_for_user(self.user), self.org_b)

    def test_connecting_second_startup_does_not_overwrite_first(self):
        conn_a = self._connect_notion(self.org_a, "token-a")
        conn_b = self._connect_notion(self.org_b, "token-b")

        self.assertNotEqual(conn_a.id, conn_b.id)
        self.assertEqual(
            ExternalServiceConnection.objects.filter(
                user=self.user, provider=ExternalServiceProvider.NOTION
            ).count(),
            2,
        )
        conn_a.refresh_from_db()
        self.assertEqual(conn_a.organization_id, self.org_a.id)
        self.assertEqual(conn_a.access_token, "token-a")  # untouched by B's connect

    def test_reconnecting_same_startup_updates_in_place(self):
        first = self._connect_notion(self.org_a, "token-a")
        again = self._connect_notion(self.org_a, "token-a2")
        self.assertEqual(first.id, again.id)
        self.assertEqual(
            ExternalServiceConnection.objects.filter(
                user=self.user, provider=ExternalServiceProvider.NOTION
            ).count(),
            1,
        )

    def test_status_shows_only_active_startups_connection(self):
        conn_a = self._connect_notion(self.org_a, "token-a")
        conn_b = self._connect_notion(self.org_b, "token-b")

        def notion_source(payload):
            return next(
                s for s in payload["sources"] if s["provider"] == ExternalServiceProvider.NOTION
            )

        self._set_active(self.company_a)
        self.assertEqual(notion_source(serialize_source_status(self.user))["connectionId"], conn_a.id)

        self._set_active(self.company_b)
        self.assertEqual(notion_source(serialize_source_status(self.user))["connectionId"], conn_b.id)

    def test_unique_constraint_blocks_duplicate_rows(self):
        from django.db import IntegrityError, transaction

        self._connect_notion(self.org_a, "token-a")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                ExternalServiceConnection.objects.create(
                    user=self.user,
                    provider=ExternalServiceProvider.NOTION,
                    organization=self.org_a,
                    external_account_id="",
                )
