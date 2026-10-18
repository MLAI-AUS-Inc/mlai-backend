"""Actual application memory window, only in disposable owned PostgreSQL CI."""

import json
import os
from unittest import skipUnless

from django.db import connection
from django.core.cache import cache
from django.test import TransactionTestCase

from content_factory.website_models import WebsiteConnection
from scripts.bootstrap_worker_profile import child_environment, profile_window, memory_ceiling_bytes
from tests import test_articles_bootstrap_endpoint_budget as budget_fixture
from tests import test_content_island_bootstrap as bootstrap_fixture


@skipUnless(os.environ.get("ARTICLES_BOOTSTRAP_MEMORY_CI") == "1", "Dedicated disposable Linux application-memory CI only")
class BootstrapWorkerMemoryTests(TransactionTestCase):
    def test_actual_bootstrap_lifetime_with_unchanged_recycle_limits(self):
        # Admit the exact disposable test database before creating any fixture.
        child_environment(database=connection.settings_dict, user_id=1)
        bootstrap_fixture.ContentIslandBootstrapTestCase.setUp(self)
        cache.clear()
        self.addCleanup(cache.clear)
        self.website = WebsiteConnection.objects.create(organization=self.organization, authorized_by=self.user,
            github_repo="fixture/site", repository_id=7002, installation_id="7003", branch="main",
            site_url="https://acme.com", verified_sha="a" * 40)
        self.config.website_connection = self.website
        self.config.github_repo = self.website.github_repo
        self.config.github_installation_id = self.website.installation_id
        self.config.save(update_fields=["website_connection", "github_repo", "github_installation_id", "updated_at"])
        budget_fixture.BootstrapEndpointBudgetTests.seed_runs(self, 24)
        results = [profile_window(database=connection.settings_dict, user_id=self.user.pk, limit=limit, jitter=jitter)
            for limit, jitter in ((300, 50),)]
        self.assertEqual([row["completed_requests"] for row in results], [1200])
        self.assertEqual([row["worker_count"] for row in results], [1])
        for row in results:
            # A fixed synthetic regression bound with headroom above the old
            # 347,216 KiB fixed fixture, not a production memory promise.
            self.assertLess(row["max_peak_rss_kib"], 512 * 1024)
        print(json.dumps({"fixture": "actual compact bootstrap / 24 large runs / synthetic forced-auth owner",
            "release_sha": os.environ["GITHUB_SHA"], "memory_ceiling_bytes": memory_ceiling_bytes(), "windows": results}, sort_keys=True))
