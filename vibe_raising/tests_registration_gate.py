"""Endpoint tests for the company-registration gate (B4) and update guard (B5)."""

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


def _abr_company(**overrides):
    base = {
        "configured": True,
        "reachable": True,
        "found": True,
        "is_company": True,
        "acn": COMPANY_ACN,
        "entity_type_code": "PRV",
        "entity_type_name": "Australian Private Company",
    }
    base.update(overrides)
    return lambda abn: base


class CompanyRegistrationGateTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            email="founder@example.com",
            password="password",
            first_name="Founder",
            last_name="User",
            role="participant",
        )
        self.client.force_authenticate(user=self.user)
        VibeRaisingProfile.objects.create(user=self.user, role=VibeRaisingProfile.ROLE_FOUNDER)

    def _post_company(self, payload):
        return self.client.post("/api/v1/vibe-raising/companies/", payload, format="json")

    def test_registered_company_verifies_and_persists_acn(self):
        with patch(_ABR_PATH, side_effect=_abr_company()):
            response = self._post_company(
                {"name": "Acme Pty Ltd", "domain": "acme.com", "abn": COMPANY_ABN, "registered": True}
            )
        self.assertEqual(response.status_code, 200)
        company = VibeRaisingCompany.objects.get(name="Acme Pty Ltd")
        self.assertTrue(company.registered)
        self.assertEqual(company.acn, COMPANY_ACN)
        self.assertEqual(company.abn, COMPANY_ABN)
        self.assertEqual(company.entity_type_code, "PRV")
        self.assertIsNotNone(company.abr_verified_at)
        # B7: the verified ACN + entity type are surfaced in the response.
        self.assertEqual(response.data["acn"], COMPANY_ACN)
        self.assertEqual(response.data["entityTypeName"], "Australian Private Company")
        self.assertIsNotNone(response.data["abrVerifiedAt"])

    def test_non_company_abn_is_blocked(self):
        with patch(_ABR_PATH, side_effect=_abr_company(is_company=False, acn=None)):
            response = self._post_company(
                {"name": "Jane Sole Trader", "domain": "jane.com", "abn": NON_COMPANY_ABN, "registered": True}
            )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.data["code"], "NOT_A_REGISTERED_COMPANY")
        # A new company that fails verification is rolled back — nothing half-registered
        # is left behind.
        self.assertFalse(VibeRaisingCompany.objects.filter(name="Jane Sole Trader").exists())

    def test_invalid_abn_checksum_is_blocked_without_calling_abr(self):
        with patch(_ABR_PATH, side_effect=AssertionError("ABR must not be called")):
            response = self._post_company(
                {"name": "Typo Co", "domain": "typo.com", "abn": "94807394138", "registered": True}
            )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.data["code"], "ABN_INVALID")

    def test_abr_unreachable_fails_closed(self):
        with patch(_ABR_PATH, side_effect=_abr_company(reachable=False)):
            response = self._post_company(
                {"name": "Acme Pty Ltd", "domain": "acme.com", "abn": COMPANY_ABN, "registered": True}
            )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.data["code"], "ABR_UNVERIFIABLE")

    def test_scratch_save_without_registered_stays_unverified(self):
        # Saving company details without requesting registration must not hit the ABR
        # and must leave the company unverified.
        with patch(_ABR_PATH, side_effect=AssertionError("ABR must not be called")):
            response = self._post_company(
                {"name": "Draft Co", "domain": "draft.com", "abn": COMPANY_ABN}
            )
        self.assertEqual(response.status_code, 200)
        company = VibeRaisingCompany.objects.get(name="Draft Co")
        self.assertFalse(company.registered)
        self.assertIsNone(company.acn)
        self.assertIsNone(company.abr_verified_at)


class UpdateGuardTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            email="founder@example.com",
            password="password",
            first_name="Founder",
            last_name="User",
            role="participant",
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
            registered=verified,
            abn=COMPANY_ABN if verified else None,
            acn=COMPANY_ACN if verified else None,
            abr_verified_at=timezone.now() if verified else None,
        )
        self.profile.active_company = company
        self.profile.save(update_fields=["active_company", "updated_at"])
        return company

    def test_unverified_company_cannot_create_update(self):
        self._make_company(verified=False)
        response = self.client.post("/api/v1/vibe-raising/updates/", {}, format="json")
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.data["code"], "ACN_REQUIRED")

    def test_unverified_company_cannot_run_update(self):
        self._make_company(verified=False)
        response = self.client.post("/api/v1/vibe-raising/startup-update/run/", {}, format="json")
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.data["code"], "ACN_REQUIRED")

    def test_verified_company_passes_the_gate(self):
        # A verified company must get past the registration guard. The request may still
        # fail later for unrelated reasons (e.g. missing payload) — it just must not be
        # the ACN block.
        self._make_company(verified=True)
        response = self.client.post("/api/v1/vibe-raising/updates/", {}, format="json")
        gated = response.status_code == 422 and getattr(response, "data", {}).get("code") == "ACN_REQUIRED"
        self.assertFalse(gated, "verified company should not be blocked by the ACN gate")

    def test_verified_nonprofit_passes_the_gate_without_acn(self):
        # A registered not-for-profit (flagged, ABR-verified, no ACN) is exempt from the
        # ACN requirement and must not be blocked.
        company = VibeRaisingCompany.objects.create(
            profile=self.profile,
            name="MLAI Aus Inc",
            domain="mlai.au",
            registered=True,
            abn=COMPANY_ABN,
            acn=None,
            is_nonprofit=True,
            abr_verified_at=timezone.now(),
        )
        self.profile.active_company = company
        self.profile.save(update_fields=["active_company", "updated_at"])
        response = self.client.post("/api/v1/vibe-raising/updates/", {}, format="json")
        gated = response.status_code == 422 and getattr(response, "data", {}).get("code") == "ACN_REQUIRED"
        self.assertFalse(gated, "verified not-for-profit should not be blocked by the ACN gate")
