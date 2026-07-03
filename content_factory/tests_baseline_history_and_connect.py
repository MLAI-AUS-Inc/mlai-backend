from datetime import timedelta
from types import SimpleNamespace
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIRequestFactory, force_authenticate

from content_factory import google_baseline, vibe_marketing_views
from content_factory.models import WebsiteBaselineSnapshot
from content_factory.vibe_marketing_views import (
    VibeMarketingBaselineHistoryView,
    _google_baseline_connect_url,
)
from integrations.models import GoogleConnection
from integrations.views import _user_from_connect_ticket, mint_google_connect_ticket
from organizations.models import Organization


def _make_user(email="founder@example.com"):
    return get_user_model().objects.create_user(email=email, password="x")


def _make_snapshot(organization, *, run_id, days_ago, score, technical=80):
    return WebsiteBaselineSnapshot.objects.create(
        organization=organization,
        domain=organization.domain,
        run_id=run_id,
        status="completed",
        collected_at=timezone.now() - timedelta(days=days_ago),
        overall_score=score,
        metrics={
            "technicalHealth": {"status": "measured", "score": technical},
            "lighthouse": {"status": "unavailable", "score": None},
        },
        source_status={"technicalHealth": "measured", "lighthouse": "unavailable"},
        raw_payload={"scoreCoverage": 40},
    )


class BaselineHistoryEndpointTest(TestCase):
    def setUp(self):
        self.user = _make_user()
        self.organization = Organization.objects.create(domain="theproductbus.com", name="The Product Bus")
        self.other_org = Organization.objects.create(domain="other.com", name="Other")
        _make_snapshot(self.organization, run_id="run-old", days_ago=30, score=52)
        _make_snapshot(self.organization, run_id="run-new", days_ago=1, score=61)
        _make_snapshot(self.other_org, run_id="run-foreign", days_ago=2, score=90)

    def _get(self):
        factory = APIRequestFactory()
        request = factory.get("/content-factory/vibe-marketing/baseline/history")
        force_authenticate(request, user=self.user)
        context = SimpleNamespace(organization=self.organization, profile=None, company=None)
        with mock.patch.object(
            vibe_marketing_views, "_resolve_context_or_response", return_value=(context, None)
        ):
            return VibeMarketingBaselineHistoryView.as_view()(request)

    def test_returns_own_org_snapshots_ascending(self):
        response = self._get()
        self.assertEqual(response.status_code, 200)
        points = response.data["snapshots"]
        self.assertEqual([p["runId"] for p in points], ["run-old", "run-new"])
        self.assertEqual([p["overallScore"] for p in points], [52, 61])

    def test_points_carry_metric_scores_and_coverage(self):
        response = self._get()
        point = response.data["snapshots"][0]
        self.assertEqual(point["metricScores"]["technicalHealth"], 80)
        self.assertIsNone(point["metricScores"]["lighthouse"])
        self.assertEqual(point["scoreCoverage"], 40)


class GoogleConnectTicketTest(TestCase):
    def test_mint_and_consume_roundtrip(self):
        user = _make_user()
        ticket = mint_google_connect_ticket(user)
        self.assertEqual(_user_from_connect_ticket(ticket), user)

    def test_garbage_ticket_is_rejected(self):
        self.assertIsNone(_user_from_connect_ticket("not-a-ticket"))
        self.assertIsNone(_user_from_connect_ticket(None))

    def test_expired_ticket_is_rejected(self):
        user = _make_user()
        ticket = mint_google_connect_ticket(user)
        with mock.patch("integrations.views.GOOGLE_CONNECT_TICKET_MAX_AGE_SECONDS", -1):
            self.assertIsNone(_user_from_connect_ticket(ticket))

    def test_inactive_user_ticket_is_rejected(self):
        user = _make_user()
        ticket = mint_google_connect_ticket(user)
        user.is_active = False
        user.save(update_fields=["is_active"])
        self.assertIsNone(_user_from_connect_ticket(ticket))

    @override_settings(
        GOOGLE_OAUTH_CLIENT_ID="client-id",
        GOOGLE_OAUTH_CLIENT_SECRET="client-secret",
        GOOGLE_OAUTH_REDIRECT_URI="https://api.example.com/integrations/callback/google",
    )
    def test_connect_with_ticket_reaches_google_without_session(self):
        user = _make_user()
        ticket = mint_google_connect_ticket(user)
        response = self.client.get(
            "/integrations/connect/google",
            {"scope": "website_baseline", "ticket": ticket},
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response["Location"].startswith("https://accounts.google.com/o/oauth2/v2/auth?"))
        self.assertIn("webmasters.readonly", response["Location"])

    def test_connect_without_ticket_or_session_bounces_to_login(self):
        response = self.client.get("/integrations/connect/google", {"scope": "website_baseline"})
        self.assertEqual(response.status_code, 302)
        self.assertIn("/platform/login", response["Location"])


class GoogleBaselineConnectUrlTest(TestCase):
    def test_connect_url_carries_company_id_and_ticket(self):
        user = _make_user()
        factory = APIRequestFactory()
        request = factory.get("/content-factory/vibe-marketing/bootstrap/")
        request.user = user
        context = SimpleNamespace(company=SimpleNamespace(id=42), organization=None, profile=None)

        url = _google_baseline_connect_url(request, context)

        self.assertIn("company_id=42", url)
        self.assertIn("ticket=", url)
        self.assertIn("company_id%3D42", url)  # inside the encoded next URL
        from urllib.parse import parse_qs, urlparse

        ticket = parse_qs(urlparse(url).query)["ticket"][0]
        self.assertEqual(_user_from_connect_ticket(ticket), user)

    def test_connect_url_without_context_still_works(self):
        user = _make_user()
        factory = APIRequestFactory()
        request = factory.get("/content-factory/vibe-marketing/bootstrap/")
        request.user = user
        url = _google_baseline_connect_url(request)
        self.assertIn("scope=website_baseline", url)
        self.assertNotIn("company_id=", url)


class OrgScopedConnectionStatusTest(TestCase):
    def setUp(self):
        self.user = _make_user()
        self.org_a = Organization.objects.create(domain="a.com", name="A")
        self.org_b = Organization.objects.create(domain="b.com", name="B")

    def test_status_reflects_the_viewed_org_not_a_sibling(self):
        GoogleConnection.objects.create(
            user=self.user,
            organization=self.org_a,
            google_email="a@example.com",
            refresh_token="tok",
            scope=google_baseline.GSC_SCOPE,
        )
        status_a = google_baseline.google_baseline_connection_status(self.user, self.org_a)
        status_b = google_baseline.google_baseline_connection_status(self.user, self.org_b)
        self.assertTrue(status_a["connected"])
        self.assertTrue(status_a["hasBaselineScopes"])
        self.assertFalse(status_b["connected"])

    def test_legacy_orgless_connection_is_visible_to_any_org(self):
        GoogleConnection.objects.create(
            user=self.user,
            organization=None,
            google_email="legacy@example.com",
            refresh_token="tok",
            scope=google_baseline.GSC_SCOPE,
        )
        status_b = google_baseline.google_baseline_connection_status(self.user, self.org_b)
        self.assertTrue(status_b["connected"])
