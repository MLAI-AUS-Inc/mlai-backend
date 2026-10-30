"""P0.11: nightly island refresh only for orgs with researched keywords, never during workshops (IF-19)."""
import json
from datetime import datetime, timedelta, timezone as datetime_timezone
from types import SimpleNamespace
from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase, override_settings

from content_factory.models import (
    ContentIsland,
    ContentIslandRefreshDispatch,
    ContentIslandStatus,
    OrganizationContentConfig,
    ResearchedKeyword,
)
from content_factory.services import island_refresh_scheduler as scheduler
from content_factory.website_models import WebsiteConnection
from organizations.models import Organization

POST_HELPER = "content_factory.services.island_refresh_scheduler._post_content_factory_queue_request"
# 00:00Z is 10:00 in Melbourne, past the 06:00 local gate.
DUE_NOW = datetime(2026, 10, 30, 0, 0, tzinfo=datetime_timezone.utc)
PILLARS = {"pillars": [{"name": "AI Grants", "keyword": "ai grants"}]}


def _accepted(run_id="island-refresh-run"):
    return SimpleNamespace(status_code=202, text="", json=lambda: {"run_id": run_id, "status": "queued"})


@override_settings(
    CONTENT_ISLANDS_SCHEDULER_ENABLED=True,
    CONTENT_ISLANDS_REFRESH_LOCAL_HOUR=6,
    CONTENT_ISLANDS_REFRESH_MAX_PER_TICK=10,
    ARTICLES_WORKSHOP_WINDOWS="[]",
)
class IslandRefreshKeywordEligibilityTests(TestCase):
    def setUp(self):
        cache.clear()

    def make_org(self, domain, *, keywords=0, islands=0, strategy=None):
        organization = Organization.objects.create(name=domain, domain=domain)
        OrganizationContentConfig.objects.create(organization=organization, pillar_strategy=strategy or {})
        for index in range(keywords):
            ResearchedKeyword.objects.create(organization=organization, keyword=f"kw {index}",
                                             keyword_normalized=f"kw {index}", volume=100)
        for index in range(islands):
            ContentIsland.objects.create(organization=organization, slug=f"island-{index}", name=f"Island {index}",
                                         pillar_keyword=f"island {index}", status=ContentIslandStatus.VISIBLE,
                                         centroid_embedding=[1.0, 0.0])
        return organization

    def test_an_org_with_keywords_and_no_website_connection_is_dispatched_unbound(self):
        organization = self.make_org("keywords-only.example.com", keywords=3, strategy=PILLARS)
        self.assertFalse(WebsiteConnection.objects.filter(organization=organization).exists())

        with patch(POST_HELPER, return_value=_accepted()) as post:
            result = scheduler.run_island_refresh_scheduler(now=DUE_NOW)

        self.assertEqual(result["dispatched"], 1)
        self.assertEqual(result["failed"], 0)
        body = post.call_args.kwargs["payload"]
        self.assertEqual(set(body), {"domain", "client_request_id", "request_source", "include_expansion"})
        self.assertEqual(body["request_source"], "mlai_backend_scheduler")
        self.assertNotIn("queue_class", body)

    def test_an_org_without_keywords_is_skipped_never_failed_or_dispatched(self):
        empty_islands = self.make_org("islands-no-keywords.example.com", islands=2)
        pillar_only = self.make_org("pillars-no-keywords.example.com", strategy=PILLARS)
        self.make_org("ready.example.com", keywords=1, islands=1)

        with patch(POST_HELPER, return_value=_accepted()) as post, \
                self.assertLogs(scheduler.logger, level="INFO") as logs:
            result = scheduler.run_island_refresh_scheduler(now=DUE_NOW)

        self.assertEqual(result["skipped_no_keywords"], 2)
        self.assertEqual(result["dispatched"], 1)
        self.assertEqual(result["failed"], 0)
        self.assertEqual([call.kwargs["payload"]["domain"] for call in post.call_args_list], ["ready.example.com"])
        for organization in (empty_islands, pillar_only):
            self.assertFalse(ContentIslandRefreshDispatch.objects.filter(organization=organization).exists())
        skipped = [line for line in logs.output if "island_refresh_skipped" in line]
        self.assertEqual(len(skipped), 2)
        self.assertTrue(all(line.startswith("INFO:") for line in skipped))

    def test_the_skip_is_logged_once_per_org_local_day(self):
        self.make_org("pillars-no-keywords.example.com", strategy=PILLARS)

        with patch(POST_HELPER, return_value=_accepted()), self.assertLogs(scheduler.logger, level="INFO") as logs:
            scheduler.run_island_refresh_scheduler(now=DUE_NOW)
            second = scheduler.run_island_refresh_scheduler(now=DUE_NOW + timedelta(minutes=1))

        self.assertEqual(second["skipped_no_keywords"], 1)
        self.assertEqual(sum("island_refresh_skipped" in line for line in logs.output), 1)

    def test_an_org_with_nothing_to_cluster_is_not_counted_as_skipped(self):
        self.make_org("empty.example.com")

        with patch(POST_HELPER, return_value=_accepted()) as post:
            result = scheduler.run_island_refresh_scheduler(now=DUE_NOW)

        post.assert_not_called()
        self.assertEqual(result["skipped_no_keywords"], 0)

    def test_a_strategy_holding_only_private_receipts_is_not_eligible(self):
        """A config created just to hold ops alerts or an offboarded owner never dispatches."""
        organization = self.make_org(
            "receipts-only.example.com",
            strategy={"ops_alerts": {"founder_journey": {"status": "sent"}},
                      "previous_owner": {"user_id": 1, "company_id": 2}},
        )

        with patch(POST_HELPER, return_value=_accepted()) as post:
            result = scheduler.run_island_refresh_scheduler(now=DUE_NOW)

        post.assert_not_called()
        self.assertEqual(result["dispatched"], 0)
        self.assertEqual(result["failed"], 0)
        self.assertFalse(ContentIslandRefreshDispatch.objects.filter(organization=organization).exists())

    def test_has_researched_keywords_mirrors_the_rows_content_factory_loads(self):
        with_keywords = self.make_org("with.example.com", keywords=1)
        islands_only = self.make_org("islands.example.com", islands=1, strategy=PILLARS)
        configs = {config.organization_id: config for config in OrganizationContentConfig.objects.all()}

        self.assertTrue(scheduler.has_researched_keywords(configs[with_keywords.pk]))
        self.assertFalse(scheduler.has_researched_keywords(configs[islands_only.pk]))
        self.assertFalse(scheduler.has_researched_keywords(SimpleNamespace(organization_id=None)))


@override_settings(
    CONTENT_ISLANDS_SCHEDULER_ENABLED=True,
    CONTENT_ISLANDS_REFRESH_LOCAL_HOUR=6,
    CONTENT_ISLANDS_REFRESH_MAX_PER_TICK=10,
)
class IslandRefreshWorkshopWindowTests(TestCase):
    def setUp(self):
        cache.clear()
        organization = Organization.objects.create(name="Ready", domain="ready.example.com")
        OrganizationContentConfig.objects.create(organization=organization, pillar_strategy=PILLARS)
        ResearchedKeyword.objects.create(organization=organization, keyword="ai grants",
                                         keyword_normalized="ai grants", volume=100)

    def _windows(self, *pairs):
        return json.dumps([{"start": start.isoformat(), "end": end.isoformat()} for start, end in pairs])

    def test_inside_a_workshop_window_nothing_is_dispatched(self):
        window = self._windows((DUE_NOW - timedelta(hours=1), DUE_NOW + timedelta(hours=2)))
        with override_settings(ARTICLES_WORKSHOP_WINDOWS=window), patch(POST_HELPER, return_value=_accepted()) as post:
            result = scheduler.run_island_refresh_scheduler(now=DUE_NOW)

        post.assert_not_called()
        self.assertEqual(result["status"], "workshop_window")
        self.assertEqual(result["dispatched"], 0)
        self.assertFalse(ContentIslandRefreshDispatch.objects.exists())

    def test_outside_every_window_the_refresh_runs(self):
        window = self._windows(
            (DUE_NOW - timedelta(hours=5), DUE_NOW - timedelta(hours=3)),
            (DUE_NOW + timedelta(hours=1), DUE_NOW + timedelta(hours=3)),
        )
        with override_settings(ARTICLES_WORKSHOP_WINDOWS=window), patch(POST_HELPER, return_value=_accepted()) as post:
            result = scheduler.run_island_refresh_scheduler(now=DUE_NOW)

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["dispatched"], 1)
        post.assert_called_once()

    def test_the_window_end_is_exclusive(self):
        window = self._windows((DUE_NOW - timedelta(hours=2), DUE_NOW))
        with override_settings(ARTICLES_WORKSHOP_WINDOWS=window), patch(POST_HELPER, return_value=_accepted()):
            result = scheduler.run_island_refresh_scheduler(now=DUE_NOW)

        self.assertEqual(result["status"], "ok")

    def test_naive_window_times_are_read_as_utc(self):
        window = json.dumps([{"start": "2026-10-29T23:00:00", "end": "2026-10-30T01:00:00"}])
        with override_settings(ARTICLES_WORKSHOP_WINDOWS=window), patch(POST_HELPER, return_value=_accepted()) as post:
            result = scheduler.run_island_refresh_scheduler(now=DUE_NOW)

        post.assert_not_called()
        self.assertEqual(result["status"], "workshop_window")

    def test_malformed_windows_are_ignored_with_a_warning(self):
        cases = ("not json", json.dumps({"start": "x"}), json.dumps([{"start": "x", "end": "y"}, "nope"]),
                 json.dumps([{"start": DUE_NOW.isoformat(), "end": (DUE_NOW - timedelta(hours=1)).isoformat()}]))
        for raw in cases:
            with self.subTest(raw=raw):
                ContentIslandRefreshDispatch.objects.all().delete()
                with override_settings(ARTICLES_WORKSHOP_WINDOWS=raw), \
                        patch(POST_HELPER, return_value=_accepted()), \
                        self.assertLogs(scheduler.logger, level="WARNING"):
                    result = scheduler.run_island_refresh_scheduler(now=DUE_NOW)
                self.assertEqual(result["status"], "ok")
                self.assertEqual(result["dispatched"], 1)

    def test_an_empty_setting_means_no_windows(self):
        for raw in ("", "[]", None, []):
            with self.subTest(raw=raw), override_settings(ARTICLES_WORKSHOP_WINDOWS=raw):
                self.assertIsNone(scheduler.active_workshop_window(DUE_NOW))
