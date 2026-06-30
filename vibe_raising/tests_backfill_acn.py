"""Tests for the backfill_company_acn management command (B6)."""

from io import StringIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase

from founder_tools.models import VibeRaisingCompany, VibeRaisingProfile

User = get_user_model()

COMPANY_ABN = "89000000019"
COMPANY_ACN = "000000019"
NON_COMPANY_ABN = "94807394137"

_ABR_PATH = "content_factory.vibe_marketing_views.verify_company_with_abr"


def _abr(abn):
    # Company ABN verifies; anything else is reported as not-a-company.
    if abn == COMPANY_ABN:
        return {
            "configured": True,
            "reachable": True,
            "found": True,
            "is_company": True,
            "acn": COMPANY_ACN,
            "entity_type_code": "PRV",
        }
    return {
        "configured": True,
        "reachable": True,
        "found": True,
        "is_company": False,
        "acn": None,
        "entity_type_code": "OIE",
    }


class BackfillCompanyAcnTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="founder@example.com", password="pw", role="participant"
        )
        self.profile = VibeRaisingProfile.objects.create(
            user=self.user, role=VibeRaisingProfile.ROLE_FOUNDER
        )

    def _company(self, *, name, abn):
        return VibeRaisingCompany.objects.create(
            profile=self.profile, name=name, domain=f"{name}.com", registered=True, abn=abn
        )

    def _run(self, *args):
        out = StringIO()
        with patch(_ABR_PATH, side_effect=_abr):
            call_command("backfill_company_acn", *args, "--sleep", "0", stdout=out)
        return out.getvalue()

    def test_dry_run_reports_without_persisting(self):
        company = self._company(name="acme", abn=COMPANY_ABN)
        output = self._run()
        self.assertIn("Verified: 1/1", output)
        self.assertIn("Dry run", output)
        company.refresh_from_db()
        self.assertIsNone(company.acn)
        self.assertIsNone(company.abr_verified_at)

    def test_commit_persists_verified_acn(self):
        company = self._company(name="acme", abn=COMPANY_ABN)
        output = self._run("--commit")
        self.assertIn("Verified: 1/1", output)
        company.refresh_from_db()
        self.assertEqual(company.acn, COMPANY_ACN)
        self.assertIsNotNone(company.abr_verified_at)

    def test_non_company_is_flagged_and_left_registered(self):
        company = self._company(name="janetrader", abn=NON_COMPANY_ABN)
        output = self._run("--commit")
        self.assertIn("NOT_A_REGISTERED_COMPANY", output)
        self.assertIn("Needs review", output)
        company.refresh_from_db()
        # Flagged for review, but never silently de-registered.
        self.assertTrue(company.registered)
        self.assertIsNone(company.acn)

    def test_only_targets_registered_companies_missing_acn(self):
        already = self._company(name="already", abn=COMPANY_ABN)
        already.acn = COMPANY_ACN
        already.save(update_fields=["acn"])
        VibeRaisingCompany.objects.create(
            profile=self.profile, name="draft", domain="draft.com", registered=False, abn=COMPANY_ABN
        )
        self._company(name="target", abn=COMPANY_ABN)

        output = self._run("--commit")
        # Only the registered-but-unverified "target" is processed.
        self.assertIn("Verified: 1/1", output)
