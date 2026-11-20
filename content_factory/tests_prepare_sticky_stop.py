"""Sticky stops on real rows (plan B3): a push that cannot change a stop dispatches no scan from either path.

Pawshare (13 Nov 2026): reverify 09:53, scan pk 5240 09:54, setup pk 5241
failing 10:01, on every push. Here the push leaves the stopped preparation
stopped, settles the push's paid source rescan without a scan, and the
source-reverify worker holds the scan too if it runs before the wake. A push
under the articles route, and an owned setup merge, still re-open.
"""
from datetime import timedelta
from unittest.mock import patch
from uuid import uuid4

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone

from workflow_runs.models import ContentFactoryRun

from . import founder_journey_alerts as alerts
from . import website_live_status as live_status
from . import website_prepare as prepare
from . import website_prepare_sticky_stop as sticky
from . import website_reconciliation as reconciliation
from . import website_source_carry as carry
from .founder_journey_testing import make_history_free_org
from .tests_live_url_verification import MERGED, LiveUrlFixture, _connect, _next_js_target
from .website_models import WebsiteConnectionOperation

PUSHED = "e" * 40
SETUP = {
    "registry_path": "src/content/articles/registry.ts",
    "route_shell_path": "src/app/blog/page.tsx",
    "detail_route_shell_path": "src/app/blog/[slug]/page.tsx",
    "managed_files": ["src/lib/articles.ts"],
    "content_path": "src/content/articles",
}
CODE = "DIRECTORY_DEPENDENCY_VALIDATION_FAILED"


def _compare(*files):
    return {"status": "ahead", "total_commits": 1, "files": [{"filename": name, "status": "modified"} for name in files]}


def _no_scan(*args, **kwargs):
    raise AssertionError("A paid repository scan was dispatched.")


@override_settings(ARTICLES_READY_ON_MERGE_ENABLED=True, ARTICLES_PROOF_CARRY_FORWARD_ENABLED=True,
                   ARTICLES_PREPARE_STICKY_STOP_ENABLED=True, ARTICLES_FOUNDER_STUCK_ALERTS_ENABLED=True)
class StickyStopPushTests(LiveUrlFixture, TestCase):
    def setUp(self):
        super().setUp()
        cache.delete(live_status.THROTTLE_KEY)
        self.built = make_history_free_org(domain="sticky.example.test")
        self.connection = _connect(self.built, verified_sha=MERGED)
        self.connection.authorized_by = self.built.owner
        self.connection.capabilities = {**self.connection.capabilities, "publishingReady": True, "generationReady": True}
        self.connection.save(update_fields=["authorized_by", "capabilities"])
        self.target = _next_js_target(self.connection)
        self.config = self.built.config
        self.config.refresh_from_db()
        self.config.article_system_setup_cache = SETUP
        self.config.save(update_fields=["article_system_setup_cache"])
        owned = patch("content_factory.website_reconciliation._find_owned_merge", return_value=None)
        owned.start()
        self.addCleanup(owned.stop)
        # Paid scans must never start in these tests unless one is expected.
        remote = patch("content_factory.vibe_marketing_views._content_factory_remote_config", side_effect=_no_scan)
        self.remote = remote.start()
        self.addCleanup(remote.stop)
        self.op = WebsiteConnectionOperation.objects.create(
            connection=self.connection, generation=self.connection.generation, action="prepare",
            idempotency_key=f"{self.connection.pk}:prepare:{self.connection.generation}", state="needs_attention",
            payload={"company_id": str(self.built.company.pk), "requested_by_user_id": str(self.built.owner.pk), "attempt": 2},
            receipt={"status": "needs_attention", "reason_code": CODE, "failure_class": "repairable", "retryable": False,
                     "current_step": "setup", "message": "We stopped after 1 attempt: the listing does not read the registry."})
        self.op.receipt[sticky.STOP_KEY] = {"version": 1, "reason_code": CODE, "failure_class": "repairable",
            "source_sha": MERGED, "relevant_paths": carry.setup_paths(SETUP), "relevant_routes": ["blog"],
            "attempt": 2, "held": False}
        WebsiteConnectionOperation.objects.filter(pk=self.op.pk).update(receipt=self.op.receipt)

    def push(self, comparison, *, wake=True, sealed=True):
        # A target needing sealed CI proof is never carried, so the push queues a paid rescan.
        stack = [patch.object(carry, "compare_sources", return_value=comparison),
                 patch("content_factory.website_sealed_workflow.required", return_value=sealed)]
        if not wake:
            stack.append(patch("content_factory.website_prepare.wake_prepare_for_source"))
        for item in stack:
            item.start()
        try:
            return reconciliation.handle_website_github_event("push", {
                "repository": {"id": 777}, "ref": "refs/heads/main", "after": PUSHED})
        finally:
            for item in reversed(stack):
                item.stop()

    def recheck(self):
        return WebsiteConnectionOperation.objects.get(connection=self.connection, action="source-reverify",
                                                      payload__source_sha=PUSHED)

    def scans(self):
        return ContentFactoryRun.objects.filter(organization=self.built.organization, workflow="repo_scan").count()

    def test_a_push_with_no_relevant_change_dispatches_no_scan_from_either_path(self):
        self.push(_compare("README.md", "src/components/Hero.tsx"))
        self.op.refresh_from_db()
        self.assertEqual(self.op.state, "needs_attention")
        self.assertEqual(self.op.receipt["source_changed_since_stop"]["source_sha"], PUSHED)
        self.assertFalse(self.op.receipt["source_changed_since_stop"]["reopen"])
        check = self.recheck()
        self.assertTrue(check.payload["scan_required"])
        self.assertEqual((check.state, check.receipt["status"]), ("completed", "held_by_stopped_prepare"))
        self.assertIsNone(reconciliation._process_source_reverification(check.pk, timezone.now()))
        self.assertEqual(self.scans(), 0)
        self.remote.assert_not_called()

    def test_the_source_check_holds_its_scan_when_it_runs_before_the_wake(self):
        self.push(_compare("README.md"), wake=False)
        check = self.recheck()
        self.assertEqual(check.state, "pending")
        with patch.object(carry, "compare_sources", return_value=_compare("README.md")):
            state = reconciliation._process_source_reverification(check.pk, timezone.now())
        check.refresh_from_db()
        self.assertEqual(state, "completed", check.receipt)
        self.assertEqual(check.receipt["status"], "held_by_stopped_prepare")
        self.assertTrue(check.receipt["scan_skipped"])
        self.op.refresh_from_db()
        self.assertEqual(self.op.state, "needs_attention")
        self.assertEqual(self.scans(), 0)
        self.remote.assert_not_called()

    def test_a_push_under_the_articles_route_reopens(self):
        with patch("content_factory.website_reconciliation._renews_by_live_url", return_value=False):
            self.push(_compare("src/app/blog/page.tsx"))
        self.op.refresh_from_db()
        self.assertEqual((self.op.state, self.op.receipt["current_step"]), ("pending", "scan"))
        self.assertTrue(self.op.receipt["source_changed_since_stop"]["reopen"])
        # The re-opened preparation owns the push's check: one scan, not two.
        self.assertEqual(self.recheck().receipt["status"], "managed_by_prepare")

    def test_an_owned_setup_merge_still_reopens(self):
        run = ContentFactoryRun.objects.create(run_id=f"setup-{uuid4().hex[:8]}", workflow="article_system_setup",
            organization=self.built.organization, status="completed",
            run_request={"website_connection_id": str(self.connection.pk), "connection_generation": self.connection.generation})
        WebsiteConnectionOperation.objects.filter(pk=self.op.pk).update(state="cancelled")
        with patch("content_factory.website_reconciliation._renews_by_live_url", return_value=False), \
                patch("content_factory.website_source_carry.compare_sources", side_effect=AssertionError("no compare")):
            reopened = prepare.wake_prepare_for_source(self.connection.pk, organization_id=self.built.organization.pk,
                                                       owned_merge_run_id=run.run_id, source_sha=PUSHED)
        self.assertIsNotNone(reopened)
        self.op.refresh_from_db()
        self.assertEqual((self.op.state, self.op.receipt["merged_setup_run_id"]), ("pending", run.run_id))

    def test_a_finished_preparation_on_a_carried_push_is_not_reopened(self):
        WebsiteConnectionOperation.objects.filter(pk=self.op.pk).update(state="completed")
        with patch("content_factory.website_reconciliation._renews_by_live_url", return_value=False):
            result = self.push(_compare("README.md"), sealed=False)
        self.assertEqual(result["carried"], 1)
        self.op.refresh_from_db()
        self.assertEqual(self.op.state, "completed")
        check = self.recheck()
        self.assertFalse(check.payload["scan_required"])
        self.assertEqual(check.state, "pending")

    def test_the_kill_switch_restores_the_reopen(self):
        with override_settings(ARTICLES_PREPARE_STICKY_STOP_ENABLED=False), \
                patch("content_factory.website_reconciliation._renews_by_live_url", return_value=False):
            self.push(_compare("README.md"))
        self.op.refresh_from_db()
        self.assertEqual(self.op.state, "pending")

    def test_the_daily_scan_budget_settles_a_paid_recheck_with_one_alert(self):
        WebsiteConnectionOperation.objects.filter(pk=self.op.pk).update(state="completed")
        for index in range(sticky.DAILY_SCAN_LIMIT):
            ContentFactoryRun.objects.create(run_id=f"scan-{index}-{uuid4().hex[:6]}", workflow="repo_scan",
                                             organization=self.built.organization, status="completed")
        # The source check runs on its own here (a finished preparation, no wake).
        self.push(_compare("src/app/blog/page.tsx"), wake=False)
        check = self.recheck()
        self.assertTrue(check.payload["scan_required"])
        now = timezone.now()
        self.assertEqual(reconciliation._process_source_reverification(check.pk, now), "needs_attention")
        check.refresh_from_db()
        self.assertEqual(check.receipt["child_dispatch_budget"]["scope"], "day")
        # A second paid check the same day raises no second alert.
        second = WebsiteConnectionOperation.objects.create(connection=self.connection, generation=self.connection.generation,
            action="source-reverify", idempotency_key=f"{self.connection.pk}:source-reverify:x", state="pending",
            payload={**check.payload, "source_sha": "f" * 40}, receipt={})
        reconciliation._process_source_reverification(second.pk, now + timedelta(seconds=1))
        self.config.refresh_from_db()
        receipts = (self.config.pillar_strategy or {}).get(alerts.RECEIPTS_KEY) or {}
        budget = [item for item in receipts.values() if (item.get("details") or {}).get("kind") == "operation_budget"]
        self.assertEqual(len(budget), 1, receipts)
        self.assertEqual(self.scans(), sticky.DAILY_SCAN_LIMIT)
        self.remote.assert_not_called()
