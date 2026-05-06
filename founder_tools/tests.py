from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from .models import VibeRaisingCompany, VibeRaisingProfile


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

        response = self.client.post(
            "/api/v1/founder-tools/companies/",
            {
                "name": "Acme Inc.",
                "domain": "https://www.acme.com",
                "companyLinkedInUrl": "https://www.linkedin.com/company/acme",
                "companyContext": "Acme helps founders write investor updates.",
                "organizationKind": "For-profit",
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
        second = self.client.post(
            "/api/v1/founder-tools/companies/",
            {
                "name": "acme inc.",
                "domain": "second.example",
                "companyLinkedInUrl": "https://www.linkedin.com/company/acme-second",
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
