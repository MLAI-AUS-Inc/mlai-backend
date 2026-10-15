"""Complete endpoint budget, run only in the existing disposable PostgreSQL CI."""
from contextlib import ExitStack
import json
import os
from time import perf_counter
from unittest.mock import patch

from django.core.cache import cache
from django.db import connection
from django.test.utils import CaptureQueriesContext

from content_factory import vibe_marketing_views as views
from content_factory.website_models import WebsiteConnection
from tests.test_content_island_bootstrap import BOOTSTRAP_URL, ContentIslandBootstrapTestCase
from workflow_runs.models import ContentFactoryRun


class BootstrapEndpointBudgetTests(ContentIslandBootstrapTestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)
        self.website = WebsiteConnection.objects.create(organization=self.organization, authorized_by=self.user,
            github_repo="fixture/site", repository_id=7002, installation_id="7003", branch="main",
            site_url="https://acme.com", verified_sha="a" * 40)
        self.config.website_connection = self.website
        self.config.github_repo = self.website.github_repo
        self.config.github_installation_id = self.website.installation_id
        self.config.save(update_fields=["website_connection", "github_repo", "github_installation_id", "updated_at"])

    def seed_runs(self, count):
        html = "<article>" + "large-review-sentinel " * 90_000 + "</article>"
        ContentFactoryRun.objects.bulk_create([ContentFactoryRun(run_id=f"budget-article-{index}",
            organization=self.organization, domain=self.organization.domain, github_repo=self.website.github_repo,
            workflow="article_generation", status="completed", run_request={"keyword": f"fixture topic {index}",
                "delivery_mode": "content_only", "website_connection_id": str(self.website.pk),
                "connection_generation": self.website.generation},
            result={"review_draft_html": html, "title": f"Fixture article {index}",
                "diagnostics": {"logs": "raw-diagnostic-sentinel " * 10_000},
                "article_body": "raw-body-sentinel " * 10_000}) for index in range(count)])

    def read_boundaries(self, stack):
        stack.enter_context(patch.dict(os.environ, {"VIBE_BOOTSTRAP_CACHE_SECONDS": "300"}))
        mint = stack.enter_context(patch("integrations.services.github_app.create_installation_access_token",
            side_effect=AssertionError("Bootstrap must not mint GitHub credentials")))
        probe = stack.enter_context(patch("integrations.services.github_app.probe_installation_liveness",
            side_effect=AssertionError("Bootstrap must not probe GitHub")))
        http = stack.enter_context(patch("requests.sessions.Session.request",
            side_effect=AssertionError("Bootstrap must not make HTTP requests")))
        refresh = stack.enter_context(patch.object(views, "_refresh_pending_article_system_setup_pr_status",
            side_effect=AssertionError("Bootstrap must not reconcile setup")))
        publish = stack.enter_context(patch.object(views, "refresh_publish_statuses",
            side_effect=AssertionError("Bootstrap must not reconcile publication")))
        return mint, probe, http, refresh, publish

    def test_complete_twenty_four_run_endpoint_cold_and_warm_budget(self):
        self.seed_runs(24)
        samples = []
        with ExitStack() as stack:
            forbidden = self.read_boundaries(stack)
            for index in range(5):
                started = perf_counter()
                with CaptureQueriesContext(connection) as queries:
                    response = self.client.get(BOOTSTRAP_URL)
                    body = response.content
                elapsed_ms = (perf_counter() - started) * 1000
                self.assertEqual(response.status_code, 200)
                self.assertEqual(len(response.data["latestRuns"]), 24)
                self.assertEqual(response["X-MLAI-View"], "summary")
                self.assertEqual(int(response["X-MLAI-Payload-Bytes"]), len(body))
                self.assertNotIn(b"large-review-sentinel", body)
                self.assertNotIn(b"raw-diagnostic-sentinel", body)
                self.assertNotIn(b"raw-body-sentinel", body)
                self.assertLess(len(body), 300_000)
                writes = [query["sql"].split(None, 1)[0].upper() for query in queries
                    if query["sql"].lstrip().upper().startswith(("INSERT ", "UPDATE ", "DELETE "))]
                self.assertEqual(writes, [])
                samples.append({"cache": "cold" if index == 0 else "warm", "bytes": len(body),
                    "sql_count": len(queries), "elapsed_ms": round(elapsed_ms, 2)})
            for boundary in forbidden:
                boundary.assert_not_called()
        print(json.dumps({"fixture": "complete bootstrap / 24 persisted large runs", "samples": samples}))
        # With five samples the nearest-rank p95 is the maximum. This CI fixture
        # is regression evidence, not a production latency/support claim.
        self.assertLess(max(sample["elapsed_ms"] for sample in samples), 1000)

    def test_explicit_full_remains_available_without_provider_calls(self):
        self.seed_runs(1)
        with ExitStack() as stack:
            forbidden = self.read_boundaries(stack)
            response = self.client.get(BOOTSTRAP_URL + "?view=full")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response["X-MLAI-View"], "full")
            self.assertIn("large-review-sentinel", response.data["latestRuns"][0]["reviewDraftHtml"])
            for boundary in forbidden:
                boundary.assert_not_called()

    def test_warm_cached_payload_still_reads_current_revocation(self):
        with ExitStack() as stack:
            forbidden = self.read_boundaries(stack)
            first = self.client.get(BOOTSTRAP_URL)
            self.assertEqual(first.status_code, 200)
            self.website.state = WebsiteConnection.State.REVOKED
            self.website.save(update_fields=["state", "updated_at"])
            second = self.client.get(BOOTSTRAP_URL)
            self.assertEqual(second.status_code, 200)
            self.assertEqual(second.data["websiteConnection"]["status"], "revoked")
            self.assertFalse(second.data["articleCapabilities"]["canGenerateArticle"])
            self.assertFalse(second.data["articleCapabilities"]["canPublishArticle"])
            for boundary in forbidden:
                boundary.assert_not_called()

    def test_website_journey_get_also_observes_without_provider_calls(self):
        with ExitStack() as stack:
            forbidden = self.read_boundaries(stack)
            response = self.client.get("/api/v1/my-startup/vibe-marketing/website-connection/")
            self.assertEqual(response.status_code, 200)
            self.assertIn("websiteJourney", response.data)
            for boundary in forbidden:
                boundary.assert_not_called()
