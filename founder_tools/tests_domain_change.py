"""Win 5: a domain edit must never silently strand a company's data.

Organization is the tenant boundary (connections, Gmail, article runs, monthly
updates all hang off it) and used to be swapped by a plain
``get_or_create(domain=...)`` whenever a company's domain changed — a typo fix
made the company's entire history vanish. Now the org is renamed in place when
that is safe, and an unsafe re-point 409s until explicitly confirmed.
"""

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from founder_tools.models import VibeRaisingCompany, VibeRaisingProfile
from integrations.models import GoogleConnection
from organizations.models import Organization

User = get_user_model()

COMPANIES_URL = "/api/v1/founder-tools/companies/"


class CompanyDomainChangeGuardTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            email="domain-change@example.com",
            password="password",
            first_name="Domain",
            last_name="Change",
            role="participant",
        )
        self.client.force_authenticate(user=self.user)

    def _create_company(self, name="Acme Inc.", domain="acme.com"):
        response = self.client.post(
            COMPANIES_URL, {"name": name, "domain": domain, "registered": True}, format="json"
        )
        self.assertEqual(response.status_code, 200)
        return VibeRaisingCompany.objects.select_related("organization").get(pk=response.data["id"])

    def _edit(self, company, domain, confirm=False, name=None):
        body = {"companyId": str(company.id), "name": name or company.name, "domain": domain}
        if confirm:
            body["confirmDomainChange"] = True
        return self.client.post(COMPANIES_URL, body, format="json")

    def _add_gmail(self, organization, email="mailbox@example.com"):
        return GoogleConnection.objects.create(
            user=self.user,
            organization=organization,
            google_email=email,
            refresh_token="token",
            scope="",
        )

    def test_safe_domain_edit_renames_the_org_in_place(self):
        company = self._create_company()
        organization = company.organization
        connection = self._add_gmail(organization)

        response = self._edit(company, "acme-rebrand.com")
        self.assertEqual(response.status_code, 200)

        company.refresh_from_db()
        organization.refresh_from_db()
        connection.refresh_from_db()
        # Same tenant, new domain — everything attached to the org followed.
        self.assertEqual(company.organization_id, organization.id)
        self.assertEqual(organization.domain, "acme-rebrand.com")
        self.assertEqual(company.domain, "acme-rebrand.com")
        self.assertEqual(connection.organization_id, organization.id)

    def test_unsafe_domain_edit_with_data_requires_confirmation(self):
        company = self._create_company()
        old_org = company.organization
        self._add_gmail(old_org)
        # The target domain already has an Organization (e.g. content-factory
        # only), so the org cannot be renamed onto it — moving strands data.
        Organization.objects.create(name="Taken", domain="taken.example")

        response = self._edit(company, "taken.example")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["code"], "company_domain_change_moves_data")
        self.assertEqual(response.data["currentDomain"], "acme.com")
        self.assertEqual(response.data["newDomain"], "taken.example")
        self.assertEqual(response.data["data"]["gmailConnections"], 1)

        company.refresh_from_db()
        self.assertEqual(company.domain, "acme.com")
        self.assertEqual(company.organization_id, old_org.id)

    def test_confirmed_unsafe_edit_repoints_and_leaves_data_on_old_org(self):
        company = self._create_company()
        old_org = company.organization
        connection = self._add_gmail(old_org)
        target = Organization.objects.create(name="Taken", domain="taken.example")

        response = self._edit(company, "taken.example", confirm=True)
        self.assertEqual(response.status_code, 200)

        company.refresh_from_db()
        connection.refresh_from_db()
        self.assertEqual(company.organization_id, target.id)
        # The stranding is explicit and confirmed; the data stays discoverable
        # on the old org rather than being deleted.
        self.assertEqual(connection.organization_id, old_org.id)

    def test_dataless_org_repoints_without_confirmation(self):
        company = self._create_company()
        target = Organization.objects.create(name="Taken", domain="taken2.example")

        response = self._edit(company, "taken2.example")
        self.assertEqual(response.status_code, 200)
        company.refresh_from_db()
        self.assertEqual(company.organization_id, target.id)

    def test_org_shared_with_another_founder_is_never_renamed(self):
        company = self._create_company()
        shared_org = company.organization
        other_user = User.objects.create_user(
            email="cofounder@example.com",
            password="password",
            first_name="Co",
            last_name="Founder",
            role="participant",
        )
        other_profile = VibeRaisingProfile.objects.create(
            user=other_user, role=VibeRaisingProfile.ROLE_FOUNDER
        )
        # Legacy shape: a second founder's company bound to the same org.
        VibeRaisingCompany.objects.create(
            profile=other_profile, name="Acme Twin", domain="acme.com", organization=shared_org
        )

        response = self._edit(company, "solo.example")
        self.assertEqual(response.status_code, 200)

        shared_org.refresh_from_db()
        company.refresh_from_db()
        # The other founder keeps their tenant domain; this company moved to a
        # fresh org instead of hijacking the shared one.
        self.assertEqual(shared_org.domain, "acme.com")
        self.assertNotEqual(company.organization_id, shared_org.id)
        self.assertEqual(company.organization.domain, "solo.example")

    def test_creating_a_company_on_another_founders_domain_is_blocked(self):
        stranger = User.objects.create_user(
            email="stranger-domain@example.com",
            password="password",
            first_name="Str",
            last_name="Anger",
            role="participant",
        )
        stranger_client = APIClient()
        stranger_client.force_authenticate(user=stranger)
        claimed = stranger_client.post(
            COMPANIES_URL, {"name": "Bar Pty", "domain": "bar.example", "registered": True}, format="json"
        )
        self.assertEqual(claimed.status_code, 200)

        response = self.client.post(
            COMPANIES_URL, {"name": "Bar Impostor", "domain": "bar.example", "registered": True}, format="json"
        )
        self.assertEqual(response.status_code, 409)
        self.assertIn("already linked", str(response.data["detail"]))
        self.assertEqual(VibeRaisingCompany.objects.filter(profile__user=self.user).count(), 0)

    def test_twin_endpoint_honours_the_same_guard(self):
        company = self._create_company()
        self._add_gmail(company.organization)
        Organization.objects.create(name="Taken", domain="taken3.example")

        response = self.client.post(
            "/api/v1/vibe-raising/companies/",
            {"companyId": str(company.id), "name": company.name, "domain": "taken3.example"},
            format="json",
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["code"], "company_domain_change_moves_data")
