"""Win 6: a founder can remove a company, and doing so revokes its integrations
and purges its data.

Before this, there was no delete path at all — a shut-down startup kept its
OAuth tokens live and its data forever. The DELETE endpoint offboards the
company: this user's connections/Gmail for the org are revoked and removed, the
org's own data is purged (only when the org isn't shared with a co-founder), the
active company is re-pointed, and the company row is deleted (freeing the
domain).
"""

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from content_factory.models import OrganizationContentConfig
from founder_tools.models import VibeRaisingCompany, VibeRaisingProfile
from integrations.models import (
    ExternalServiceConnection,
    ExternalServiceProvider,
    GoogleConnection,
)
from organizations.models import Organization
from startup_updates.models import MonthlyUpdateDraft

User = get_user_model()

# Offboarding revokes Google tokens over HTTP; stub it in tests.
_REVOKE = "startup_updates.data_deletion._revoke_google_refresh_token"
_REVOKE_OK = {"requested": True, "succeeded": True, "warning": None}


def _company_url(company_id):
    return f"/api/v1/founder-tools/companies/{company_id}/"


class CompanyOffboardingTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            email="offboard@example.com",
            password="password",
            first_name="Off",
            last_name="Board",
            role="participant",
        )
        self.client.force_authenticate(user=self.user)

    def _create_company(self, name, domain):
        response = self.client.post(
            "/api/v1/founder-tools/companies/",
            {"name": name, "domain": domain, "registered": True},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        return VibeRaisingCompany.objects.select_related("organization").get(pk=response.data["id"])

    def _add_connection(self, organization, provider=ExternalServiceProvider.NOTION):
        return ExternalServiceConnection.objects.create(
            user=self.user,
            organization=organization,
            provider=provider,
            status="connected",
            access_token="secret",
            refresh_token="secret",
        )

    def _add_gmail(self, organization, email="mailbox@example.com"):
        return GoogleConnection.objects.create(
            user=self.user,
            organization=organization,
            google_email=email,
            refresh_token="token",
            scope="",
        )

    def _add_monthly_update(self, organization, month):
        return MonthlyUpdateDraft.objects.create(
            organization=organization,
            month=month,
            title="Update",
            model_name="test",
        )

    def test_delete_revokes_connections_and_purges_data_and_frees_domain(self):
        company = self._create_company("Acme Inc.", "acme.com")
        organization = company.organization
        self._add_connection(organization)
        self._add_gmail(organization)
        from datetime import date

        self._add_monthly_update(organization, date(2026, 6, 1))
        # A content config exists from create; confirm it will be purged.
        self.assertTrue(OrganizationContentConfig.objects.filter(organization=organization).exists())

        with patch(_REVOKE, return_value=_REVOKE_OK) as revoke:
            response = self.client.delete(_company_url(company.id))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["offboarding"]["connectionsRemoved"], 1)
        self.assertTrue(response.data["offboarding"]["gmailDisconnected"])
        self.assertTrue(response.data["offboarding"]["orgDataPurged"])
        revoke.assert_called_once()

        # Company gone; domain freed; tokens and data purged.
        self.assertFalse(VibeRaisingCompany.objects.filter(pk=company.id).exists())
        self.assertFalse(ExternalServiceConnection.objects.filter(organization=organization).exists())
        self.assertFalse(GoogleConnection.objects.filter(organization=organization).exists())
        self.assertFalse(MonthlyUpdateDraft.objects.filter(organization=organization).exists())
        self.assertFalse(OrganizationContentConfig.objects.filter(organization=organization).exists())
        # Organization shell is retained for re-registration.
        self.assertTrue(Organization.objects.filter(pk=organization.pk).exists())

        # The freed domain can be registered again.
        again = self._create_company("Acme Again", "acme.com")
        self.assertEqual(again.domain, "acme.com")

    def test_delete_repoints_active_company_to_a_sibling(self):
        company_a = self._create_company("Alpha", "alpha.example")
        company_b = self._create_company("Beta", "beta.example")
        # A is active (first created).
        profile = VibeRaisingProfile.objects.get(user=self.user)
        self.assertEqual(profile.active_company_id, company_a.id)

        with patch(_REVOKE, return_value=_REVOKE_OK):
            response = self.client.delete(_company_url(company_a.id))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["offboarding"]["newActiveCompanyId"], str(company_b.id))
        profile.refresh_from_db()
        self.assertEqual(profile.active_company_id, company_b.id)

    def test_deleting_the_last_company_clears_active(self):
        company = self._create_company("Solo", "solo.example")
        with patch(_REVOKE, return_value=_REVOKE_OK):
            response = self.client.delete(_company_url(company.id))

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.data["offboarding"]["newActiveCompanyId"])
        profile = VibeRaisingProfile.objects.get(user=self.user)
        self.assertIsNone(profile.active_company_id)
        self.assertEqual(profile.companies.count(), 0)

    def test_shared_org_spares_org_level_data_but_removes_this_users_tokens(self):
        company = self._create_company("Acme Inc.", "acme.com")
        organization = company.organization
        self._add_connection(organization)
        from datetime import date

        self._add_monthly_update(organization, date(2026, 6, 1))

        # A second founder shares the org (legacy first-claim tenancy).
        other = User.objects.create_user(
            email="cofounder@example.com",
            password="password",
            first_name="Co",
            last_name="Founder",
            role="participant",
        )
        other_profile = VibeRaisingProfile.objects.create(
            user=other, role=VibeRaisingProfile.ROLE_FOUNDER
        )
        VibeRaisingCompany.objects.create(
            profile=other_profile, name="Acme Twin", domain="acme.com", organization=organization
        )

        with patch(_REVOKE, return_value=_REVOKE_OK):
            response = self.client.delete(_company_url(company.id))

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["offboarding"]["orgShared"])
        self.assertFalse(response.data["offboarding"]["orgDataPurged"])
        # This user's tokens are gone...
        self.assertFalse(ExternalServiceConnection.objects.filter(user=self.user, organization=organization).exists())
        # ...but the shared org's data is preserved for the co-founder.
        self.assertTrue(MonthlyUpdateDraft.objects.filter(organization=organization).exists())
        self.assertTrue(OrganizationContentConfig.objects.filter(organization=organization).exists())

    def test_cannot_delete_another_founders_company(self):
        stranger = User.objects.create_user(
            email="stranger-offboard@example.com",
            password="password",
            first_name="Str",
            last_name="Anger",
            role="participant",
        )
        stranger_client = APIClient()
        stranger_client.force_authenticate(user=stranger)
        stranger_resp = stranger_client.post(
            "/api/v1/founder-tools/companies/",
            {"name": "Theirs", "domain": "theirs.example", "registered": True},
            format="json",
        )
        stranger_company_id = stranger_resp.data["id"]

        response = self.client.delete(_company_url(stranger_company_id))
        self.assertEqual(response.status_code, 404)
        self.assertTrue(VibeRaisingCompany.objects.filter(pk=stranger_company_id).exists())

    def test_delete_unknown_company_is_404(self):
        import uuid

        response = self.client.delete(_company_url(uuid.uuid4()))
        self.assertEqual(response.status_code, 404)
