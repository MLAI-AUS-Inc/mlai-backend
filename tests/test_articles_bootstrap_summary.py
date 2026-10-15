"""Database/network-free F2 read-contract and real serialization benchmarks."""
from datetime import datetime, timezone
import json
import time
from types import SimpleNamespace
from unittest.mock import patch

import django
from django.apps import apps
from django.test import SimpleTestCase
from rest_framework.renderers import JSONRenderer

if not apps.ready:
    django.setup()

from content_factory import vibe_marketing_views as views


class BootstrapSummaryTests(SimpleTestCase):
    def test_both_bootstrap_views_skip_github_reconciliation(self):
        class ProjectionBoundary(Exception):
            pass
        context = SimpleNamespace(organization=SimpleNamespace(id=1))
        for view in ("summary", "full"):
            with self.subTest(view=view), patch.object(views, "_latest_runs_for_org", return_value=[]), patch.object(
                views, "_recent_discovery_topic_runs_for_org", side_effect=ProjectionBoundary
            ), patch.object(views, "_refresh_pending_article_system_setup_pr_status", return_value=(None, False)) as setup, patch.object(
                views, "refresh_publish_statuses"
            ) as published:
                with self.assertRaises(ProjectionBoundary):
                    views._compute_bootstrap_payload(context, view=view, config=SimpleNamespace())
                setup.assert_not_called()
                published.assert_not_called()

    def test_internal_bootstrap_callers_also_default_to_summary(self):
        context = SimpleNamespace(organization=SimpleNamespace(id=1))
        with patch.object(views, "_get_config", return_value=SimpleNamespace()), patch.object(
            views, "_bootstrap_cache_seconds", return_value=0
        ), patch.object(views, "_compute_bootstrap_payload", return_value={}) as compute, patch.object(
            views, "_overlay_live_bootstrap_fields", side_effect=lambda payload, **kwargs: payload
        ):
            views._serialize_bootstrap(context)
            self.assertEqual(compute.call_args.kwargs["view"], "summary")
            views._serialize_bootstrap(context, view="full")
            self.assertEqual(compute.call_args.kwargs["view"], "full")

    def test_bootstrap_get_defaults_to_summary_and_keeps_explicit_full(self):
        context = SimpleNamespace()
        company = SimpleNamespace(domain="example.test")
        for query, expected in (("", "summary"), ("?view=summary", "summary"),
                                ("?view=full", "full"), ("?view=unknown", "summary")):
            with self.subTest(query=query), patch.object(
                views, "_resolve_profile_company_or_response", return_value=(None, company, None)
            ), patch.object(views, "_resolve_context_or_response", return_value=(context, None)), patch.object(
                views, "_serialize_bootstrap", return_value={"latestRuns": []}
            ) as serialize:
                request = SimpleNamespace(query_params={"view": query.partition("=")[2]} if query else {})
                response = views.VibeMarketingBootstrapView().get(request)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(serialize.call_args.kwargs["view"], expected)
                self.assertEqual(response["X-MLAI-View"], expected)

    def test_negotiated_response_is_encoded_once_and_reports_actual_bytes(self):
        class CountingRenderer(JSONRenderer):
            calls = 0
            def render(self, *args, **kwargs):
                type(self).calls += 1
                return super().render(*args, **kwargs)

        renderer = CountingRenderer()
        with patch("rest_framework.renderers.JSONRenderer", CountingRenderer):
            response = views._timed_vibe_response({"title": "Café", "empty": []}, started_at=time.perf_counter(),
                metric_name="vibe_bootstrap", view="summary")
        self.assertEqual(renderer.calls, 0)
        response.accepted_renderer = renderer
        response.accepted_media_type = "application/json; indent=2"
        response.renderer_context = {}
        response.render()
        self.assertEqual(response.rendered_content, response.content)
        response.render()
        self.assertEqual(renderer.calls, 1)
        self.assertEqual(int(response["X-MLAI-Payload-Bytes"]), len(response.content))
        self.assertEqual(json.loads(response.content), {"title": "Café", "empty": []})
        self.assertIn(b"\n", response.content)
        self.assertIn("vibe_bootstrap;dur=", response["Server-Timing"])

    def test_twenty_four_large_runs_use_the_real_compact_serializer(self):
        now = datetime(2026, 10, 16, tzinfo=timezone.utc)
        html = "<article>" + "x" * (2_000_000 - len("<article></article>")) + "</article>"
        raw = {"review_draft_html": html, "review_draft_actions_available": True,
            "diagnostics": {"logs": "private diagnostic " * 10_000},
            "component_manifest": {"components": [{"body": "component " * 10_000}]},
            "article_body": "article text " * 10_000}
        runs = [SimpleNamespace(workflow="article", run_id=f"article-{index}", domain="example.test", github_repo="",
            status="completed", current_step="", approval_state="", resume_available=False, created_at=now, updated_at=now,
            step_order=[], steps=SimpleNamespace(order_by=lambda *_: [], all=lambda: []), acceptance_summary={}, result=raw,
            run_request={}, error="", verification_summary={}) for index in range(24)]
        def payload(mode):
            with patch.object(views, "_workflow_progress", return_value={}), patch.object(
                views, "_article_restart_available", return_value=False
            ), patch.object(views, "_component_feedback_from_run", return_value={}
            ):
                return {"latestRuns": [views._serialize_run(run, mode=mode,
                    precomputed_article_setup_state={}) for run in runs]}
        started = time.perf_counter()
        full = JSONRenderer().render(payload("full"))
        full_ms = (time.perf_counter() - started) * 1000
        started = time.perf_counter()
        summary_payload = payload("summary")
        summary = JSONRenderer().render(summary_payload)
        summary_ms = (time.perf_counter() - started) * 1000
        self.assertGreater(len(full), 40_000_000)
        self.assertLess(len(summary), 300_000)
        self.assertLess(len(summary), len(full) / 100)
        self.assertNotIn("reviewDraftHtml", summary_payload["latestRuns"][0])
        self.assertTrue(summary_payload["latestRuns"][0]["reviewDraftActionsAvailable"])
        self.assertNotIn("article_body", summary_payload["latestRuns"][0]["result"])
        print(json.dumps({"fixture": "24 large article runs", "full_bytes": len(full), "summary_bytes": len(summary),
            "full_serialize_ms": round(full_ms, 2), "summary_serialize_ms": round(summary_ms, 2)}))
