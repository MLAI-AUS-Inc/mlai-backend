"""Authorization tests for the startup data-deletion endpoint (finding #3).

DELETE /api/v1/startups/<organization_id>/data is a service-key-only endpoint
(authentication_classes=[] + HasRooApiKey) that wipes an org's entire data plane.
Before the fix it trusted `requested_by_user_id` (even None) from the body, so any
holder of the (widely shared) Roo API key could delete any org's data by id. The
fix requires the named user to actually be bound to the org.
"""
from datetime import date

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from organizations.models import Organization
from startup_updates.models import (
    MonthlyUpdateDraft,
    MonthlyUpdateDraftStatus,
    UserStartupBinding,
)

User = get_user_model()


@override_settings(ROO_API_KEY="roo-test-key", INTERNAL_API_KEY="", MLAI_API_KEY="")
class StartupDataDeletionAuthzTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.client.credentials(HTTP_X_API_KEY="roo-test-key")
        self.owner = User.objects.create_user(
            email="owner@acme.com", password="pw",
            first_name="O", last_name="Wner", role="participant",
        )
        self.other = User.objects.create_user(
            email="other@evil.com", password="pw",
            first_name="E", last_name="Vil", role="participant",
        )
        self.org = Organization.objects.create(name="Acme", domain="acme.com")
        UserStartupBinding.objects.create(user=self.owner, organization=self.org, role="founder")
        self.draft = MonthlyUpdateDraft.objects.create(
            organization=self.org,
            month=date(2026, 5, 1),
            status=MonthlyUpdateDraftStatus.READY,
            structured_memo={"highlights": ["Confidential"]},
        )

    @property
    def url(self):
        return f"/api/v1/startups/{self.org.id}/data"

    def _draft_survives(self):
        return MonthlyUpdateDraft.objects.filter(pk=self.draft.pk).exists()

    def test_delete_by_non_member_is_forbidden_and_keeps_data(self):
        resp = self.client.delete(self.url, {"requested_by_user_id": self.other.id}, format="json")
        self.assertEqual(resp.status_code, 403)
        self.assertTrue(self._draft_survives())

    def test_delete_without_user_is_forbidden_and_keeps_data(self):
        resp = self.client.delete(self.url, {}, format="json")
        self.assertEqual(resp.status_code, 403)
        self.assertTrue(self._draft_survives())

    def test_delete_by_bound_member_succeeds_and_wipes_data(self):
        resp = self.client.delete(self.url, {"requested_by_user_id": self.owner.id}, format="json")
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(self._draft_survives())

    def test_delete_without_api_key_is_rejected_and_keeps_data(self):
        anon = APIClient()  # no X-API-KEY
        resp = anon.delete(self.url, {"requested_by_user_id": self.owner.id}, format="json")
        self.assertIn(resp.status_code, (401, 403))
        self.assertTrue(self._draft_survives())
