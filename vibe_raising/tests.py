from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from .models import VibeRaisingCompany, VibeRaisingProfile


User = get_user_model()


class VibeRaisingApiTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            email="founder@example.com",
            password="password",
            first_name="Founder",
            last_name="User",
            role="participant",
        )

    def test_profile_requires_authentication(self):
        response = self.client.get("/api/v1/vibe-raising/profile/")
        self.assertEqual(response.status_code, 401)

    def test_get_profile_returns_404_before_onboarding(self):
        self.client.force_authenticate(user=self.user)
        response = self.client.get("/api/v1/vibe-raising/profile/")
        self.assertEqual(response.status_code, 404)

    def test_founder_profile_post_creates_profile(self):
        self.client.force_authenticate(user=self.user)

        response = self.client.post(
            "/api/v1/vibe-raising/profile/",
            {"role": "founder"},
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["role"], "founder")
        self.assertIsNone(response.data["organizationName"])
        self.assertEqual(response.data["companies"], [])
        self.assertIsNone(response.data["activeCompanyId"])

        profile = VibeRaisingProfile.objects.get(user=self.user)
        self.assertEqual(profile.role, VibeRaisingProfile.ROLE_FOUNDER)
        self.assertIsNone(profile.organization_name)

    def test_investor_profile_requires_organization_name(self):
        self.client.force_authenticate(user=self.user)

        response = self.client.post(
            "/api/v1/vibe-raising/profile/",
            {"role": "investor"},
            format="json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("organizationName", response.data)

    def test_profile_put_matches_post_behavior(self):
        self.client.force_authenticate(user=self.user)

        response = self.client.put(
            "/api/v1/vibe-raising/profile/",
            {"role": "investor", "organizationName": "Alpha Ventures"},
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["role"], "investor")
        self.assertEqual(response.data["organizationName"], "Alpha Ventures")
        self.assertEqual(response.data["companies"], [])
        self.assertIsNone(response.data["activeCompanyId"])

    def test_founder_can_create_first_company_and_it_becomes_active(self):
        self.client.force_authenticate(user=self.user)
        VibeRaisingProfile.objects.create(user=self.user, role=VibeRaisingProfile.ROLE_FOUNDER)

        response = self.client.post(
            "/api/v1/vibe-raising/companies/",
            {
                "name": "Acme Inc.",
                "domain": "acme.com",
                "abn": "123",
                "registered": False,
            },
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["name"], "Acme Inc.")
        self.assertEqual(response.data["domain"], "acme.com")

        profile = VibeRaisingProfile.objects.get(user=self.user)
        self.assertIsNotNone(profile.active_company_id)
        self.assertEqual(str(profile.active_company_id), response.data["id"])

    def test_founder_can_update_owned_company_by_company_id(self):
        self.client.force_authenticate(user=self.user)
        profile = VibeRaisingProfile.objects.create(user=self.user, role=VibeRaisingProfile.ROLE_FOUNDER)
        company = VibeRaisingCompany.objects.create(
            profile=profile,
            name="Acme Inc.",
            domain="old.example",
            registered=False,
        )

        response = self.client.post(
            "/api/v1/vibe-raising/companies/",
            {
                "companyId": str(company.id),
                "name": "Acme Inc.",
                "domain": "new.example",
                "registered": True,
            },
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        company.refresh_from_db()
        self.assertEqual(company.domain, "new.example")
        self.assertTrue(company.registered)

    def test_retry_create_same_name_does_not_duplicate_company(self):
        self.client.force_authenticate(user=self.user)
        VibeRaisingProfile.objects.create(user=self.user, role=VibeRaisingProfile.ROLE_FOUNDER)

        first = self.client.post(
            "/api/v1/vibe-raising/companies/",
            {"name": "Acme Inc.", "domain": "first.example"},
            format="json",
        )
        second = self.client.post(
            "/api/v1/vibe-raising/companies/",
            {"name": "acme inc.", "domain": "second.example"},
            format="json",
        )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(VibeRaisingCompany.objects.count(), 1)

        company = VibeRaisingCompany.objects.get()
        self.assertEqual(company.domain, "second.example")

    def test_investor_gets_403_on_company_endpoints(self):
        self.client.force_authenticate(user=self.user)
        VibeRaisingProfile.objects.create(
            user=self.user,
            role=VibeRaisingProfile.ROLE_INVESTOR,
            organization_name="Alpha Ventures",
        )

        company_response = self.client.post(
            "/api/v1/vibe-raising/companies/",
            {"name": "Acme Inc."},
            format="json",
        )
        active_response = self.client.post(
            "/api/v1/vibe-raising/active-company/",
            {"companyId": "33f3e9c7-85b0-458b-a3ee-7bb8b9f0d4f8"},
            format="json",
        )

        self.assertEqual(company_response.status_code, 403)
        self.assertEqual(active_response.status_code, 403)

    def test_switching_to_unowned_company_returns_404(self):
        self.client.force_authenticate(user=self.user)
        profile = VibeRaisingProfile.objects.create(user=self.user, role=VibeRaisingProfile.ROLE_FOUNDER)
        other_user = User.objects.create_user(email="other@example.com", password="password")
        other_profile = VibeRaisingProfile.objects.create(user=other_user, role=VibeRaisingProfile.ROLE_FOUNDER)
        other_company = VibeRaisingCompany.objects.create(profile=other_profile, name="Other Co")

        response = self.client.post(
            "/api/v1/vibe-raising/active-company/",
            {"companyId": str(other_company.id)},
            format="json",
        )

        self.assertEqual(response.status_code, 404)
        profile.refresh_from_db()
        self.assertIsNone(profile.active_company_id)
