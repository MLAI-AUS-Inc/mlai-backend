from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from content_factory.models import ContentFactoryJob
from core.models import User
from founder_tools.models import VibeRaisingCompany, VibeRaisingProfile
from organizations.models import Organization
from roo.models import CoworkingBooking, PointsAdmin, PointsAccount
from startup_updates.models import MonthlyUpdateDraft

from .registry import assert_no_sensitive_fields_registered


class DataAccessApiTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.client.defaults["HTTP_X_API_KEY"] = "roo-test-key"
        self.override = self.settings(ROO_API_KEY="roo-test-key", INTERNAL_API_KEY="")
        self.override.enable()

        self.user = User.objects.create_user(email="founder@example.com", slack_id="UFOUNDER")
        self.other_user = User.objects.create_user(email="other@example.com", slack_id="UOTHER")
        self.admin_user = User.objects.create_user(email="admin@example.com", slack_id="UADMIN", is_staff=True)
        PointsAdmin.objects.create(slack_user_id="UADMIN", user=self.admin_user, role="admin", is_active=True)

        self.org = Organization.objects.create(name="Acme", domain="acme.test")
        self.other_org = Organization.objects.create(name="Other", domain="other.test")
        self.profile = VibeRaisingProfile.objects.create(user=self.user, role=VibeRaisingProfile.ROLE_FOUNDER)
        VibeRaisingCompany.objects.create(profile=self.profile, organization=self.org, name="Acme", domain="acme.test")

    def tearDown(self):
        self.override.disable()

    def post_query(self, payload):
        return self.client.post(reverse("data_access_query"), payload, format="json")

    def test_catalog_exposes_allowlisted_fields_only(self):
        response = self.client.get(reverse("data_access_catalog"))

        self.assertEqual(response.status_code, 200)
        resources = {resource["key"]: resource for resource in response.json()["resources"]}
        self.assertIn("github_integrations", resources)
        self.assertIn("content_org_config", resources)
        for resource in resources.values():
            joined = " ".join(resource["fields"]).lower()
            self.assertNotIn("access_token", joined)
            self.assertNotIn("refresh_token", joined)
            self.assertNotIn("raw_payload", joined)
            self.assertNotIn("storage_path", joined)

    def test_sensitive_field_registry_assertion(self):
        assert_no_sensitive_fields_registered()

    def test_self_scoped_user_can_only_query_own_points_account(self):
        PointsAccount.objects.create(user=self.user, balance=7)
        PointsAccount.objects.create(user=self.other_user, balance=99)

        response = self.post_query(
            {
                "requester_slack_id": "UFOUNDER",
                "resource": "points_accounts",
                "fields": ["user_slack_id", "balance"],
                "limit": 20,
            }
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["rows"], [{"user_slack_id": "UFOUNDER", "balance": 7}])

    def test_admin_access_is_still_resource_policy_defined(self):
        PointsAccount.objects.create(user=self.user, balance=7)
        PointsAccount.objects.create(user=self.other_user, balance=99)

        response = self.post_query(
            {
                "requester_slack_id": "UADMIN",
                "resource": "points_accounts",
                "fields": ["user_slack_id", "balance"],
                "order_by": [{"field": "balance", "direction": "asc"}],
                "limit": 20,
            }
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()["rows"],
            [{"user_slack_id": "UFOUNDER", "balance": 7}, {"user_slack_id": "UOTHER", "balance": 99}],
        )

    def test_founder_org_scope_filters_vibe_raising_data(self):
        MonthlyUpdateDraft.objects.create(organization=self.org, month="2026-06-01", title="Acme Update")
        MonthlyUpdateDraft.objects.create(organization=self.other_org, month="2026-06-01", title="Other Update")

        response = self.post_query(
            {
                "requester_slack_id": "UFOUNDER",
                "resource": "monthly_update_drafts",
                "fields": ["organization_id", "title"],
                "limit": 20,
            }
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["rows"], [{"organization_id": self.org.id, "title": "Acme Update"}])

    def test_unknown_field_and_orm_lookup_are_rejected(self):
        response = self.post_query(
            {
                "requester_slack_id": "UADMIN",
                "resource": "points_accounts",
                "fields": ["user__email"],
            }
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("Unknown field", response.json()["error"])

    def test_icontains_requires_searchable_field(self):
        response = self.post_query(
            {
                "requester_slack_id": "UADMIN",
                "resource": "points_accounts",
                "filters": [{"field": "balance", "operator": "icontains", "value": "7"}],
            }
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("does not allow `icontains`", response.json()["error"])

    def test_icontains_is_case_insensitive_substring_search(self):
        ContentFactoryJob.objects.create(job_id="job-1", slack_user_id="UFOUNDER", domain="Acme.Test", status="queued")
        ContentFactoryJob.objects.create(job_id="job-2", slack_user_id="UOTHER", domain="other.test", status="queued")

        response = self.post_query(
            {
                "requester_slack_id": "UADMIN",
                "resource": "content_factory_jobs",
                "fields": ["job_id", "domain"],
                "filters": [{"field": "domain", "operator": "icontains", "value": "acme"}],
                "limit": 20,
            }
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["rows"], [{"job_id": "job-1", "domain": "Acme.Test"}])

    def test_pagination_uses_returned_count_without_total_count(self):
        for day in range(1, 4):
            CoworkingBooking.objects.create(user=self.user, date=f"2026-06-0{day}", status="booked", points_cost=1)

        response = self.post_query(
            {
                "requester_slack_id": "UADMIN",
                "resource": "coworking_bookings",
                "fields": ["date"],
                "order_by": [{"field": "date", "direction": "asc"}],
                "limit": 2,
                "offset": 0,
            }
        )

        payload = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload["returned_count"], 2)
        self.assertTrue(payload["has_more"])
        self.assertNotIn("total_count", payload)

    def test_limit_above_resource_max_is_rejected(self):
        response = self.post_query(
            {
                "requester_slack_id": "UADMIN",
                "resource": "coworking_bookings",
                "limit": 9999,
            }
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("max_limit", response.json()["error"])
