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
from .services import ensure_company_organization, get_founder_company_context
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


class FounderCompanyContextScopingTests(TestCase):
    """Phase 4: a per-request company_id scopes the read without thrashing the
    profile's shared active_company. Only explicit switches / write flows
    (persist_active=True) may persist the selection."""

    def setUp(self):
        self.user = User.objects.create_user(
            email="multi-startup@example.com",
            password="password",
            first_name="Multi",
            last_name="Startup",
            role="participant",
        )
        self.profile = VibeRaisingProfile.objects.create(
            user=self.user, role=VibeRaisingProfile.ROLE_FOUNDER
        )
        self.company_a = VibeRaisingCompany.objects.create(
            profile=self.profile, name="Alpha", domain="alpha.example"
        )
        self.company_b = VibeRaisingCompany.objects.create(
            profile=self.profile, name="Beta", domain="beta.example"
        )
        self.profile.active_company = self.company_a
        self.profile.save(update_fields=["active_company", "updated_at"])

    def test_read_with_company_id_scopes_without_repinning_active_company(self):
        context = get_founder_company_context(self.user, company_id=self.company_b.id)

        # The request is scoped to company B...
        self.assertEqual(context.company.id, self.company_b.id)
        self.assertEqual(context.organization.domain, "beta.example")

        # ...but the shared active company is unchanged (still A).
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.active_company_id, self.company_a.id)

    def test_persist_active_switches_active_company(self):
        context = get_founder_company_context(
            self.user, company_id=self.company_b.id, persist_active=True
        )

        self.assertEqual(context.company.id, self.company_b.id)
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.active_company_id, self.company_b.id)

    def test_read_without_company_id_uses_active_company(self):
        context = get_founder_company_context(self.user)

        self.assertEqual(context.company.id, self.company_a.id)
        self.assertEqual(context.organization.domain, "alpha.example")

    def test_company_id_for_other_users_company_is_rejected(self):
        other_user = User.objects.create_user(
            email="intruder@example.com",
            password="password",
            first_name="In",
            last_name="Truder",
            role="participant",
        )
        VibeRaisingProfile.objects.create(
            user=other_user, role=VibeRaisingProfile.ROLE_FOUNDER
        )

        with self.assertRaises(VibeRaisingCompany.DoesNotExist):
            get_founder_company_context(other_user, company_id=self.company_a.id)


class DuplicateCompanyDomainGuardTests(TestCase):
    """Phase 3a: a profile cannot register two companies on the same domain."""

    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            email="dup-domain@example.com",
            password="password",
            first_name="Dup",
            last_name="Domain",
            role="participant",
        )
        self.client.force_authenticate(user=self.user)

    def _create(self, name, domain, company_id=None):
        body = {"name": name, "domain": domain, "registered": True}
        if company_id is not None:
            body["companyId"] = str(company_id)
        return self.client.post("/api/v1/founder-tools/companies/", body, format="json")

    def test_second_company_with_same_domain_is_blocked(self):
        first = self._create("Acme Inc.", "https://www.acme.com")
        self.assertEqual(first.status_code, 200)

        second = self._create("Beta Corp", "acme.com")  # different name, same normalized domain
        self.assertEqual(second.status_code, 409)
        self.assertEqual(second.data["code"], "duplicate_company_domain")
        self.assertEqual(second.data["field"], "domain")
        self.assertEqual(second.data["companyId"], first.data["id"])

        # Only the first company exists.
        self.assertEqual(VibeRaisingCompany.objects.filter(domain="acme.com").count(), 1)

    def test_resaving_same_company_with_its_own_domain_is_allowed(self):
        first = self._create("Acme Inc.", "acme.com")
        self.assertEqual(first.status_code, 200)

        # Same name → resolves to the same company → no conflict with itself.
        again = self._create("Acme Inc.", "acme.com")
        self.assertEqual(again.status_code, 200)
        self.assertEqual(again.data["id"], first.data["id"])
        self.assertEqual(VibeRaisingCompany.objects.count(), 1)

    def test_distinct_domains_coexist(self):
        first = self._create("Acme Inc.", "acme.com")
        second = self._create("Beta Corp", "beta.com")
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(VibeRaisingCompany.objects.count(), 2)

    def test_editing_company_to_a_siblings_domain_is_blocked(self):
        first = self._create("Acme Inc.", "acme.com")
        second = self._create("Beta Corp", "beta.com")
        self.assertEqual(second.status_code, 200)

        # Try to move Beta onto Acme's domain via companyId update.
        conflict = self._create("Beta Corp", "acme.com", company_id=second.data["id"])
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(conflict.data["companyId"], first.data["id"])
