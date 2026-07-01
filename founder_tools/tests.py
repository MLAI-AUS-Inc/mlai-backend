from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

# Mocked ABR response for a verified registered company (ABN 89 000 000 019).
_VERIFIED_ABN = "89000000019"
_VERIFIED_ACN = "000000019"


def _abr_company(abn):
    return {
        "configured": True,
        "reachable": True,
        "found": True,
        "is_company": True,
        "acn": _VERIFIED_ACN,
        "entity_type_code": "PRV",
    }


_PATCH_ABR = "content_factory.vibe_marketing_views.verify_company_with_abr"

from .models import VibeRaisingCompany, VibeRaisingProfile
from .services import ensure_company_organization
from organizations.models import Organization


User = get_user_model()


class FounderToolsCompanyApiTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            email="founder-tools@example.com",
            password="password",
            first_name="Founder",
            last_name="Tools",
            role="participant",
        )

    def test_founder_can_create_first_company(self):
        self.client.force_authenticate(user=self.user)

        with patch(_PATCH_ABR, side_effect=_abr_company):
            response = self.client.post(
                "/api/v1/founder-tools/companies/",
                {
                    "name": "Acme Inc.",
                    "domain": "https://www.acme.com",
                    "companyLinkedInUrl": "https://www.linkedin.com/company/acme",
                    "companyContext": "Acme helps founders write investor updates.",
                    "organizationKind": "For-profit",
                    "abn": _VERIFIED_ABN,
                    "registered": True,
                },
                format="json",
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["name"], "Acme Inc.")
        self.assertEqual(response.data["domain"], "acme.com")
        self.assertEqual(response.data["companyLinkedInUrl"], "https://www.linkedin.com/company/acme")

        profile = VibeRaisingProfile.objects.get(user=self.user)
        company = VibeRaisingCompany.objects.get(profile=profile)
        self.assertEqual(profile.active_company_id, company.id)
        self.assertEqual(company.organization.domain, "acme.com")
        self.assertEqual(company.organization.company_linkedin_url, "https://www.linkedin.com/company/acme")

    def test_personal_linkedin_url_returns_field_validation_error(self):
        self.client.force_authenticate(user=self.user)

        response = self.client.post(
            "/api/v1/founder-tools/companies/",
            {
                "name": "Acme Inc.",
                "domain": "acme.com",
                "companyLinkedInUrl": "https://www.linkedin.com/in/founder",
                "registered": True,
            },
            format="json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("companyLinkedInUrl", response.data)
        self.assertEqual(
            str(response.data["companyLinkedInUrl"][0]),
            "Enter a LinkedIn company URL, not a personal profile URL.",
        )
        self.assertEqual(VibeRaisingCompany.objects.count(), 0)

    def test_retry_create_same_name_updates_existing_company(self):
        self.client.force_authenticate(user=self.user)

        first = self.client.post(
            "/api/v1/founder-tools/companies/",
            {
                "name": "Acme Inc.",
                "domain": "first.example",
                "companyLinkedInUrl": "https://www.linkedin.com/company/acme-first",
                "registered": False,
            },
            format="json",
        )
        with patch(_PATCH_ABR, side_effect=_abr_company):
            second = self.client.post(
                "/api/v1/founder-tools/companies/",
                {
                    "name": "acme inc.",
                    "domain": "second.example",
                    "companyLinkedInUrl": "https://www.linkedin.com/company/acme-second",
                    "abn": _VERIFIED_ABN,
                    "registered": True,
                },
                format="json",
            )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(VibeRaisingCompany.objects.count(), 1)
        self.assertEqual(first.data["id"], second.data["id"])

        company = VibeRaisingCompany.objects.get()
        self.assertEqual(company.name, "acme inc.")
        self.assertEqual(company.domain, "second.example")
        self.assertTrue(company.registered)
        self.assertEqual(company.organization.domain, "second.example")
        self.assertEqual(company.organization.company_linkedin_url, "https://www.linkedin.com/company/acme-second")

    def test_ensure_company_organization_skips_unchanged_company_save(self):
        profile = VibeRaisingProfile.objects.create(user=self.user, role=VibeRaisingProfile.ROLE_FOUNDER)
        organization = Organization.objects.create(name="Acme Inc.", domain="acme.com")
        company = VibeRaisingCompany.objects.create(
            profile=profile,
            organization=organization,
            name="Acme Inc.",
            domain="acme.com",
            registered=True,
        )

        with CaptureQueriesContext(connection) as captured:
            resolved = ensure_company_organization(company)

        self.assertEqual(resolved, organization)
        company_updates = [
            query["sql"]
            for query in captured.captured_queries
            if 'UPDATE "vibe_raising_viberaisingcompany"' in query["sql"]
        ]
        self.assertEqual(company_updates, [])
