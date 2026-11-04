"""The P2 runtime foundation (be-registry): sweeps, lazy routes, request counters and idempotency.

Covers the acceptance lines for IF-P2-08, IF-P2-25, ``staff_signals`` and P11.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone as dt_timezone
import json
import threading
from unittest.mock import patch

from django.core.cache import cache
from django.http import HttpResponse
from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings
from django.urls import resolve
from rest_framework.response import Response
from rest_framework.views import APIView

from founder_tools import idempotency
from . import self_heal_sweeps, staff_signals
from .lazy_views import NOT_FOUND, lazy_view
from .self_heal_testing import ALL_FLAGS_ON, fake_provider, missing_provider

LAZY_ROUTES = (
    ("/api/content-factory/runs/run-1/repin", ("content_factory.source_repin_views", "SourceRepinView")),
    ("/api/content-factory/runs/run-1/repin/", ("content_factory.source_repin_views", "SourceRepinView")),
    ("/api/v1/my-startup/runs/run-1/publish-reviewed/",
     ("content_factory.publish_reviewed_draft_views", "PublishReviewedDraftView")),
    ("/api/v1/my-startup/team/", ("founder_tools.startup_team_views", "TeamView")),
    ("/api/v1/my-startup/team/requests/", ("founder_tools.startup_team_views", "JoinRequestView")),
    ("/api/v1/my-startup/team/requests/3f8e2d7a-1b2c-4d5e-8f90-123456789abc/decision/",
     ("founder_tools.startup_team_views", "JoinRequestDecisionView")),
    ("/api/v1/my-startup/team/members/3f8e2d7a-1b2c-4d5e-8f90-123456789abc/",
     ("founder_tools.startup_team_views", "TeamMemberView")),
    ("/api/v1/my-startup/team/invite-link/", ("founder_tools.startup_team_views", "InviteLinkView")),
    ("/api/v1/my-startup/team/join/", ("founder_tools.startup_team_views", "InviteJoinView")),
    ("/api/v1/community-chat/admin/startups/", ("community_chat.staff_console_views", "StartupListView")),
    ("/api/v1/community-chat/admin/startups/12/", ("community_chat.staff_console_views", "StartupDetailView")),
    ("/api/v1/community-chat/admin/startups/12/actions/", ("community_chat.staff_console_views", "StartupActionView")),
    ("/api/v1/community-chat/admin/alerts/", ("community_chat.staff_console_views", "AlertListView")),
    ("/api/v1/community-chat/admin/alerts/abc-1/ack/", ("community_chat.staff_console_views", "AlertAckView")),
)


class SweepRunnerTests(SimpleTestCase):
    """IF-P2-08: isolation, order and the failure log."""

    def test_the_sweep_table(self):
        self.assertEqual([name for name, _, _ in self_heal_sweeps.SWEEPS], [
            "failure_policy", "no_delivery_refunds", "merge_worker", "deployment_watch",
            "founder_transitions", "team_requests", "staff_alert_rules", "self_heal_report"])

    def test_missing_raising_and_flag_off_runners_are_isolated_in_order(self):
        calls = []

        def flag_off(now=None, limit=None):
            calls.append("flag_off")
            return {"skipped": "flag_off"}

        def raising(now=None, limit=None):
            calls.append("raising")
            raise RuntimeError("sweep broke")

        def working(now=None, limit=None):
            calls.append("working")
            return {"processed": 2, "now": now.isoformat()}

        now = datetime(2026, 10, 31, tzinfo=dt_timezone.utc)
        sweeps = (("first", "content_factory.p2_fake_sweeps_a", "flag_off"),
                  ("absent", "content_factory.p2_fake_sweeps_missing", "run"),
                  ("broken", "content_factory.p2_fake_sweeps_a", "raising"),
                  ("no_function", "content_factory.p2_fake_sweeps_a", "not_there"),
                  ("last", "content_factory.p2_fake_sweeps_a", "working"))
        with fake_provider("content_factory.p2_fake_sweeps_a", flag_off=flag_off, raising=raising, working=working), \
                missing_provider("content_factory.p2_fake_sweeps_missing"):
            with self.assertLogs("content_factory.self_heal_sweeps", "WARNING") as logs:
                results = self_heal_sweeps.run_self_heal_sweeps(now=now, sweeps=sweeps)
        self.assertEqual(list(results), ["first", "absent", "broken", "no_function", "last"])
        self.assertEqual(results["first"], {"skipped": "flag_off"})
        self.assertEqual(results["absent"], "missing")
        self.assertEqual(results["broken"], "error")
        self.assertEqual(results["no_function"], "missing")
        self.assertEqual(results["last"], {"processed": 2, "now": now.isoformat()})
        self.assertEqual(calls, ["flag_off", "raising", "working"])
        self.assertEqual(len(logs.output), 1)
        self.assertIn("self_heal_sweep_failed name=broken", logs.output[0])

    def test_a_broken_import_is_reported_and_the_next_sweep_runs(self):
        sweeps = (("bad_import", "content_factory.p2_fake_sweeps_b", "run"),
                  ("next", "content_factory.p2_fake_sweeps_c", "run"))
        with patch.object(self_heal_sweeps, "_module_available", return_value=True), \
                patch.object(self_heal_sweeps.importlib, "import_module",
                             side_effect=[SyntaxError("bad"), type("M", (), {"run": staticmethod(lambda now=None: {"ok": 1})})]):
            with self.assertLogs("content_factory.self_heal_sweeps", "WARNING") as logs:
                results = self_heal_sweeps.run_self_heal_sweeps(sweeps=sweeps)
        self.assertEqual(results, {"bad_import": "error", "next": {"ok": 1}})
        self.assertIn("self_heal_sweep_failed name=bad_import stage=import", logs.output[0])

    def test_datetime_result_is_json_safe_and_does_not_break_scheduler(self):
        now = datetime(2026, 11, 2, tzinfo=dt_timezone.utc)
        sweeps = (("date", "content_factory.p2_fake_json", "run"),)
        with fake_provider("content_factory.p2_fake_json", run=lambda now: {"next_at": now, "processed": 2}):
            result = self_heal_sweeps.run_self_heal_sweeps(now=now, sweeps=sweeps)
        self.assertEqual(json.loads(json.dumps(result)), {"date": {"next_at": str(now), "processed": 2}})

    def test_missing_packages_are_reported_without_importing_them(self):
        with patch.object(self_heal_sweeps, "_module_available", return_value=False):
            result = self_heal_sweeps.scheduled_self_heal_sweeps()
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["errors"], 0)
        self.assertEqual(set(result["sweeps"]), {name for name, _, _ in self_heal_sweeps.SWEEPS})
        self.assertTrue(all(value == "missing" for value in result["sweeps"].values()))


class SchedulerWiringTests(TestCase):
    """IF-P2-08: one call per tick, after the outbox step, whatever the P1 V2 switch says."""

    def _runner_names(self):
        captured = []

        def fake_run_runners(runners):
            captured.append([name for name, _ in runners])
            return {}, []

        from django.core.management import call_command
        with patch("core.management.commands.run_scheduled_discovery.run_runners", side_effect=fake_run_runners):
            call_command("run_scheduled_discovery")
        self.assertEqual(len(captured), 1)
        return captured[0]

    def test_called_once_after_the_outbox(self):
        for flags in ({"MY_STARTUP_FOUNDER_JOURNEY_V2_ENABLED": False}, {"MY_STARTUP_FOUNDER_JOURNEY_V2_ENABLED": True},
                      ALL_FLAGS_ON):
            with self.subTest(v2=flags.get("MY_STARTUP_FOUNDER_JOURNEY_V2_ENABLED")), override_settings(**flags):
                names = self._runner_names()
                self.assertEqual(names.count("self_heal_sweeps"), 1)
                self.assertEqual(names.index("self_heal_sweeps"), names.index("article_dispatch_outbox") + 1)

    def test_the_scheduled_entry_is_the_runner(self):
        from core.management.commands import run_scheduled_discovery
        self.assertIs(run_scheduled_discovery.scheduled_self_heal_sweeps, self_heal_sweeps.scheduled_self_heal_sweeps)


class FakeView(APIView):
    authentication_classes = []
    permission_classes = []

    def get(self, request, *args, **kwargs):
        return Response({"fake": True, "kwargs": {key: str(value) for key, value in kwargs.items()}})

    def post(self, request, *args, **kwargs):
        return Response({"fake": True})


class LazyRouteTests(TestCase):
    """IF-P2-25: every lazy route answers 404 while its view is missing, and resolves once it is there."""

    def test_every_route_resolves_to_its_lazy_view(self):
        for path, target in LAZY_ROUTES:
            with self.subTest(path=path):
                self.assertEqual(getattr(resolve(path).func, "lazy_target", None), target)

    def test_repin_is_not_caught_by_the_run_control_route(self):
        for suffix in ("", "/"):
            match = resolve(f"/api/content-factory/runs/abc123/repin{suffix}")
            self.assertEqual(match.func.lazy_target, ("content_factory.source_repin_views", "SourceRepinView"))
            self.assertEqual(match.kwargs, {"run_id": "abc123"})
        other = resolve("/api/content-factory/runs/abc123/cancel/")
        self.assertIsNone(getattr(other.func, "lazy_target", None))

    def test_missing_views_answer_the_not_found_body(self):
        for path, (module, _) in LAZY_ROUTES:
            with self.subTest(path=path), missing_provider(module):
                resolve(path).func.lazy_reset()
                for method in ("get", "post"):
                    response = getattr(self.client, method)(path)
                    self.assertEqual(response.status_code, 404)
                    self.assertEqual(json.loads(response.content), NOT_FOUND)

    def test_a_fake_view_is_resolved_and_cached(self):
        for path, (module, attr) in LAZY_ROUTES:
            with self.subTest(path=path):
                route = resolve(path).func
                route.lazy_reset()
                self.addCleanup(route.lazy_reset)
                with fake_provider(module, **{attr: FakeView}):
                    response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.json()["fake"])
                # Cached: still served after the fake module is gone.
                self.assertEqual(self.client.get(path).status_code, 200)
                route.lazy_reset()
                with missing_provider(module):
                    self.assertEqual(self.client.get(path).status_code, 404)

    def test_a_missing_class_in_a_present_module_is_404(self):
        view = lazy_view("content_factory.p2_fake_views", "Missing")
        with fake_provider("content_factory.p2_fake_views", Other=FakeView):
            response = view(RequestFactory().get("/x"))
        self.assertEqual((response.status_code, json.loads(response.content)), (404, NOT_FOUND))


class StaffSignalTests(TestCase):
    """Acceptance: a 429 on /api/v1/my-startup/ costs one cache increment and no query."""

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.now = datetime(2026, 10, 31, 2, 5, tzinfo=dt_timezone.utc)

    def _middleware(self, status):
        return staff_signals.ThrottleSignalMiddleware(lambda request: HttpResponse(status=status))

    @override_settings(ARTICLES_STAFF_ALERT_RULES_ENABLED=True)
    def test_a_429_costs_one_increment_and_no_query(self):
        staff_signals.count(staff_signals.FOUNDER_THROTTLED)  # the bucket exists, as in steady state
        request = RequestFactory().get("/api/v1/my-startup/journey/")
        with patch.object(cache, "incr", wraps=cache.incr) as incr, patch.object(cache, "add", wraps=cache.add) as add, \
                patch.object(cache, "get", wraps=cache.get) as get, self.assertNumQueries(0):
            response = self._middleware(429)(request)
        self.assertEqual(response.status_code, 429)
        self.assertEqual(incr.call_count, 1)
        self.assertEqual((add.call_count, get.call_count), (0, 0))
        self.assertEqual(staff_signals.total(staff_signals.FOUNDER_THROTTLED, window=timedelta(minutes=10)), 2)

    @override_settings(ARTICLES_STAFF_ALERT_RULES_ENABLED=True)
    def test_what_counts(self):
        middleware = staff_signals.ThrottleSignalMiddleware
        factory = RequestFactory()
        cases = (("/api/v1/my-startup/journey/", 429, staff_signals.FOUNDER_THROTTLED),
                 ("/api/v1/my-startup/journey/", 200, None),
                 ("/api/v1/community-chat/channels/", 429, None),
                 (staff_signals.REFRESH_PATH, 401, staff_signals.REFRESH_FAILED),
                 (staff_signals.REFRESH_PATH, 200, None),
                 (staff_signals.REFRESH_PATH, 503, None))
        for path, status, expected in cases:
            with self.subTest(path=path, status=status):
                self.assertEqual(staff_signals.signal_for(path, status), expected)
                with patch.object(staff_signals, "count") as count:
                    middleware(lambda request, status=status: HttpResponse(status=status))(factory.post(path))
                self.assertEqual(count.call_count, 1 if expected else 0)

    def test_nothing_is_counted_while_the_rules_are_off(self):
        request = RequestFactory().get("/api/v1/my-startup/journey/")
        with patch.object(cache, "incr") as incr, patch.object(cache, "add") as add, self.assertNumQueries(0):
            self._middleware(429)(request)
            staff_signals.count("github_callback_failed")
        self.assertEqual((incr.call_count, add.call_count), (0, 0))

    @override_settings(ARTICLES_STAFF_ALERT_RULES_ENABLED=True)
    def test_buckets_and_window_totals(self):
        for minutes in (0, 5, 12, 61):
            staff_signals.count("github_callback_failed", now=self.now - timedelta(minutes=minutes))
        self.assertEqual(staff_signals.total("github_callback_failed", window=timedelta(minutes=10), now=self.now), 2)
        self.assertEqual(staff_signals.total("github_callback_failed", window=timedelta(minutes=30), now=self.now), 3)
        self.assertEqual(staff_signals.total("github_callback_failed", window=timedelta(hours=2), now=self.now), 4)
        staff_signals.count("github_callback_failed", now=self.now, amount=3)
        self.assertEqual(staff_signals.total("github_callback_failed", window=timedelta(minutes=10), now=self.now), 5)

    @override_settings(ARTICLES_STAFF_ALERT_RULES_ENABLED=True)
    def test_a_cache_outage_never_breaks_a_request(self):
        request = RequestFactory().get("/api/v1/my-startup/journey/")
        with patch.object(cache, "incr", side_effect=ConnectionError("down")):
            with self.assertLogs("content_factory.staff_signals", "WARNING"):
                self.assertEqual(self._middleware(429)(request).status_code, 429)


class IdempotencyTests(SimpleTestCase):
    """P11 (the module): key validation, in-flight, replay, conflict and scope isolation."""

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.scope = ("profile_save", 7)
        self.body = idempotency.request_sha256({"name": "Acme"})

    def test_begin_validates_the_key(self):
        for key in ("", "a" * 129, "has space", "semi;colon", "ünicode", None, 7):
            with self.subTest(key=key), self.assertRaises(ValueError):
                idempotency.begin(self.scope, key, self.body)
        for key in ("a", "a" * 128, "0c6c4c0e-6d8f-4b0f-9f3e-1d2c3b4a5f60", "under_score"):
            with self.subTest(key=key):
                self.assertEqual(idempotency.begin(("check", key), key, self.body), ("new", None))
        with self.assertRaises(ValueError):
            idempotency.begin((), "key", self.body)

    def test_in_flight_replay_and_conflict(self):
        key = "6f1d"
        self.assertEqual(idempotency.begin(self.scope, key, self.body), ("new", None))
        self.assertEqual(idempotency.begin(self.scope, key, self.body), ("in_flight", None))
        self.assertEqual(idempotency.begin(self.scope, key, idempotency.request_sha256({"name": "Other"})),
                         ("conflict", None))
        idempotency.finish(self.scope, key, self.body, status=200, body={"saved": True})
        self.assertEqual(idempotency.begin(self.scope, key, self.body), ("replay", {"status": 200, "body": {"saved": True}}))
        self.assertEqual(idempotency.begin(self.scope, key, idempotency.request_sha256({"name": "Other"})),
                         ("conflict", None))
        # A finish with a different body never overwrites the stored response.
        idempotency.finish(self.scope, key, idempotency.request_sha256({"name": "Other"}), status=201, body={"x": 1})
        self.assertEqual(idempotency.begin(self.scope, key, self.body)[1]["status"], 200)

    def test_concurrent_begins_with_one_key(self):
        barrier = threading.Barrier(8)
        outcomes = []
        lock = threading.Lock()

        def worker():
            barrier.wait()
            outcome = idempotency.begin(self.scope, "same-key", self.body)[0]
            with lock:
                outcomes.append(outcome)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sorted(outcomes), ["in_flight"] * 7 + ["new"])

    def test_the_same_key_under_two_scopes_never_collides(self):
        scopes = (("profile_save", 7), ("profile_save", 8), ("staff_action", 7, 1), ("publish_reviewed", "run-1", 7))
        keys = {idempotency.cache_key(scope, "k") for scope in scopes}
        self.assertEqual(len(keys), len(scopes))
        self.assertTrue(all(key.startswith("founder_tools:idem:") and len(key) == len("founder_tools:idem:") + 64
                            for key in keys))
        for scope in scopes:
            self.assertEqual(idempotency.begin(scope, "k", self.body), ("new", None))
        idempotency.finish(scopes[0], "k", self.body, status=200, body={"a": 1})
        self.assertEqual(idempotency.begin(scopes[1], "k", self.body), ("in_flight", None))
        self.assertEqual(idempotency.begin(scopes[0], "k", self.body)[0], "replay")

    def test_abandon_releases_an_in_flight_claim(self):
        self.assertEqual(idempotency.begin(self.scope, "retry-me", self.body), ("new", None))
        idempotency.abandon(self.scope, "retry-me", self.body)
        self.assertEqual(idempotency.begin(self.scope, "retry-me", self.body), ("new", None))

    def test_conflict_copy(self):
        self.assertEqual(idempotency.CONFLICT_DETAIL, "Couldn't save just now. Trying again…")
        self.assertEqual(idempotency.CONFLICT_REASON, "operation_key_conflict")
        self.assertEqual(idempotency.TTL_SECONDS, 24 * 60 * 60)
