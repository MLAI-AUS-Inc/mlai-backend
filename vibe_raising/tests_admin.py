from datetime import date

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from founder_tools.models import VibeRaisingCompany, VibeRaisingProfile
from organizations.models import Organization
from startup_updates.models import MonthlyUpdateDraft, MonthlyUpdateDraftStatus

User = get_user_model()

OVERVIEW_URL = "/api/v1/vibe-raising/admin/overview/"
UPDATES_URL = "/api/v1/vibe-raising/admin/updates/"


class VibeRaisingAdminEndpointsTests(TestCase):
    def setUp(self):
        self.client = APIClient()

        self.admin = User.objects.create_user(email="admin@example.com")
        self.admin.is_superuser = True
        self.admin.save(update_fields=["is_superuser"])

        self.founder = User.objects.create_user(
            email="founder@example.com", first_name="Fiona", last_name="Founder"
        )
        self.profile = VibeRaisingProfile.objects.create(
            user=self.founder,
            role=VibeRaisingProfile.ROLE_FOUNDER,
            organization_name="Acme",
        )
        self.org = Organization.objects.create(name="Acme", domain="acme.example")
        self.company = VibeRaisingCompany.objects.create(
            profile=self.profile,
            organization=self.org,
            name="Acme Inc",
            registered=True,
            avatar_url="https://img.example/acme.png",
        )
        self.draft = MonthlyUpdateDraft.objects.create(
            organization=self.org,
            month=date(2026, 6, 1),
            status=MonthlyUpdateDraftStatus.NEEDS_REVIEW,
            structured_memo={"summary": "We shipped a lot."},
        )

    # --- gating -----------------------------------------------------------
    def test_overview_rejects_non_admin(self):
        self.client.force_authenticate(self.founder)
        self.assertEqual(self.client.get(OVERVIEW_URL).status_code, 403)

    def test_overview_rejects_anonymous(self):
        self.assertIn(self.client.get(OVERVIEW_URL).status_code, (401, 403))

    def test_updates_rejects_non_admin(self):
        self.client.force_authenticate(self.founder)
        self.assertEqual(self.client.get(UPDATES_URL).status_code, 403)

    # --- overview ---------------------------------------------------------
    def test_overview_returns_contract_shape(self):
        self.client.force_authenticate(self.admin)
        response = self.client.get(OVERVIEW_URL)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        for key in (
            "stats",
            "updatesOverTime",
            "updatesByStage",
            "updatesByIndustry",
            "recentUpdates",
            "reviewCount",
        ):
            self.assertIn(key, body)
        self.assertEqual(body["reviewCount"], 1)
        self.assertIn("updatesCreated", {stat["key"] for stat in body["stats"]})

        summary = next(u for u in body["recentUpdates"] if u["id"] == str(self.draft.id))
        # IDs must be strings or the frontend normalizer drops the whole summary.
        self.assertIsInstance(summary["id"], str)
        self.assertEqual(summary["startupName"], "Acme Inc")
        self.assertEqual(summary["founderName"], "Fiona Founder")
        self.assertEqual(summary["companyId"], str(self.company.id))
        self.assertEqual(summary["status"], MonthlyUpdateDraftStatus.NEEDS_REVIEW)
        self.assertEqual(summary["updateMonth"], "June 2026")

    # --- list + filters ---------------------------------------------------
    def test_updates_list_filters_and_search(self):
        self.client.force_authenticate(self.admin)

        body = self.client.get(UPDATES_URL).json()
        self.assertEqual(body["total"], 1)
        self.assertEqual(len(body["updates"]), 1)
        self.assertEqual(body["updates"][0]["startupName"], "Acme Inc")
        self.assertIsInstance(body["updates"][0]["id"], str)
        self.assertFalse(body["hasNext"])
        self.assertFalse(body["hasPrevious"])

        # "review" alias maps onto needs_review and matches our draft.
        self.assertEqual(self.client.get(UPDATES_URL + "?status=review").json()["total"], 1)
        # "ready" does not match a needs_review draft.
        self.assertEqual(self.client.get(UPDATES_URL + "?status=ready").json()["total"], 0)
        # search by founder name and by company name.
        self.assertEqual(self.client.get(UPDATES_URL + "?q=Fiona").json()["total"], 1)
        self.assertEqual(self.client.get(UPDATES_URL + "?q=Acme").json()["total"], 1)
        self.assertEqual(self.client.get(UPDATES_URL + "?q=nomatch").json()["total"], 0)

    # --- detail -----------------------------------------------------------
    def test_update_detail_returns_summary_update_and_founder(self):
        self.client.force_authenticate(self.admin)
        response = self.client.get(f"{UPDATES_URL}{self.draft.id}/")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["summary"]["startupName"], "Acme Inc")
        self.assertEqual(body["company"]["name"], "Acme Inc")
        self.assertEqual(body["founder"]["email"], "founder@example.com")
        self.assertIsNotNone(body["update"])
        self.assertEqual(body["update"]["status"], MonthlyUpdateDraftStatus.NEEDS_REVIEW)

    def test_update_detail_missing_returns_404(self):
        self.client.force_authenticate(self.admin)
        self.assertEqual(self.client.get(f"{UPDATES_URL}999999/").status_code, 404)
