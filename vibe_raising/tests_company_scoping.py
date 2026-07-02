"""Win 4: Vibe Raising founder endpoints honour an explicit per-request company_id.

These endpoints previously resolved the tenant only from the profile's mutable
active_company, so two tabs on different startups could read or write each
other's monthly updates. company_id now scopes the request (ownership
enforced), with the active company as the flag-less fallback.
"""

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from founder_tools.models import VibeRaisingCompany, VibeRaisingProfile

User = get_user_model()

UPDATES_URL = "/api/v1/vibe-raising/updates/"


class VibeRaisingCompanyScopingTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            email="vr-scope@example.com",
            password="password",
            first_name="Vr",
            last_name="Scope",
            role="participant",
        )
        self.client.force_authenticate(user=self.user)
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

    def _post_update(self, company_id=None):
        body = {
            "month": "June",
            "year": 2026,
            "summary": "Scoped update",
            "highlights": "Shipped things",
            "challenges": "",
            "asks": "",
            "learnings": "",
            "next30Days": "",
            "metrics": {},
        }
        if company_id is not None:
            body["companyId"] = str(company_id)
        return self.client.post(UPDATES_URL, body, format="json")

    def _updates(self, company_id=None):
        params = {"company_id": str(company_id)} if company_id is not None else {}
        response = self.client.get(UPDATES_URL, params)
        self.assertEqual(response.status_code, 200)
        return response.data["updates"]

    def test_write_and_read_scope_to_requested_company(self):
        # Active company is A, but the request pins B: the update must land on
        # B and never appear under A.
        response = self._post_update(company_id=self.company_b.id)
        self.assertIn(response.status_code, (200, 201))

        self.assertEqual(self._updates(), [])
        self.assertEqual(len(self._updates(company_id=self.company_b.id)), 1)

        # The shared active company was not thrashed by the scoped write.
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.active_company_id, self.company_a.id)

    def test_flagless_requests_still_use_the_active_company(self):
        response = self._post_update()
        self.assertIn(response.status_code, (200, 201))

        self.assertEqual(len(self._updates()), 1)
        self.assertEqual(self._updates(company_id=self.company_b.id), [])

    def test_company_id_of_another_users_company_is_rejected(self):
        stranger = User.objects.create_user(
            email="vr-stranger@example.com",
            password="password",
            first_name="Str",
            last_name="Anger",
            role="participant",
        )
        stranger_profile = VibeRaisingProfile.objects.create(
            user=stranger, role=VibeRaisingProfile.ROLE_FOUNDER
        )
        stranger_company = VibeRaisingCompany.objects.create(
            profile=stranger_profile, name="Gamma", domain="gamma.example"
        )

        response = self.client.get(UPDATES_URL, {"company_id": str(stranger_company.id)})
        self.assertEqual(response.status_code, 404)

        write = self._post_update(company_id=stranger_company.id)
        self.assertEqual(write.status_code, 404)

    def test_malformed_company_id_is_rejected(self):
        response = self.client.get(UPDATES_URL, {"company_id": "not-a-uuid"})
        self.assertEqual(response.status_code, 404)
