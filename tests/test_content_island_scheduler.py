"""Phase 4: the daily island-refresh runner in the multi-runner scheduler tick.

Structural twin of ``run_daily_article_report_scheduler``: kill switch, org-local
hour gate, DB-unique idempotency, per-org error isolation, bounded results. The
runner must never raise - a raising runner spams the 60-second compose loop.
"""
from datetime import datetime, timedelta, timezone as datetime_timezone
from io import StringIO
from types import SimpleNamespace
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.utils import timezone

from content_factory.models import (
    ClusterMembership,
    ContentIsland,
    ContentIslandRefreshDispatch,
    ContentIslandRefreshDispatchStatus,
    ContentIslandStatus,
    OrganizationContentConfig,
    ResearchedKeyword,
    SemanticCluster,
)
from content_factory.services.island_refresh_scheduler import (
    IDEMPOTENCY_KEY_MAX_LENGTH,
    ISLAND_REFRESH_REQUEST_SOURCE,
    build_island_refresh_idempotency_key,
    run_island_refresh_scheduler,
)
from organizations.models import Organization

POST_HELPER = "content_factory.services.island_refresh_scheduler._post_content_factory_queue_request"

# 2026-08-23T00:00Z is 10:00 in Melbourne (AEST, UTC+10) - past the 06:00 gate.
DUE_NOW = datetime(2026, 8, 23, 0, 0, tzinfo=datetime_timezone.utc)
# 2026-08-22T19:00Z is 05:00 the next morning in Melbourne - before the gate.
TOO_EARLY_NOW = datetime(2026, 8, 22, 19, 0, tzinfo=datetime_timezone.utc)


class _FakeResponse(SimpleNamespace):
    text = ""

    def json(self):
        return self.payload


def _accepted(run_id="island-refresh-run-1"):
    return _FakeResponse(
        status_code=202,
        payload={
            "run_id": run_id,
            "job_id": run_id,
            "workflow": "island_refresh",
            "status": "queued",
        },
    )


class IslandRefreshSchedulerTestCase(TestCase):
    def make_org(self, domain, *, timezone_name="Australia/Melbourne", islands=0, strategy=None, clusters=0):
        organization = Organization.objects.create(name=domain, domain=domain)
        OrganizationContentConfig.objects.create(
            organization=organization,
            default_timezone=timezone_name,
            pillar_strategy=strategy or {},
        )
        for index in range(islands):
            ContentIsland.objects.create(
                organization=organization,
                slug=f"island-{index}",
                name=f"Island {index}",
                pillar_keyword=f"island {index}",
                status=ContentIslandStatus.VISIBLE,
                centroid_embedding=[1.0, 0.0],
            )
        for index in range(clusters):
            cluster = SemanticCluster.objects.create(
                organization=organization,
                cluster_id=index + 1,
                pillar_keyword=f"cluster {index}",
                total_volume=500,
            )
            keyword = ResearchedKeyword.objects.create(
                organization=organization,
                keyword=f"{domain} keyword {index}",
                keyword_normalized=f"{domain} keyword {index}",
                volume=400,
            )
            ClusterMembership.objects.create(keyword=keyword, cluster=cluster, is_pillar=False)
        return organization


@override_settings(
    CONTENT_ISLANDS_SCHEDULER_ENABLED=True,
    CONTENT_ISLANDS_REFRESH_LOCAL_HOUR=6,
    CONTENT_ISLANDS_REFRESH_MAX_PER_TICK=3,
)
class IslandRefreshSchedulerTests(IslandRefreshSchedulerTestCase):
    def test_a_due_org_is_dispatched_with_the_documented_request_body(self):
        organization = self.make_org("acme.com", islands=2)

        with patch(POST_HELPER, return_value=_accepted()) as post:
            result = run_island_refresh_scheduler(now=DUE_NOW)

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["dispatched"], 1)
        endpoint, kwargs = post.call_args[0][0], post.call_args[1]
        self.assertTrue(endpoint.endswith("/api/runs/island-refresh"))
        dispatch = ContentIslandRefreshDispatch.objects.get(organization=organization)
        self.assertEqual(
            kwargs["payload"],
            {
                "domain": "acme.com",
                "client_request_id": dispatch.idempotency_key,
                "request_source": ISLAND_REFRESH_REQUEST_SOURCE,
                "include_expansion": True,
            },
        )

    def test_the_dispatch_row_stores_the_content_factory_run_id(self):
        self.make_org("acme.com", islands=1)

        with patch(POST_HELPER, return_value=_accepted("island-refresh-run-42")):
            run_island_refresh_scheduler(now=DUE_NOW)

        dispatch = ContentIslandRefreshDispatch.objects.get()
        self.assertEqual(dispatch.content_factory_run_id, "island-refresh-run-42")
        self.assertEqual(dispatch.status, ContentIslandRefreshDispatchStatus.QUEUED)
        self.assertEqual(dispatch.local_date.isoformat(), "2026-08-23")

    def test_the_local_hour_gate_holds_an_org_back_until_its_own_morning(self):
        self.make_org("acme.com", islands=1)

        with patch(POST_HELPER, return_value=_accepted()) as post:
            result = run_island_refresh_scheduler(now=TOO_EARLY_NOW)

        self.assertEqual(result["not_due"], 1)
        self.assertEqual(result["dispatched"], 0)
        post.assert_not_called()
        self.assertFalse(ContentIslandRefreshDispatch.objects.exists())

    def test_an_unknown_timezone_falls_back_to_melbourne(self):
        self.make_org("broken-tz.com", timezone_name="Not/AZone", islands=1)
        self.make_org("utc.com", timezone_name="UTC", islands=1)

        with patch(POST_HELPER, return_value=_accepted()):
            result = run_island_refresh_scheduler(now=TOO_EARLY_NOW)

        # 19:00Z is 05:00 in Melbourne (gated) but 19:00 in UTC (due).
        self.assertEqual(result["not_due"], 1)
        self.assertEqual(result["dispatched"], 1)
        dispatch = ContentIslandRefreshDispatch.objects.get()
        self.assertEqual(dispatch.organization.domain, "utc.com")
        self.assertEqual(dispatch.local_date.isoformat(), "2026-08-22")

    def test_a_second_tick_on_the_same_local_day_is_a_cheap_no_op(self):
        self.make_org("acme.com", islands=1)

        with patch(POST_HELPER, return_value=_accepted()) as post:
            run_island_refresh_scheduler(now=DUE_NOW)
            result = run_island_refresh_scheduler(now=DUE_NOW + timedelta(hours=2))

        self.assertEqual(post.call_count, 1)
        self.assertEqual(result["existing"], 1)
        self.assertEqual(result["dispatched"], 0)
        self.assertEqual(ContentIslandRefreshDispatch.objects.count(), 1)

    def test_one_failing_org_never_blocks_the_others(self):
        self.make_org("aaa-broken.com", islands=1)
        self.make_org("bbb-healthy.com", islands=1)

        def _post(endpoint, **kwargs):
            if kwargs["payload"]["domain"] == "aaa-broken.com":
                raise RuntimeError("content factory unreachable")
            return _accepted()

        with patch(POST_HELPER, side_effect=_post):
            result = run_island_refresh_scheduler(now=DUE_NOW)

        self.assertEqual(result["dispatched"], 1)
        self.assertEqual(result["failed"], 1)
        broken = ContentIslandRefreshDispatch.objects.get(organization__domain="aaa-broken.com")
        healthy = ContentIslandRefreshDispatch.objects.get(organization__domain="bbb-healthy.com")
        self.assertEqual(broken.status, ContentIslandRefreshDispatchStatus.FAILED)
        self.assertIn("content factory unreachable", broken.last_error)
        self.assertEqual(healthy.status, ContentIslandRefreshDispatchStatus.QUEUED)

    def test_a_non_2xx_response_fails_the_dispatch_without_raising(self):
        self.make_org("acme.com", islands=1)

        with patch(POST_HELPER, return_value=_FakeResponse(status_code=403, payload={}, text="bad source")):
            result = run_island_refresh_scheduler(now=DUE_NOW)

        self.assertEqual(result["failed"], 1)
        dispatch = ContentIslandRefreshDispatch.objects.get()
        self.assertEqual(dispatch.status, ContentIslandRefreshDispatchStatus.FAILED)
        self.assertIn("403", dispatch.last_error)

    @override_settings(CONTENT_ISLANDS_REFRESH_MAX_PER_TICK=1)
    def test_the_per_tick_cap_defers_the_rest_to_the_next_tick(self):
        self.make_org("aaa.com", islands=1)
        self.make_org("bbb.com", islands=1)
        self.make_org("ccc.com", islands=1)

        with patch(POST_HELPER, return_value=_accepted()):
            first = run_island_refresh_scheduler(now=DUE_NOW)
            second = run_island_refresh_scheduler(now=DUE_NOW)

        self.assertEqual(first["dispatched"], 1)
        self.assertEqual(first["deferred"], 2)
        self.assertEqual(second["dispatched"], 1)
        self.assertEqual(second["existing"], 1)
        self.assertEqual(ContentIslandRefreshDispatch.objects.count(), 2)

    def test_a_queued_dispatch_older_than_three_hours_is_failed(self):
        organization = self.make_org("acme.com", islands=1)
        dispatch = ContentIslandRefreshDispatch.objects.create(
            organization=organization,
            local_date=DUE_NOW.date(),
            idempotency_key="island-refresh:acme.com:2026-08-23",
        )
        ContentIslandRefreshDispatch.objects.filter(pk=dispatch.pk).update(
            updated_at=DUE_NOW - timedelta(hours=4)
        )

        with patch(POST_HELPER, return_value=_accepted()) as post:
            result = run_island_refresh_scheduler(now=DUE_NOW)

        post.assert_not_called()
        dispatch.refresh_from_db()
        self.assertEqual(result["stuck_failed"], 1)
        self.assertEqual(dispatch.status, ContentIslandRefreshDispatchStatus.FAILED)
        self.assertTrue(dispatch.last_error)

    def test_a_fresh_queued_dispatch_is_left_alone(self):
        organization = self.make_org("acme.com", islands=1)
        ContentIslandRefreshDispatch.objects.create(
            organization=organization,
            local_date=DUE_NOW.date(),
            idempotency_key="island-refresh:acme.com:2026-08-23",
        )

        with patch(POST_HELPER, return_value=_accepted()):
            result = run_island_refresh_scheduler(now=DUE_NOW)

        self.assertEqual(result["stuck_failed"], 0)
        self.assertEqual(
            ContentIslandRefreshDispatch.objects.get().status,
            ContentIslandRefreshDispatchStatus.QUEUED,
        )


@override_settings(CONTENT_ISLANDS_SCHEDULER_ENABLED=True, CONTENT_ISLANDS_REFRESH_LOCAL_HOUR=6)
class IslandRefreshEligibilityTests(IslandRefreshSchedulerTestCase):
    def _dispatched_domains(self):
        with patch(POST_HELPER, return_value=_accepted()):
            run_island_refresh_scheduler(now=DUE_NOW)
        return sorted(
            ContentIslandRefreshDispatch.objects.values_list("organization__domain", flat=True)
        )

    def test_a_cold_start_org_with_only_a_pillar_strategy_is_eligible(self):
        self.make_org(
            "cold-start.com",
            strategy={"pillars": [{"name": "AI Startup Fundraising", "keyword": "ai startup fundraising"}]},
        )

        self.assertEqual(self._dispatched_domains(), ["cold-start.com"])

    def test_a_cluster_backed_org_with_no_islands_is_eligible(self):
        self.make_org("clustered.com", clusters=1)

        self.assertEqual(self._dispatched_domains(), ["clustered.com"])

    def test_an_org_with_nothing_seedable_is_skipped(self):
        self.make_org("empty.com")
        self.make_org("islanded.com", islands=1)

        self.assertEqual(self._dispatched_domains(), ["islanded.com"])

    def test_an_org_without_a_content_config_is_skipped(self):
        organization = Organization.objects.create(name="No Config", domain="no-config.com")
        ContentIsland.objects.create(
            organization=organization,
            slug="orphan",
            name="Orphan",
            pillar_keyword="orphan",
            status=ContentIslandStatus.VISIBLE,
            centroid_embedding=[1.0, 0.0],
        )

        self.assertEqual(self._dispatched_domains(), [])

    def test_an_empty_pillar_list_is_not_seedable(self):
        self.make_org("empty-strategy.com", strategy={"pillars": []})

        self.assertEqual(self._dispatched_domains(), [])


class IslandRefreshKillSwitchTests(IslandRefreshSchedulerTestCase):
    @override_settings(CONTENT_ISLANDS_SCHEDULER_ENABLED=False)
    def test_the_kill_switch_short_circuits_before_any_query_or_post(self):
        self.make_org("acme.com", islands=1)

        with patch(POST_HELPER, return_value=_accepted()) as post:
            result = run_island_refresh_scheduler(now=DUE_NOW)

        self.assertEqual(result, {"status": "disabled", "dispatched": 0})
        post.assert_not_called()
        self.assertFalse(ContentIslandRefreshDispatch.objects.exists())


class IslandRefreshIdempotencyKeyTests(TestCase):
    def test_the_key_is_stable_and_dated(self):
        key = build_island_refresh_idempotency_key("mlai.au", timezone.now().date())
        self.assertTrue(key.startswith("island-refresh:mlai.au:"))
        self.assertLessEqual(len(key), IDEMPOTENCY_KEY_MAX_LENGTH)

    def test_a_very_long_domain_is_clamped_to_the_varchar_limit(self):
        long_domain = "a" * 240 + ".example.com"
        key = build_island_refresh_idempotency_key(long_domain, timezone.now().date())

        # sqlite does not enforce varchar(100) - Ledger.reference_id in prod does.
        self.assertEqual(len(key), IDEMPOTENCY_KEY_MAX_LENGTH)
        self.assertTrue(key.startswith("island-refresh:"))
        self.assertTrue(key.endswith(timezone.now().date().isoformat()))


@override_settings(CONTENT_ISLANDS_SCHEDULER_ENABLED=False, CONTENT_ISLANDS_REFRESH_LOCAL_HOUR=23)
class RefreshContentIslandsCommandTests(IslandRefreshSchedulerTestCase):
    """The pilot bypasses both the kill switch and the hour gate by design."""

    def test_the_pilot_dispatches_despite_the_kill_switch_and_hour_gate(self):
        self.make_org("acme.com", islands=1)
        stdout = StringIO()

        with patch(POST_HELPER, return_value=_accepted("pilot-run-1")):
            call_command("refresh_content_islands", "--domain", "acme.com", stdout=stdout)

        self.assertIn("dispatched", stdout.getvalue())
        dispatch = ContentIslandRefreshDispatch.objects.get()
        self.assertEqual(dispatch.content_factory_run_id, "pilot-run-1")

    def test_without_force_an_existing_dispatch_is_left_alone(self):
        self.make_org("acme.com", islands=1)

        with patch(POST_HELPER, return_value=_accepted()) as post:
            call_command("refresh_content_islands", "--domain", "acme.com", stdout=StringIO())
            call_command("refresh_content_islands", "--domain", "acme.com", stdout=StringIO())

        self.assertEqual(post.call_count, 1)

    def test_force_re_dispatches_the_same_local_day(self):
        self.make_org("acme.com", islands=1)

        with patch(POST_HELPER, return_value=_accepted()) as post:
            call_command("refresh_content_islands", "--domain", "acme.com", stdout=StringIO())
            call_command("refresh_content_islands", "--domain", "acme.com", "--force", stdout=StringIO())

        self.assertEqual(post.call_count, 2)
        self.assertEqual(ContentIslandRefreshDispatch.objects.count(), 1)

    def test_no_expansion_flips_the_request_body(self):
        self.make_org("acme.com", islands=1)

        with patch(POST_HELPER, return_value=_accepted()) as post:
            call_command(
                "refresh_content_islands", "--domain", "acme.com", "--no-expansion", stdout=StringIO()
            )

        self.assertFalse(post.call_args[1]["payload"]["include_expansion"])

    def test_an_unknown_domain_fails_loudly(self):
        with self.assertRaises(CommandError):
            call_command("refresh_content_islands", "--domain", "nope.com", stdout=StringIO())


class IslandRefreshRunnerRegistrationTests(TestCase):
    def test_the_runner_is_registered_in_the_scheduler_tick(self):
        import inspect

        from core.management.commands import run_scheduled_discovery

        source = inspect.getsource(run_scheduled_discovery.Command.handle)
        self.assertIn('("content_island_refresh", run_island_refresh_scheduler)', source)
