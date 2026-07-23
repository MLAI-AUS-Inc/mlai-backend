from __future__ import annotations

from datetime import date

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from content_analytics.models import ArticlePerformanceReport
from content_factory.models import OrganizationContentConfig
from core.models import User
from founder_tools.models import VibeRaisingCompany, VibeRaisingProfile
from organizations.models import Organization

BASE = "/api/v1/vibe-marketing/analytics/reports"


class ArticleReportApiTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(email="report-api@example.com", password="password")
        self.profile = VibeRaisingProfile.objects.create(
            user=self.user,
            role=VibeRaisingProfile.ROLE_FOUNDER,
        )
        self.organization = Organization.objects.create(name="Report Co", domain="reports.example")
        self.company = VibeRaisingCompany.objects.create(
            profile=self.profile,
            organization=self.organization,
            name="Report Co",
            domain="reports.example",
            registered=True,
        )
        self.profile.active_company = self.company
        self.profile.save(update_fields=["active_company", "updated_at"])
        OrganizationContentConfig.objects.create(
            organization=self.organization,
            baseline_skipped_at=timezone.now(),
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

        self.other_org = Organization.objects.create(name="Other", domain="other.example")
        self.foreign_report = self._report(self.other_org, date(2026, 7, 21))

    def _report(self, organization, report_date):
        return ArticlePerformanceReport.objects.create(
            organization=organization,
            report_date=report_date,
            window_start=report_date.replace(day=report_date.day - 7)
            if report_date.day > 7
            else report_date,
            window_end=report_date,
            prior_window_start=report_date,
            prior_window_end=report_date,
            payload={
                "headline": {"humanVisits": 42, "ctaClickers": 3},
                "categoriesSummary": {"top_performer": 1},
                "articles": [{"title": "A"}],
                "notes": ["Known bots are excluded at collection."],
            },
        )

    def test_list_is_scoped_newest_first_and_limited(self):
        for day in (18, 19, 20, 21):
            self._report(self.organization, date(2026, 7, day))

        response = self.client.get(BASE)
        self.assertEqual(response.status_code, 200)
        rows = response.json()["reports"]
        self.assertEqual(len(rows), 4)
        self.assertEqual(
            [row["reportDate"] for row in rows],
            ["2026-07-21", "2026-07-20", "2026-07-19", "2026-07-18"],
        )
        self.assertEqual(rows[0]["headline"]["humanVisits"], 42)
        self.assertEqual(rows[0]["categoriesSummary"], {"top_performer": 1})
        self.assertNotIn("payload", rows[0])
        # Foreign-org reports never appear.
        ids = {row["id"] for row in rows}
        self.assertNotIn(self.foreign_report.pk, ids)

        limited = self.client.get(f"{BASE}?limit=2").json()["reports"]
        self.assertEqual(len(limited), 2)
        clamped = self.client.get(f"{BASE}?limit=banana").json()["reports"]
        self.assertEqual(len(clamped), 4)

    def test_detail_returns_stored_payload_verbatim(self):
        report = self._report(self.organization, date(2026, 7, 21))
        response = self.client.get(f"{BASE}/{report.pk}")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["id"], report.pk)
        self.assertEqual(body["reportDate"], "2026-07-21")
        self.assertEqual(body["payload"], report.payload)

    def test_detail_is_org_scoped(self):
        response = self.client.get(f"{BASE}/{self.foreign_report.pk}")
        self.assertEqual(response.status_code, 404)
        missing = self.client.get(f"{BASE}/999999")
        self.assertEqual(missing.status_code, 404)

    def test_requires_authentication(self):
        anonymous = APIClient()
        response = anonymous.get(BASE)
        self.assertNotEqual(response.status_code, 200)
