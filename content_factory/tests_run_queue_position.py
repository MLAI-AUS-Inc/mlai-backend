"""P0.13: interactive queue class on founder starts, queue position on queued runs (IF-17)."""
from datetime import datetime, timedelta, timezone as datetime_timezone
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import SimpleTestCase, TestCase, override_settings

from content_factory.billing import build_roo_points_authorization_payload
from content_factory.models import ContentIslandRefreshDispatch, OrganizationContentConfig, ResearchedKeyword
from content_factory.run_queue_position import (
    INTERACTIVE_QUEUE_CLASS,
    interactive_queue_class,
    queue_position_payload,
    with_interactive_queue_class,
)
from organizations.models import Organization
from workflow_runs.models import ContentFactoryRun, ContentFactoryRunStep

NOW = datetime(2026, 10, 29, 3, 0, tzinfo=datetime_timezone.utc)


def _gate(action, *, billing_status, **extra):
    payload = build_roo_points_authorization_payload(
        domain="founder.example.com",
        action=action,
        cost_points=0,
        required_points=0,
        current_balance=0,
        billing_status=billing_status,
        ledger_id=extra.pop("ledger_id", None),
    )
    payload.update(extra)
    return payload


def _entitled(action):
    return _gate(action, billing_status="entitled", ledger_id=41, roo_points_entitlement="first_article")


class InteractiveQueueClassTests(SimpleTestCase):
    def test_startup_autofill_is_interactive(self):
        payload = {"domain": "founder.example.com", "request_source": "ai_solopreneur",
                   **_gate("startup_autofill", billing_status="free")}
        self.assertEqual(interactive_queue_class(payload), INTERACTIVE_QUEUE_CLASS)

    def test_the_entitled_first_topic_batch_is_interactive_for_both_gate_actions(self):
        for action in ("content_island_topic_generation", "topic_discovery"):
            with self.subTest(action=action):
                payload = {"workflow": "auto_discovery", "requested_topic_count": 4, **_entitled(action)}
                self.assertEqual(interactive_queue_class(payload), INTERACTIVE_QUEUE_CLASS)

    def test_the_entitled_first_article_is_interactive(self):
        payload = {"delivery_mode": "content_only", "delivery_mode_confirmed": True,
                   **_entitled("article_generation")}
        self.assertEqual(interactive_queue_class(payload), INTERACTIVE_QUEUE_CLASS)

    def test_paid_or_gated_work_keeps_the_default_queue(self):
        cases = {
            "charged article": _gate("article_generation", billing_status="charged", ledger_id=7),
            "reused article": _gate("article_generation", billing_status="reused", ledger_id=7),
            "charged topic batch": _gate("content_island_topic_generation", billing_status="charged", ledger_id=8),
            "gated topic discovery": _gate("topic_discovery", billing_status="gated"),
            "gated repo scan": _gate("repo_scan", billing_status="gated"),
            "free website baseline": _gate("website_baseline", billing_status="free"),
            "entitled without the first-article marker": _gate("article_generation", billing_status="entitled", ledger_id=9),
            "first-article marker on an unrelated action": _gate(
                "repo_scan", billing_status="entitled", ledger_id=9, roo_points_entitlement="first_article"),
            "no billing at all": {"domain": "founder.example.com"},
        }
        for label, payload in cases.items():
            with self.subTest(label):
                self.assertIsNone(interactive_queue_class(payload))

    def test_island_refresh_payloads_never_get_a_class(self):
        scheduler = {"domain": "founder.example.com", "client_request_id": "island-refresh:founder.example.com:2026-10-29",
                     "request_source": "mlai_backend_scheduler", "include_expansion": True}
        research = {"workflow": "island_refresh", "island_research_brief": {"topic": "x"}, **_entitled("content_island_topic_generation")}
        for payload in (scheduler, {**scheduler, **_gate("startup_autofill", billing_status="free")}, research):
            with self.subTest(payload=payload):
                self.assertIsNone(interactive_queue_class(payload))
                self.assertNotIn("queue_class", with_interactive_queue_class(payload))

    def test_non_mapping_payloads_have_no_class(self):
        for value in (None, [], "startup_autofill"):
            with self.subTest(value=value):
                self.assertIsNone(interactive_queue_class(value))
                self.assertIs(with_interactive_queue_class(value), value)

    def test_the_post_body_gains_the_class_without_mutating_the_saved_request(self):
        payload = {"domain": "founder.example.com", **_gate("startup_autofill", billing_status="free")}
        body = with_interactive_queue_class(payload)
        self.assertEqual(body["queue_class"], "interactive")
        self.assertNotIn("queue_class", payload)
        default = {"domain": "founder.example.com", **_gate("article_generation", billing_status="charged", ledger_id=3)}
        self.assertIs(with_interactive_queue_class(default), default)


class _Response(SimpleNamespace):
    text = ""

    def json(self):
        return self.payload


class QueueClassCallSiteTests(TestCase):
    """The class is attached at the two CF POST helpers, never on island refresh."""

    def test_the_queue_helper_posts_the_class_for_autofill(self):
        from integrations.services.article_generation import _post_content_factory_queue_request

        payload = {"domain": "founder.example.com", **_gate("startup_autofill", billing_status="free")}
        with patch("integrations.services.article_generation.http_requests.post",
                   return_value=_Response(status_code=202, payload={"run_id": "r1"})) as post:
            _post_content_factory_queue_request("https://cf.example.test/api/runs/autofill", payload=payload,
                                                headers={}, operation="queue_autofill", domain="founder.example.com")
        self.assertEqual(post.call_args.kwargs["json"]["queue_class"], "interactive")
        self.assertNotIn("queue_class", payload)

    def test_the_queue_helper_leaves_paid_articles_on_the_default_queue(self):
        from integrations.services.article_generation import _post_content_factory_queue_request

        payload = {"domain": "founder.example.com", "delivery_mode": "content_only",
                   **_gate("article_generation", billing_status="charged", ledger_id=5)}
        with patch("integrations.services.article_generation.http_requests.post",
                   return_value=_Response(status_code=202, payload={"run_id": "r2"})) as post:
            _post_content_factory_queue_request("https://cf.example.test/api/runs/discovery", payload=payload,
                                                headers={}, operation="queue_discovery", domain="founder.example.com")
        self.assertNotIn("queue_class", post.call_args.kwargs["json"])

    @override_settings(CONTENT_FACTORY_URL="https://cf.example.test")
    def test_the_nightly_island_refresh_body_never_carries_a_class(self):
        from content_factory.services.island_refresh_scheduler import dispatch_island_refresh

        organization = Organization.objects.create(name="Founder", domain="founder.example.com")
        OrganizationContentConfig.objects.create(organization=organization)
        ResearchedKeyword.objects.create(organization=organization, keyword="ai grants", keyword_normalized="ai grants")
        with patch("integrations.services.article_generation.http_requests.post",
                   return_value=_Response(status_code=202, payload={"run_id": "island-1"})) as post:
            result = dispatch_island_refresh(organization, local_date=NOW.date())
        self.assertEqual(result["status"], "dispatched")
        self.assertEqual(set(post.call_args.kwargs["json"]),
                         {"domain", "client_request_id", "request_source", "include_expansion"})
        self.assertTrue(ContentIslandRefreshDispatch.objects.filter(organization=organization).exists())

    def test_the_outbox_retry_posts_the_class_for_an_entitled_first_article(self):
        from content_factory import dispatch_outbox

        request = {"domain": "founder.example.com", "client_request_id": "k1", "delivery_mode": "content_only",
                   "delivery_mode_confirmed": True, "roo_points_requested_by_user_id": 1,
                   **_entitled("article_generation")}
        row = SimpleNamespace(client_request_id="k1", organization=SimpleNamespace(domain="founder.example.com"),
                              payload={"request": request, "endpoint": "article", "workflow": "article_generation"})
        remote = {"enabled": True, "base_url": "https://cf.example.test"}
        with patch("content_factory.vibe_marketing_views._content_factory_remote_config", return_value=remote), \
                patch("content_factory.vibe_marketing_views._content_factory_headers", return_value={}), \
                patch("content_factory.vibe_marketing_views._lookup_content_factory_dispatch_by_key",
                      return_value=("not_received", {})), \
                patch("content_factory.portable_drafts.original_portable_run", return_value=True), \
                patch("content_factory.vibe_marketing_views._refresh_article_editorial_payload", return_value=None), \
                patch("integrations.services.article_generation.require_article_activation"), \
                patch("django.contrib.auth.get_user_model") as users, \
                patch("integrations.http_client.post",
                      return_value=_Response(status_code=409, payload={"code": "dispatch_rejected"})) as post:
            users.return_value.objects.get.return_value = SimpleNamespace(pk=1)
            state, _code, _run_id = dispatch_outbox._deliver(row)
        self.assertEqual(state, "failed")
        self.assertEqual(post.call_args.kwargs["json"]["queue_class"], "interactive")
        self.assertNotIn("queue_class", row.payload["request"])


class _WebResponse(_Response):
    @property
    def content(self):
        return b"{}"


@override_settings(CONTENT_FACTORY_URL="https://cf.example.test", CONTENT_FACTORY_API_KEY="secret-key", IS_LOCAL_ENV=False)
class WebStartQueueClassTests(TestCase):
    """Founder web starts POST through _queue_content_factory_run_authorized, so the class is attached there too."""

    def setUp(self):
        self.user = get_user_model().objects.create_user(email="queue-class@example.com", password="password")
        self.organization = Organization.objects.create(name="Founder", domain="founder.example.com")
        self.config = OrganizationContentConfig.objects.create(organization=self.organization)
        self.context = SimpleNamespace(profile=SimpleNamespace(user=self.user), organization=self.organization)

    def _post_body(self, *, endpoint, workflow, payload):
        from content_factory import vibe_marketing_views as views

        with patch("content_factory.vibe_marketing_views.http_client.post",
                   return_value=_WebResponse(status_code=202, payload={"run_id": f"cf-{endpoint}-1", "status": "queued"})) as post, \
                patch("content_factory.vibe_marketing_views._refresh_article_editorial_payload", return_value=None):
            views._queue_content_factory_run(endpoint=endpoint, workflow=workflow, context=self.context,
                                             config=self.config, payload=payload)
        post.assert_called_once()
        return post.call_args.kwargs["json"]

    def test_a_web_autofill_start_asks_for_the_interactive_queue(self):
        payload = {"domain": "founder.example.com", "request_source": "ai_solopreneur", "persist": False,
                   **_gate("startup_autofill", billing_status="free")}
        body = self._post_body(endpoint="autofill", workflow="startup_autofill", payload=payload)
        self.assertEqual(body["queue_class"], "interactive")
        self.assertNotIn("queue_class", payload)

    def test_an_entitled_web_first_article_asks_for_the_interactive_queue(self):
        payload = {"domain": "founder.example.com", "target_keyword": "ai grants", "delivery_mode": "content_only",
                   **_entitled("article_generation")}
        body = self._post_body(endpoint="article", workflow="article_generation", payload=payload)
        self.assertEqual(body["queue_class"], "interactive")

    def test_an_entitled_web_first_topic_batch_asks_for_the_interactive_queue(self):
        payload = {"domain": "founder.example.com", "requested_topic_count": 4,
                   **_entitled("content_island_topic_generation")}
        body = self._post_body(endpoint="discovery", workflow="auto_discovery", payload=payload)
        self.assertEqual(body["queue_class"], "interactive")

    def test_a_web_article_outside_the_entitlement_keeps_the_default_queue(self):
        payload = {"domain": "founder.example.com", "target_keyword": "ai grants", "delivery_mode": "content_only",
                   **_gate("article_generation", billing_status="gated")}
        body = self._post_body(endpoint="article", workflow="article_generation", payload=payload)
        self.assertNotIn("queue_class", body)

    def test_paid_island_research_never_gets_a_class(self):
        payload = {"domain": "founder.example.com", "island_research_brief": {"topic": "grants"},
                   "requested_topic_count": 1, **_entitled("content_island_topic_generation")}
        body = self._post_body(endpoint="island-research", workflow="island_refresh", payload=payload)
        self.assertNotIn("queue_class", body)


def _queued(run_id, *, queue_name="interactive", queued_at, status="queued", updated_at=None):
    run = ContentFactoryRun.objects.create(
        run_id=run_id, workflow="startup_autofill", domain="founder.example.com", status=status,
        result={"queue_name": queue_name, "queued_at": queued_at.isoformat(), "queue_class": "interactive"},
    )
    ContentFactoryRun.objects.filter(pk=run.pk).update(updated_at=updated_at or queued_at)
    run.refresh_from_db()
    return run


def _completed(run_id, *, seconds, queue_name="interactive", finished=NOW - timedelta(hours=1)):
    run = ContentFactoryRun.objects.create(run_id=run_id, workflow="startup_autofill", domain="done.example.com",
                                           status="completed", result={"queue_name": queue_name})
    ContentFactoryRunStep.objects.create(run=run, step_key="research", status="completed",
                                         started_at=finished - timedelta(seconds=seconds), completed_at=finished)
    ContentFactoryRun.objects.filter(pk=run.pk).update(updated_at=finished)
    return run


class QueuePositionTests(TestCase):
    def setUp(self):
        # The ETA median is cached per queue; start every test from an empty cache.
        cache.clear()
        self.addCleanup(cache.clear)

    def test_position_counts_earlier_runs_on_the_same_queue_within_two_hours(self):
        me = _queued("me", queued_at=NOW - timedelta(minutes=10))
        _queued("ahead-1", queued_at=NOW - timedelta(minutes=30))
        _queued("ahead-2", queued_at=NOW - timedelta(minutes=90))
        _queued("too-old", queued_at=NOW - timedelta(hours=3), updated_at=NOW - timedelta(minutes=5))
        _queued("behind", queued_at=NOW - timedelta(minutes=5))
        _queued("other-queue", queue_name="default", queued_at=NOW - timedelta(minutes=40))
        _queued("already-running", status="running", queued_at=NOW - timedelta(minutes=45))

        payload = queue_position_payload(me, now=NOW)

        self.assertEqual(payload["queuePosition"], 3)
        self.assertEqual(payload["queueAhead"], 2)
        self.assertIsNone(payload["queueEtaSeconds"])
        self.assertEqual(payload["queueObservedAt"], NOW.isoformat())

    def test_the_first_run_in_line_is_position_one(self):
        me = _queued("me", queued_at=NOW - timedelta(minutes=1))
        payload = queue_position_payload(me, now=NOW)
        self.assertEqual((payload["queuePosition"], payload["queueAhead"]), (1, 0))

    def test_the_fields_are_absent_when_the_run_is_not_queued(self):
        for status in ("running", "completed", "failed", "awaiting_confirmation"):
            with self.subTest(status=status):
                run = _queued(f"run-{status}", status=status, queued_at=NOW - timedelta(minutes=5))
                self.assertEqual(queue_position_payload(run, now=NOW), {})

    def test_the_fields_are_absent_without_mirrored_queue_metadata(self):
        for result in ({}, {"queue_name": "interactive"}, {"queued_at": NOW.isoformat()},
                       {"queue_name": "interactive", "queued_at": "not a time"}):
            with self.subTest(result=result):
                run = SimpleNamespace(run_id="x", status="queued", result=result)
                self.assertEqual(queue_position_payload(run, now=NOW), {})

    @override_settings(CONTENT_FACTORY_INTERACTIVE_QUEUE_SLOTS=0)
    def test_the_eta_is_null_when_slots_are_not_configured(self):
        for index in range(6):
            _completed(f"done-{index}", seconds=300)
        _queued("ahead", queued_at=NOW - timedelta(minutes=20))
        me = _queued("me", queued_at=NOW - timedelta(minutes=10))
        self.assertIsNone(queue_position_payload(me, now=NOW)["queueEtaSeconds"])

    @override_settings(CONTENT_FACTORY_INTERACTIVE_QUEUE_SLOTS=2)
    def test_the_eta_is_null_with_fewer_than_five_samples(self):
        for index in range(4):
            _completed(f"done-{index}", seconds=300)
        _queued("ahead", queued_at=NOW - timedelta(minutes=20))
        me = _queued("me", queued_at=NOW - timedelta(minutes=10))
        self.assertIsNone(queue_position_payload(me, now=NOW)["queueEtaSeconds"])

    @override_settings(CONTENT_FACTORY_INTERACTIVE_QUEUE_SLOTS=2)
    def test_the_eta_is_waves_ahead_times_the_median_run_time(self):
        for index, seconds in enumerate((100, 200, 300, 400, 900)):
            _completed(f"done-{index}", seconds=seconds)
        _completed("other-queue-sample", seconds=5000, queue_name="default")
        for index in range(3):
            _queued(f"ahead-{index}", queued_at=NOW - timedelta(minutes=30 - index))
        me = _queued("me", queued_at=NOW - timedelta(minutes=10))

        payload = queue_position_payload(me, now=NOW)

        # 3 ahead on 2 slots is 2 waves; the median sample is 300 s.
        self.assertEqual(payload["queueAhead"], 3)
        self.assertEqual(payload["queueEtaSeconds"], 600)

    @override_settings(CONTENT_FACTORY_INTERACTIVE_QUEUE_SLOTS=2)
    def test_the_front_of_the_line_has_no_eta_and_reads_no_median(self):
        for index in range(6):
            _completed(f"done-{index}", seconds=300)
        me = _queued("me", queued_at=NOW - timedelta(minutes=1))
        with patch("content_factory.run_queue_position._median_run_seconds") as median:
            payload = queue_position_payload(me, now=NOW)
        median.assert_not_called()
        self.assertEqual((payload["queuePosition"], payload["queueAhead"]), (1, 0))
        self.assertIsNone(payload["queueEtaSeconds"])

    @override_settings(CONTENT_FACTORY_INTERACTIVE_QUEUE_SLOTS=2)
    def test_the_median_is_cached_per_queue_between_polls(self):
        _queued("ahead", queued_at=NOW - timedelta(minutes=20))
        me = _queued("me", queued_at=NOW - timedelta(minutes=10))
        with patch("content_factory.run_queue_position._median_run_seconds", return_value=240.0) as median:
            first = queue_position_payload(me, now=NOW)
            second = queue_position_payload(me, now=NOW)
        median.assert_called_once()
        self.assertEqual(first["queueEtaSeconds"], 240)
        self.assertEqual(second["queueEtaSeconds"], 240)

    @override_settings(CONTENT_FACTORY_INTERACTIVE_QUEUE_SLOTS=2)
    def test_too_few_samples_is_cached_too(self):
        _queued("ahead", queued_at=NOW - timedelta(minutes=20))
        me = _queued("me", queued_at=NOW - timedelta(minutes=10))
        with patch("content_factory.run_queue_position._median_run_seconds", return_value=None) as median:
            queue_position_payload(me, now=NOW)
            payload = queue_position_payload(me, now=NOW)
        median.assert_called_once()
        self.assertIsNone(payload["queueEtaSeconds"])

    @override_settings(CONTENT_FACTORY_INTERACTIVE_QUEUE_SLOTS=2)
    def test_a_cache_outage_still_gives_an_eta(self):
        _queued("ahead", queued_at=NOW - timedelta(minutes=20))
        me = _queued("me", queued_at=NOW - timedelta(minutes=10))
        with patch("content_factory.run_queue_position.cache.get", side_effect=RuntimeError("redis down")), \
                patch("content_factory.run_queue_position._median_run_seconds", return_value=120.0):
            payload = queue_position_payload(me, now=NOW)
        self.assertEqual(payload["queueEtaSeconds"], 120)

    def test_a_cf_mirrored_queued_snapshot_yields_a_position(self):
        """Feed the helper the shape content-factory mirrors while a busy queue holds the run."""
        from content_factory.service_views import _apply_run_snapshot

        _queued("ahead", queued_at=NOW - timedelta(minutes=4))
        queued_at = (NOW - timedelta(minutes=2)).isoformat().replace("+00:00", "Z")
        response = _apply_run_snapshot("cf-mirrored-autofill", {
            "workflow": "startup_autofill",
            "status": "queued",
            "current_step": "",
            "step_order": ["research"],
            "approval_state": "not_required",
            "resume_available": False,
            "run_request": {"domain": "founder.example.com", "request_source": "ai_solopreneur",
                            "queue_class": "interactive"},
            "result": {"status": "queued", "queue_name": "interactive", "queued_at": queued_at,
                       "queue_class": "interactive"},
            "step_states": {},
        })
        self.assertIn(response.status_code, {200, 201})
        run = ContentFactoryRun.objects.get(run_id="cf-mirrored-autofill")

        payload = queue_position_payload(run, now=NOW)

        self.assertEqual(run.status, "queued")
        self.assertEqual((payload["queuePosition"], payload["queueAhead"]), (2, 1))

    def test_serialized_runs_expose_the_position_only_while_queued(self):
        from content_factory.vibe_marketing_views import _serialize_run

        _queued("ahead", queued_at=NOW - timedelta(minutes=8))
        me = _queued("me", queued_at=NOW - timedelta(minutes=4))
        for mode in ("status", "full"):
            with self.subTest(mode=mode), patch("content_factory.run_queue_position.timezone.now", return_value=NOW):
                payload = _serialize_run(me, mode=mode, include_workflow_progress=False)
                self.assertEqual(payload["queuePosition"], 2)
                self.assertEqual(payload["queueAhead"], 1)
                self.assertIn("queueEtaSeconds", payload)
                self.assertEqual(payload["queueName"], "interactive")

        ContentFactoryRun.objects.filter(pk=me.pk).update(status="running")
        me.refresh_from_db()
        payload = _serialize_run(me, mode="status", include_workflow_progress=False)
        for key in ("queuePosition", "queueAhead", "queueEtaSeconds", "queueObservedAt"):
            self.assertNotIn(key, payload)
