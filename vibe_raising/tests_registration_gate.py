"""Endpoint tests for non-blocking company verification.

Verification is best-effort: a valid ABN/ACN stamps the company as verified (which
unlocks perks like the coworking discount), but an invalid/missing one must NOT block
the founder from saving their company or creating updates.
"""

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from founder_tools.models import VibeRaisingCompany, VibeRaisingProfile

User = get_user_model()

COMPANY_ABN = "89000000019"
COMPANY_ACN = "000000019"
NON_COMPANY_ABN = "94807394137"

_ABR_PATH = "content_factory.vibe_marketing_views.verify_company_with_abr"


def _abr(**overrides):
    base = {
        "configured": True,
        "reachable": True,
        "found": True,
        "is_company": True,
        "acn": COMPANY_ACN,
        "entity_type_code": "PRV",
    }
    base.update(overrides)
    return lambda abn: base


class NonBlockingCompanySaveTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            email="founder@example.com", password="password", role="participant",
        )
        self.client.force_authenticate(user=self.user)
        VibeRaisingProfile.objects.create(user=self.user, role=VibeRaisingProfile.ROLE_FOUNDER)

    def _post(self, payload):
        return self.client.post("/api/v1/vibe-raising/companies/", payload, format="json")

    def test_valid_company_is_saved_and_verified(self):
        with patch(_ABR_PATH, side_effect=_abr()):
            response = self._post(
                {"name": "Acme Pty Ltd", "domain": "acme.com", "abn": COMPANY_ABN, "registered": True}
            )
        self.assertEqual(response.status_code, 200)
        company = VibeRaisingCompany.objects.get(name="Acme Pty Ltd")
        self.assertTrue(company.registered)
        self.assertEqual(company.acn, COMPANY_ACN)
        self.assertIsNotNone(company.abr_verified_at)

    def test_non_company_abn_is_saved_but_unverified(self):
        # A non-company ABN no longer blocks — the company saves, just unverified.
        with patch(_ABR_PATH, side_effect=_abr(is_company=False, acn=None)):
            response = self._post(
                {"name": "Jane Sole Trader", "domain": "jane.com", "abn": NON_COMPANY_ABN, "registered": True}
            )
        self.assertEqual(response.status_code, 200)
        company = VibeRaisingCompany.objects.get(name="Jane Sole Trader")
        self.assertTrue(company.registered)
        self.assertIsNone(company.acn)
        self.assertIsNone(company.abr_verified_at)

    def test_invalid_abn_is_saved_but_unverified(self):
        # Invalid checksum short-circuits before the ABR is consulted; still no block.
        with patch(_ABR_PATH, side_effect=AssertionError("ABR must not be called")):
            response = self._post(
                {"name": "Typo Co", "domain": "typo.com", "abn": "94807394138", "registered": True}
            )
        self.assertEqual(response.status_code, 200)
        company = VibeRaisingCompany.objects.get(name="Typo Co")
        self.assertTrue(company.registered)
        self.assertIsNone(company.acn)

    def test_missing_abn_is_saved_but_unverified(self):
        with patch(_ABR_PATH, side_effect=AssertionError("ABR must not be called")):
            response = self._post({"name": "No Abn Co", "domain": "noabn.com", "registered": True})
        self.assertEqual(response.status_code, 200)
        company = VibeRaisingCompany.objects.get(name="No Abn Co")
        self.assertIsNone(company.acn)
        self.assertIsNone(company.abr_verified_at)


class UpdateNotBlockedTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            email="founder@example.com", password="password", role="participant",
        )
        self.client.force_authenticate(user=self.user)
        self.profile = VibeRaisingProfile.objects.create(
            user=self.user, role=VibeRaisingProfile.ROLE_FOUNDER
        )

    def _make_company(self, *, verified):
        company = VibeRaisingCompany.objects.create(
            profile=self.profile,
            name="Acme Pty Ltd",
            domain="acme.com",
            registered=True,
            abn=COMPANY_ABN,
            acn=COMPANY_ACN if verified else None,
            abr_verified_at=timezone.now() if verified else None,
        )
        self.profile.active_company = company
        self.profile.save(update_fields=["active_company", "updated_at"])
        return company

    def _not_acn_blocked(self, response):
        # The old gate returned 422 ACN_REQUIRED. That must never happen now.
        blocked = response.status_code == 422 and getattr(response, "data", {}).get("code") == "ACN_REQUIRED"
        self.assertFalse(blocked, "verification must not block update creation")

    def test_unverified_company_can_create_update(self):
        self._make_company(verified=False)
        self._not_acn_blocked(self.client.post("/api/v1/vibe-raising/updates/", {}, format="json"))

    def test_unverified_company_can_run_update(self):
        self._make_company(verified=False)
        self._not_acn_blocked(
            self.client.post("/api/v1/vibe-raising/startup-update/run/", {}, format="json")
        )
