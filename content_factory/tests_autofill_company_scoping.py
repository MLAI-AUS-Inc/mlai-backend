"""Autofill must never silently convert one company into another.

Historically, starting AI research ("startup_autofill") resolved the profile's
active company and rewrote its name/domain in place — so a founder onboarding
their second startup through the wizard destroyed their first one. These tests
pin the new contract: an implicit domain change 409s until confirmed, and
create_new registers a sibling instead of mutating the active company.
"""

from contextlib import ExitStack
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.response import Response
from rest_framework.test import APIClient

from founder_tools.models import VibeRaisingCompany, VibeRaisingProfile

User = get_user_model()

AUTOFILL_URL = "/api/v1/vibe-marketing/autofill"
_VIEWS = "content_factory.vibe_marketing_views"


class AutofillCompanyScopingTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            email="autofill-scope@example.com",
            password="password",
            first_name="Auto",
            last_name="Fill",
            role="participant",
        )
        self.client.force_authenticate(user=self.user)

    def _create_company(self, name="Acme Inc.", domain="acme.com"):
        response = self.client.post(
            "/api/v1/founder-tools/companies/",
            {"name": name, "domain": domain, "registered": True},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        return VibeRaisingCompany.objects.get(pk=response.data["id"])

    def _post_autofill(self, body):
        # Everything past company resolution (points gate, run queueing) is
        # out of scope here — stub it so the tests exercise only the scoping.
        with ExitStack() as stack:
            stack.enter_context(
                patch(f"{_VIEWS}._require_roo_points_for_ai_agent", return_value=(None, 100))
            )
            stack.enter_context(
                patch(f"{_VIEWS}._active_startup_autofill_run_for_domain", return_value=None)
            )
            stack.enter_context(
                patch(f"{_VIEWS}._queue_content_factory_run", return_value=MagicMock())
            )
            stack.enter_context(
                patch(
                    f"{_VIEWS}._autofill_start_response",
                    return_value=Response({"runId": "run-1", "status": "queued"}, status=200),
                )
            )
            return self.client.post(AUTOFILL_URL, body, format="json")

    def test_implicit_domain_change_is_blocked_without_confirmation(self):
        company = self._create_company()

        response = self._post_autofill({"company_name": "Beta Corp", "domain": "beta.com"})

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["code"], "company_domain_change_requires_confirmation")
        self.assertEqual(response.data["companyId"], str(company.id))
        self.assertEqual(response.data["existingDomain"], "acme.com")
        self.assertEqual(response.data["submittedDomain"], "beta.com")

        company.refresh_from_db()
        self.assertEqual(company.name, "Acme Inc.")
        self.assertEqual(company.domain, "acme.com")
        self.assertEqual(VibeRaisingCompany.objects.count(), 1)

    def test_create_new_registers_a_second_company(self):
        original = self._create_company()

        response = self._post_autofill(
            {"company_name": "Beta Corp", "domain": "beta.com", "create_new": True}
        )

        self.assertEqual(response.status_code, 200)
        profile = VibeRaisingProfile.objects.get(user=self.user)
        self.assertEqual(profile.companies.count(), 2)

        original.refresh_from_db()
        self.assertEqual(original.name, "Acme Inc.")
        self.assertEqual(original.domain, "acme.com")

        created = profile.companies.exclude(pk=original.pk).get()
        self.assertEqual(created.name, "Beta Corp")
        self.assertEqual(created.domain, "beta.com")
        # Autofill pins the company it operated on as active.
        profile.refresh_from_db()
        self.assertEqual(profile.active_company_id, created.id)

    def test_confirmed_domain_change_updates_in_place(self):
        company = self._create_company()

        response = self._post_autofill(
            {
                "company_name": "Acme Rebrand",
                "domain": "acme-rebrand.com",
                "confirm_domain_change": True,
            }
        )

        self.assertEqual(response.status_code, 200)
        company.refresh_from_db()
        self.assertEqual(company.name, "Acme Rebrand")
        self.assertEqual(company.domain, "acme-rebrand.com")
        self.assertEqual(VibeRaisingCompany.objects.count(), 1)

    def test_confirmed_domain_change_renames_the_org_so_data_follows(self):
        company = self._create_company()
        original_org_id = company.organization_id

        response = self._post_autofill(
            {
                "company_name": "Acme Rebrand",
                "domain": "acme-rebrand.com",
                "confirm_domain_change": True,
            }
        )

        self.assertEqual(response.status_code, 200)
        company.refresh_from_db()
        # Same tenant, renamed in place: connections/runs/updates keep working.
        self.assertEqual(company.organization_id, original_org_id)
        self.assertEqual(company.organization.domain, "acme-rebrand.com")

    def test_autofill_never_binds_to_another_founders_domain(self):
        from organizations.models import Organization

        stranger = User.objects.create_user(
            email="autofill-stranger@example.com",
            password="password",
            first_name="Str",
            last_name="Anger",
            role="participant",
        )
        stranger_profile = VibeRaisingProfile.objects.create(
            user=stranger, role=VibeRaisingProfile.ROLE_FOUNDER
        )
        VibeRaisingCompany.objects.create(
            profile=stranger_profile,
            name="Foreign Co",
            domain="foreign.example",
            organization=Organization.objects.create(name="Foreign", domain="foreign.example"),
        )

        response = self._post_autofill(
            {"company_name": "Impostor", "domain": "foreign.example", "create_new": True}
        )

        self.assertEqual(response.status_code, 409)
        self.assertIn("already linked", str(response.data["detail"]))

    def test_same_domain_update_needs_no_confirmation(self):
        company = self._create_company()

        response = self._post_autofill({"company_name": "Acme Updated", "domain": "acme.com"})

        self.assertEqual(response.status_code, 200)
        company.refresh_from_db()
        self.assertEqual(company.name, "Acme Updated")

    def test_explicit_company_id_with_new_domain_also_requires_confirmation(self):
        company = self._create_company()

        response = self._post_autofill(
            {"companyId": str(company.id), "company_name": "Acme", "domain": "beta.com"}
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["code"], "company_domain_change_requires_confirmation")

    def test_create_new_still_blocks_a_sibling_domain(self):
        self._create_company()

        response = self._post_autofill(
            {"company_name": "Copycat", "domain": "acme.com", "create_new": True}
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["code"], "duplicate_company_domain")
        self.assertEqual(VibeRaisingCompany.objects.count(), 1)

    def test_first_company_is_still_created_for_a_fresh_profile(self):
        response = self._post_autofill({"company_name": "First Co", "domain": "first.co"})

        self.assertEqual(response.status_code, 200)
        profile = VibeRaisingProfile.objects.get(user=self.user)
        company = profile.companies.get()
        self.assertEqual(company.name, "First Co")
        self.assertEqual(company.domain, "first.co")
        profile.refresh_from_db()
        self.assertEqual(profile.active_company_id, company.id)


class SettingsCompanyScopingTests(TestCase):
    """PUT /settings and the avatar upload honour an explicit companyId."""

    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            email="settings-scope@example.com",
            password="password",
            first_name="Settings",
            last_name="Scope",
            role="participant",
        )
        self.client.force_authenticate(user=self.user)
        self.company_a = self._create_company("Acme Inc.", "acme.com")
        self.company_b = self._create_company("Beta Corp", "beta.com")
        # First-created company stays active; B is the non-active sibling.
        profile = VibeRaisingProfile.objects.get(user=self.user)
        self.assertEqual(profile.active_company_id, self.company_a.id)

    def _create_company(self, name, domain):
        response = self.client.post(
            "/api/v1/founder-tools/companies/",
            {"name": name, "domain": domain, "registered": True},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        return VibeRaisingCompany.objects.get(pk=response.data["id"])

    def test_settings_save_targets_the_requested_company(self):
        with patch(f"{_VIEWS}._serialize_bootstrap", return_value={}):
            response = self.client.put(
                "/api/v1/vibe-marketing/settings",
                {
                    "companyId": str(self.company_b.id),
                    "companyName": "Beta Renamed",
                    "domain": "beta.com",
                },
                format="json",
            )

        self.assertEqual(response.status_code, 200)
        self.company_b.refresh_from_db()
        self.company_a.refresh_from_db()
        self.assertEqual(self.company_b.name, "Beta Renamed")
        self.assertEqual(self.company_a.name, "Acme Inc.")

    def test_settings_domain_edit_renames_the_org_in_place(self):
        original_org_id = self.company_b.organization_id
        with patch(f"{_VIEWS}._serialize_bootstrap", return_value={}):
            response = self.client.put(
                "/api/v1/vibe-marketing/settings",
                {
                    "companyId": str(self.company_b.id),
                    "companyName": "Beta Corp",
                    "domain": "beta-rebrand.com",
                },
                format="json",
            )

        self.assertEqual(response.status_code, 200)
        self.company_b.refresh_from_db()
        self.assertEqual(self.company_b.organization_id, original_org_id)
        self.assertEqual(self.company_b.organization.domain, "beta-rebrand.com")

    def test_settings_unsafe_domain_edit_requires_confirmation(self):
        from integrations.models import GoogleConnection
        from organizations.models import Organization

        GoogleConnection.objects.create(
            user=self.user,
            organization=self.company_b.organization,
            google_email="beta@example.com",
            refresh_token="token",
            scope="",
        )
        Organization.objects.create(name="Taken", domain="taken-settings.example")

        with patch(f"{_VIEWS}._serialize_bootstrap", return_value={}):
            blocked = self.client.put(
                "/api/v1/vibe-marketing/settings",
                {
                    "companyId": str(self.company_b.id),
                    "companyName": "Beta Corp",
                    "domain": "taken-settings.example",
                },
                format="json",
            )
            confirmed = self.client.put(
                "/api/v1/vibe-marketing/settings",
                {
                    "companyId": str(self.company_b.id),
                    "companyName": "Beta Corp",
                    "domain": "taken-settings.example",
                    "confirmDomainChange": True,
                },
                format="json",
            )

        self.assertEqual(blocked.status_code, 409)
        self.assertEqual(blocked.data["code"], "company_domain_change_moves_data")
        self.assertEqual(confirmed.status_code, 200)
        self.company_b.refresh_from_db()
        self.assertEqual(self.company_b.organization.domain, "taken-settings.example")

    def test_settings_save_rejects_a_company_the_user_does_not_own(self):
        stranger = User.objects.create_user(
            email="stranger@example.com",
            password="password",
            first_name="Str",
            last_name="Anger",
            role="participant",
        )
        stranger_client = APIClient()
        stranger_client.force_authenticate(user=stranger)

        response = stranger_client.put(
            "/api/v1/vibe-marketing/settings",
            {"companyId": str(self.company_b.id), "companyName": "Hijack", "domain": "beta.com"},
            format="json",
        )

        self.assertEqual(response.status_code, 404)
        self.company_b.refresh_from_db()
        self.assertEqual(self.company_b.name, "Beta Corp")
