"""Win 8: scheduled monthly updates are backend-driven per company, self-serve.

Previously the valley scheduler dispatched only companies hardcoded in an env
allowlist on the droplet, so a founder's second startup got no scheduled updates
until ops edited server config. Now a per-company opt-in flag on the founder's
binding drives it: the founder toggles it in-app, and valley reads the enabled
set from the monthly-dispatch-targets endpoint.
"""

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from founder_tools.models import VibeRaisingCompany, VibeRaisingProfile
from organizations.models import Organization
from startup_updates.models import UserStartupBinding

User = get_user_model()

API_KEY = "test-roo-key"
TARGETS_URL = "/api/v1/integrations/startup-updates/monthly-dispatch-targets"


@override_settings(ROO_API_KEY=API_KEY)
class MonthlyDispatchTargetsViewTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            email="dispatch@example.com", password="pw", first_name="D", last_name="T", role="participant"
        )
        self.org_a = Organization.objects.create(name="Acme", domain="acme.com")
        self.org_b = Organization.objects.create(name="Beta", domain="beta.com")

    def test_returns_only_enabled_bindings_with_a_domain(self):
        enabled = UserStartupBinding.objects.create(
            user=self.user, organization=self.org_a, monthly_updates_enabled=True
        )
        UserStartupBinding.objects.create(
            user=self.user, organization=self.org_b, monthly_updates_enabled=False
        )
        # Enabled but domainless org is excluded (nothing to dispatch to).
        domainless = Organization.objects.create(name="Ghost", domain="")
        UserStartupBinding.objects.create(
            user=self.user, organization=domainless, monthly_updates_enabled=True
        )

        response = self.client.get(TARGETS_URL, HTTP_X_API_KEY=API_KEY)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 1)
        target = response.data["targets"][0]
        self.assertEqual(target["user_id"], self.user.id)
        self.assertEqual(target["domain"], "acme.com")
        self.assertEqual(target["organization_id"], self.org_a.id)
        self.assertEqual(target["binding_id"], enabled.id)

    def test_requires_service_key(self):
        response = self.client.get(TARGETS_URL)
        self.assertEqual(response.status_code, 403)


class FounderMonthlyUpdatesToggleTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            email="toggle@example.com", password="pw", first_name="T", last_name="G", role="participant"
        )
        self.client.force_authenticate(user=self.user)

    def _create_company(self, name="Acme", domain="acme.com"):
        response = self.client.post(
            "/api/v1/founder-tools/companies/",
            {"name": name, "domain": domain, "registered": True},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        return VibeRaisingCompany.objects.select_related("organization").get(pk=response.data["id"])

    def _url(self, company_id):
        return f"/api/v1/founder-tools/companies/{company_id}/monthly-updates/"

    def test_enable_then_disable_updates_the_binding_flag(self):
        company = self._create_company()

        on = self.client.post(self._url(company.id), {"enabled": True}, format="json")
        self.assertEqual(on.status_code, 200)
        self.assertTrue(on.data["monthlyUpdatesEnabled"])
        self.assertTrue(
            UserStartupBinding.objects.get(
                user=self.user, organization=company.organization
            ).monthly_updates_enabled
        )

        off = self.client.post(self._url(company.id), {"enabled": False}, format="json")
        self.assertEqual(off.status_code, 200)
        self.assertFalse(off.data["monthlyUpdatesEnabled"])
        self.assertFalse(
            UserStartupBinding.objects.get(
                user=self.user, organization=company.organization
            ).monthly_updates_enabled
        )

    def test_flag_surfaces_on_the_company_serializer(self):
        company = self._create_company()
        self.client.post(self._url(company.id), {"enabled": True}, format="json")

        listing = self.client.get("/api/v1/founder-tools/companies/")
        self.assertEqual(listing.status_code, 200)
        row = next(item for item in listing.data if item["id"] == str(company.id))
        self.assertTrue(row["monthlyUpdatesEnabled"])

    def test_cannot_toggle_another_founders_company(self):
        stranger = User.objects.create_user(
            email="stranger-toggle@example.com", password="pw", first_name="S", last_name="T", role="participant"
        )
        stranger_client = APIClient()
        stranger_client.force_authenticate(user=stranger)
        theirs = stranger_client.post(
            "/api/v1/founder-tools/companies/",
            {"name": "Theirs", "domain": "theirs.example", "registered": True},
            format="json",
        )
        theirs_id = theirs.data["id"]

        response = self.client.post(self._url(theirs_id), {"enabled": True}, format="json")
        self.assertEqual(response.status_code, 404)
