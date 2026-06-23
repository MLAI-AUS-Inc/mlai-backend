"""A deleted/reset run polled by a stale wizard session must return an explicit, stable
"gone" 404 (code=run_not_found) instead of a bare Http404. The mlai.au run-status loader
branches on this instead of turning a 404 into a 500 (the recurring reset -> 500).
"""
from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from content_factory.models import OrganizationContentConfig
from founder_tools.models import VibeRaisingCompany, VibeRaisingProfile
from organizations.models import Organization

User = get_user_model()

MISSING_RUN_ID = "6714669e-c887-4fba-89d7-22618bfd6174"  # skedy's deleted scaffold run


class RunStatusMissingRunTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            email="founder-run404@example.com", password="password", role="participant"
        )
        self.profile = VibeRaisingProfile.objects.create(user=self.user, role=VibeRaisingProfile.ROLE_FOUNDER)
        self.organization = Organization.objects.create(name="MLAI", domain="mlai.au")
        self.company = VibeRaisingCompany.objects.create(
            profile=self.profile,
            organization=self.organization,
            name="MLAI",
            domain="mlai.au",
            registered=True,
        )
        self.profile.active_company = self.company
        self.profile.save(update_fields=["active_company", "updated_at"])
        OrganizationContentConfig.objects.create(organization=self.organization, github_repo="MLAI-AUS-Inc/mlai-au")
        self.client.force_authenticate(user=self.user)

    def test_missing_run_returns_structured_gone_404(self):
        resp = self.client.get(f"/api/v1/vibe-marketing/runs/{MISSING_RUN_ID}?view=status")
        self.assertEqual(resp.status_code, 404)
        body = resp.json()
        self.assertEqual(body.get("code"), "run_not_found")
        self.assertTrue(body.get("gone"))
        self.assertEqual(body.get("runId"), MISSING_RUN_ID)
