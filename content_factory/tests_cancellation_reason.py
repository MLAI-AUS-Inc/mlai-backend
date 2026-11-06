"""P0.14: every backend cancel records why the run stopped and who asked (IF-20).

29 Oct 2026 funnel: 26 of 59 confirmed_topic runs were cancelled with no code,
an empty error and nothing to say who stopped them. They match only the
backend's local article cancel (owner cancel, delete-draft cascade, discard),
which wrote 'Cancelled by user.' and no reason. Every cancel now records
cancellation_reason and cancelled_by, sends them to Content Factory, keeps them
through worker receipts and snapshots, and exposes them to the client.
"""
import ast
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from roo.models import Ledger
from workflow_runs.models import ContentFactoryRun, ContentFactoryRunStep

from . import founder_journey_alerts as alerts
from . import reconciliation
from . import vibe_marketing_views as views
from .dispatch_models import ContentFactoryDispatchOutbox
from .dispatch_outbox import process_dispatch_outbox
from .run_cancellation import (
    DRAFT_DELETED_AT_KEY,
    cancellation_payload,
    hidden_by_draft_delete,
    cancellation_request_fields,
    normalize_cancellation,
    receipt_cancellation_fields,
)
from .service_views import _apply_cancellation_receipt, _merge_django_owned_run_result
from .tests_first_article_entitlement import FREE, EntitlementFixture, keys

CHANNEL = "C0000000000"
ALERTS = {"ARTICLES_FOUNDER_JOURNEY_ALERTS_ENABLED": True, "ARTICLES_FOUNDER_JOURNEY_ALERTS_SINCE": "2026-01-01",
    "ARTICLES_FOUNDER_JOURNEY_ALERTS_MAX_PER_TICK": 20, "CONTENT_FACTORY_OPS_SLACK_CHANNEL_ID": CHANNEL,
    "DEFAULT_BACKEND_URL": "https://api.example.test"}


class CancellationFieldTests(SimpleTestCase):
    def test_reasons_are_allowlisted_and_an_unknown_one_is_unspecified(self):
        self.assertEqual(normalize_cancellation("owner_cancelled", "owner"), ("owner_cancelled", "owner"))
        self.assertEqual(normalize_cancellation("worker_heartbeat_lost", None), ("worker_heartbeat_lost", "system"))
        self.assertEqual(normalize_cancellation("draft_deleted", ""), ("draft_deleted", "owner"))
        self.assertEqual(normalize_cancellation("CANCELLED BY USER", "robot"), ("unspecified", "system"))
        self.assertEqual(normalize_cancellation(None, None), ("unspecified", "system"))

    def test_receipt_keeps_the_saved_reason_and_falls_back_to_the_worker(self):
        self.assertEqual(receipt_cancellation_fields({"cancellation_reason": "owner_cancelled", "cancelled_by": "owner"},
            {"cancellation_reason": "unspecified", "cancelled_by": "system"}),
            {"cancellation_reason": "owner_cancelled", "cancelled_by": "owner"})
        self.assertEqual(receipt_cancellation_fields({}, {"cancellation_reason": "system_stop"}),
            {"cancellation_reason": "system_stop", "cancelled_by": "system"})
        self.assertEqual(receipt_cancellation_fields({}, {}), {"cancellation_reason": "unspecified",
            "cancelled_by": "system"})

    def test_client_payload_exposes_both_fields_and_nothing_for_a_live_run(self):
        self.assertEqual(cancellation_payload({"cancellation_reason": "draft_deleted", "cancelled_by": "owner"}),
            {"cancellationReason": "draft_deleted", "cancelledBy": "owner"})
        self.assertEqual(cancellation_payload({"status": "running"}), {"cancellationReason": None, "cancelledBy": None})
        self.assertEqual(cancellation_payload(None), {"cancellationReason": None, "cancelledBy": None})

    def test_the_fields_are_never_stored_under_a_scanned_code_name(self):
        fields = cancellation_request_fields("system_stop", "system")
        self.assertEqual(set(fields), {"cancellation_reason", "cancelled_by"})
        self.assertFalse({"code", "error_code", "reason_code"} & set(fields))

    def test_both_backend_owned_key_lists_spell_the_draft_deleted_key_as_a_literal(self):
        # AST-extracted merge tests exec these tuples without the module imports.
        from . import service_views
        self.assertIn(DRAFT_DELETED_AT_KEY, views.DJANGO_OWNED_ARTICLE_RESULT_KEYS)
        self.assertIn(DRAFT_DELETED_AT_KEY, service_views._DJANGO_OWNED_RUN_RESULT_KEYS)

    def test_a_kept_attempt_is_hidden_only_while_it_stays_finished(self):
        hidden = {DRAFT_DELETED_AT_KEY: "2026-10-30T00:00:00+00:00"}
        for status in ("completed", "failed", "denied", "cancelled"):
            self.assertTrue(hidden_by_draft_delete(status, hidden))
        self.assertFalse(hidden_by_draft_delete("running", hidden))
        self.assertFalse(hidden_by_draft_delete("completed", {}))


class EveryBackendCancelPassesAReasonTests(SimpleTestCase):
    """Regression: no backend call site can produce a codeless local article cancel again."""

    def test_every_local_article_cancel_names_its_reason_and_actor(self):
        root = Path(__file__).resolve().parent.parent
        calls = []
        for path in sorted(root.glob("*/**/*.py")):
            parts = path.relative_to(root).parts
            if parts[0].startswith(".") or parts[0] in {"tests", "docs", "scripts"} or "migrations" in parts \
                    or path.name.startswith(("test_", "tests_")):
                continue
            source = path.read_text(encoding="utf-8")
            if "_cancel_local_article_run(" not in source:
                continue
            for node in ast.walk(ast.parse(source)):
                if isinstance(node, ast.Call) and getattr(node.func, "id", getattr(node.func, "attr", "")) == \
                        "_cancel_local_article_run":
                    calls.append((path.name, node.lineno, {keyword.arg for keyword in node.keywords}))
        self.assertGreaterEqual(len(calls), 3)
        for name, line, keywords in calls:
            with self.subTest(site=f"{name}:{line}"):
                self.assertLessEqual({"cancellation_reason", "cancelled_by"}, keywords)

    # Cancel calls that never stop an article run, so IF-20 does not label them.
    NON_ARTICLE_CANCEL_SITES = {
        ("community_chat/my_startup_views.py", "post"),  # forwards to control_article_run
        ("content_factory/island_research_cancel.py", "cancel_island_research"),  # island research
        # Non-article branch only (articles go through _stop_stalled_article); its
        # body is pinned by tests_reconciliation_unit, and "worker_heartbeat_lost"
        # is already an allowlisted reason Content Factory records as is.
        ("content_factory/reconciliation.py", "recover_stalled_run"),
        ("content_factory/vibe_marketing_views.py", "_supersede_stale_scan_runs"),  # repo scans
        ("content_factory/vibe_marketing_views.py", "_delete_article_setup_scaffold_branches"),  # setup builds
        ("integrations/services/valley_harness.py", "cancel_valley_run"),  # Valley harness, not Content Factory
    }

    @staticmethod
    def _is_cancel_call(node):
        if not isinstance(node, ast.Call):
            return False
        if any(keyword.arg == "action" and isinstance(keyword.value, ast.Constant) and keyword.value.value == "cancel"
               for keyword in node.keywords):
            return True
        return any(isinstance(arg, ast.JoinedStr) and isinstance(arg.values[-1], ast.Constant)
            and str(arg.values[-1].value).endswith("/cancel") for arg in node.args)

    def test_every_content_factory_cancel_request_sends_a_reason(self):
        """Every function that POSTs a cancel to Content Factory sends the IF-20 pair."""
        root = Path(__file__).resolve().parent.parent
        seen = set()
        for path in sorted(root.glob("*/**/*.py")):
            parts = path.relative_to(root).parts
            if parts[0].startswith(".") or parts[0] in {"tests", "docs", "scripts"} or "migrations" in parts \
                    or path.name.startswith(("test_", "tests_")):
                continue
            source = path.read_text(encoding="utf-8")
            if "cancel" not in source:
                continue
            relative = path.relative_to(root).as_posix()
            for function in ast.walk(ast.parse(source)):
                if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if not any(self._is_cancel_call(node) for node in ast.walk(function)):
                    continue
                site = (relative, function.name)
                seen.add(site)
                if site in self.NON_ARTICLE_CANCEL_SITES:
                    continue
                with self.subTest(site=f"{relative}:{function.name}"):
                    self.assertIn("cancellation_request_fields", ast.get_source_segment(source, function))
        self.assertTrue({("content_factory/website_first_article_settlement.py", "stop_children"),
            ("content_factory/reconciliation.py", "_stop_stalled_article")} <= seen)
        # A stale allowlist entry would hide a new cancel under an old name.
        self.assertLessEqual(self.NON_ARTICLE_CANCEL_SITES, seen)


@override_settings(**FREE)
class OwnerCancelReasonTests(EntitlementFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.bodies = {}
        self.remote = self.enterContext(patch.object(views, "_call_content_factory_run_action",
            side_effect=self.cancel_remote))
        self.enterContext(patch.object(views, "_serialize_run", side_effect=lambda run, **kwargs: {
            "runId": run.run_id, "status": run.status, **cancellation_payload(run.result)}))
        self.context = SimpleNamespace(organization=self.org)

    def cancel_remote(self, *, run_id, action, payload, workflow, **kwargs):
        self.bodies[run_id] = dict(payload)
        return {"status": "cancelled", "cleanup": {}}

    def article(self, *, status="running", step="research_topic", keyword="copy ready topic", **result):
        run = self.make_run({"domain": self.org.domain, "github_repo": "", "target_keyword": keyword,
            "client_request_id": f"key-{uuid4().hex[:8]}", "delivery_mode": "content_only"},
            status=status, step=step, result={"generation": 0, "state_version": 4, "target_keyword": keyword, **result})
        ContentFactoryRunStep.objects.create(run=run, step_key="write", status="running", message="Writing.")
        return run

    def control(self, run, data=None):
        return views.control_article_run(user=self.user, context=self.context, run=run, action="cancel",
            data=data or {})

    def test_owner_cancel_records_owner_cancelled_with_an_empty_error(self):
        run = self.article()
        # A founder's request cannot relabel their own cancel as a system stop.
        response = self.control(run, {"cancellation_reason": "worker_heartbeat_lost", "cancelled_by": "system"})
        self.assertEqual(response.status_code, 202)
        run.refresh_from_db()
        self.assertEqual((run.status, run.error), ("cancelled", ""))
        self.assertEqual((run.result["cancellation_reason"], run.result["cancelled_by"]), ("owner_cancelled", "owner"))
        self.assertEqual(run.steps.get().message, "Cancelled by user.")
        self.assertEqual((self.bodies[run.run_id]["cancellation_reason"], self.bodies[run.run_id]["cancelled_by"]),
            ("owner_cancelled", "owner"))
        self.assertEqual((response.data["cancellationReason"], response.data["cancelledBy"]), ("owner_cancelled", "owner"))

    def test_draft_delete_cancels_live_members_and_keeps_finished_siblings(self):
        now = timezone.now()
        failed = self.article(status="failed", step="plan_article", error_code="CORPUS_INCOMPLETE")
        failed.error = "The corpus was incomplete."
        failed.save(update_fields=["error"])
        delivered = self.article(status="completed", step="finalize")
        blocked = self.article(status="blocked", step="assemble_article")
        earlier_stop = self.article(status="cancelled", step="cancelled", cancellation_reason="system_stop",
            cancelled_by="system")
        pressed = self.article(status="awaiting_approval", step="await_review")
        unrelated = self.article(keyword="another topic")
        for age, run in enumerate((pressed, blocked, delivered, failed, earlier_stop)):
            ContentFactoryRun.objects.filter(pk=run.pk).update(updated_at=now - timedelta(minutes=age))
        kept = {}
        for run in (failed, delivered, earlier_stop):
            run.refresh_from_db()
            kept[run.pk] = (run.status, run.error, run.current_step, run.updated_at)

        response = self.control(pressed, {"cancel_group": True})

        self.assertEqual(response.status_code, 202)
        self.assertCountEqual(response.data["cancelledRunIds"], [pressed.run_id, blocked.run_id])
        for run in (pressed, blocked):
            run.refresh_from_db()
            self.assertEqual((run.status, run.error), ("cancelled", ""))
            self.assertEqual((run.result["cancellation_reason"], run.result["cancelled_by"]), ("draft_deleted", "owner"))
            self.assertEqual(self.bodies[run.run_id]["cancellation_reason"], "draft_deleted")
        for run in (failed, delivered, earlier_stop):
            run.refresh_from_db()
            # Refund and alert sweeps keyed on updated_at see the run as before.
            self.assertEqual((run.status, run.error, run.current_step, run.updated_at), kept[run.pk])
            self.assertNotIn(run.run_id, self.bodies)
        self.assertEqual(failed.result["error_code"], "CORPUS_INCOMPLETE")
        self.assertEqual(earlier_stop.result["cancellation_reason"], "system_stop")
        unrelated.refresh_from_db()
        self.assertEqual(unrelated.status, "running")
        # The kept siblings do not come back as the card the owner just deleted.
        self.assertTrue(delivered.result[DRAFT_DELETED_AT_KEY])
        drafts = views._recent_article_drafts(self.org)
        self.assertEqual([draft["runId"] for draft in drafts], [unrelated.run_id])
        # A worker snapshot of a kept sibling keeps it hidden.
        merged = views._merge_django_owned_article_result(delivered.result, {"status": "completed"})
        self.assertEqual(merged[DRAFT_DELETED_AT_KEY], delivered.result[DRAFT_DELETED_AT_KEY])
        merged = _merge_django_owned_run_result(delivered.result, {"status": "completed"})
        self.assertEqual(merged[DRAFT_DELETED_AT_KEY], delivered.result[DRAFT_DELETED_AT_KEY])

    def test_deleting_a_delivered_draft_cancels_the_card_that_was_pressed(self):
        delivered = self.article(status="completed", step="finalize")
        response = self.control(delivered, {"cancel_group": True})
        self.assertEqual(response.data["cancelledRunIds"], [delivered.run_id])
        delivered.refresh_from_db()
        self.assertEqual((delivered.status, delivered.result["cancellation_reason"]), ("cancelled", "draft_deleted"))

    def test_a_delete_the_worker_refuses_hides_nothing(self):
        delivered = self.article(status="completed", step="finalize")
        pressed = self.article(status="running", step="draft_section:intro")
        self.remote.side_effect = lambda **kwargs: {"error": "Still writing.", "content_factory_status_code": 409}
        response = self.control(pressed, {"cancel_group": True})
        self.assertEqual(response.status_code, 409)
        delivered.refresh_from_db()
        self.assertNotIn(DRAFT_DELETED_AT_KEY, delivered.result)

    def close_pull_requests(self, *, ok=True, reason=""):
        closed = []
        def close(config, pr_url):
            closed.append(pr_url)
            return {"prUrl": pr_url, "ok": ok, "action": "closed" if ok else "", "reason": "" if ok else reason}
        self.enterContext(patch("content_factory.draft_pull_request_cleanup.close_draft_pull_request",
            side_effect=close))
        return closed

    def test_a_kept_sibling_has_its_own_draft_pr_closed_before_it_is_hidden(self):
        pr_url = "https://github.com/acme/site/pull/9"
        delivered = self.article(status="completed", step="finalize", draft_pr_url=pr_url)
        pressed = self.article(status="awaiting_approval", step="await_review")
        closed = self.close_pull_requests()
        response = self.control(pressed, {"cancel_group": True})
        self.assertEqual((response.data["cancelledRunIds"], response.data["protectedRunIds"]), ([pressed.run_id], []))
        self.assertEqual(closed, [pr_url])
        delivered.refresh_from_db()
        self.assertEqual(delivered.status, "completed")
        self.assertTrue(delivered.result[DRAFT_DELETED_AT_KEY])
        self.assertNotIn(delivered.run_id, self.bodies)

    def test_a_kept_sibling_whose_pr_cannot_be_closed_stays_visible(self):
        pr_url = "https://github.com/acme/site/pull/9"
        delivered = self.article(status="completed", step="finalize", draft_pr_url=pr_url)
        pressed = self.article(status="awaiting_approval", step="await_review")
        self.close_pull_requests(ok=False, reason="team_changes")
        response = self.control(pressed, {"cancel_group": True})
        self.assertEqual(response.status_code, 202)
        self.assertEqual((response.data["cancelledRunIds"], response.data["protectedRunIds"]),
            ([pressed.run_id], [delivered.run_id]))
        delivered.refresh_from_db()
        self.assertNotIn(DRAFT_DELETED_AT_KEY, delivered.result)
        self.assertIn(delivered.run_id, [draft["runId"] for draft in views._recent_article_drafts(self.org)])

    def test_kept_siblings_are_gone_from_latest_runs_and_publish_evidence(self):
        delivered = self.article(status="completed", step="finalize")
        pressed = self.article(status="failed", step="plan_article", error_code="CORPUS_INCOMPLETE")
        before = [run.run_id for run in views._latest_runs_for_org(self.org)]
        self.assertIn(delivered.run_id, before)
        self.control(pressed, {"cancel_group": True})
        latest = views._latest_runs_for_org(self.org)
        self.assertNotIn(delivered.run_id, [run.run_id for run in latest])
        # The same pick bootstrap uses for latestRuns' article and publishEvidence.
        self.assertIsNone(views._latest_run_matching(latest, views.ARTICLE_WORKFLOWS))

    def test_a_kept_sibling_the_worker_revives_shows_again(self):
        failed = self.article(status="failed", step="plan_article")
        pressed = self.article(status="awaiting_approval", step="await_review")
        self.control(pressed, {"cancel_group": True})
        failed.refresh_from_db()
        self.assertTrue(failed.result[DRAFT_DELETED_AT_KEY])
        ContentFactoryRun.objects.filter(pk=failed.pk).update(status="running")
        self.assertIn(failed.run_id, [run.run_id for run in views._latest_runs_for_org(self.org)])
        self.assertIn(failed.run_id, [draft["runId"] for draft in views._recent_article_drafts(self.org)])
        # A delete that raced the revival does not hide the live run.
        other = self.article(status="running", step="research_topic", keyword="raced topic")
        self.assertFalse(views._hide_kept_draft_sibling(other, now=timezone.now()))
        other.refresh_from_db()
        self.assertNotIn(DRAFT_DELETED_AT_KEY, other.result)

    def test_a_later_owner_action_never_relabels_a_system_stop(self):
        for data in ({"cancel_group": True}, {}):
            with self.subTest(data=data):
                run = self.article(keyword=f"stopped {bool(data)}")
                views._cancel_local_article_run(run=run, organization=self.org, remote_data={},
                    cancellation_reason="worker_heartbeat_lost", cancelled_by="system")
                run.refresh_from_db()
                self.control(run, data)
                run.refresh_from_db()
                self.assertEqual((run.result["cancellation_reason"], run.result["cancelled_by"]),
                    ("worker_heartbeat_lost", "system"))
                self.assertEqual(run.steps.get().message, "Stopped.")

    def test_a_cancel_with_no_reason_records_unspecified_with_system_wording(self):
        run = self.article()
        cancelled = views._cancel_local_article_run(run=run, organization=self.org, remote_data={})
        self.assertEqual((cancelled.result["cancellation_reason"], cancelled.result["cancelled_by"]),
            ("unspecified", "system"))
        self.assertEqual(cancelled.steps.get().message, "Stopped.")


class DiscardReasonTests(TestCase):
    def setUp(self):
        from .tests_written_article_discard import WrittenArticleDiscardTestCase
        WrittenArticleDiscardTestCase.setUp(self)

    def test_discard_records_article_discarded_on_every_lineage_run(self):
        from .models import WrittenArticle
        article = WrittenArticle.objects.create(organization=self.organization, title="AI Meetup Article",
            slug="meetup-article", category="featured", primary_keyword="ai meetup", source_run_id="run-root")
        root = ContentFactoryRun.objects.create(run_id="run-root", workflow="article_generation", domain="mlai.au",
            status="completed", result={"delivery_package": {"title": "AI Meetup Article", "slug": "meetup-article",
                "target_keyword": "ai meetup"}})
        revision = ContentFactoryRun.objects.create(run_id="run-rev", workflow="article_revision", domain="mlai.au",
            status="running", result={"source_run_id": "run-root"})

        response = self.client.post(f"/api/v1/vibe-marketing/written-articles/{article.id}/discard/")

        self.assertEqual(response.status_code, 200)
        for run in (root, revision):
            run.refresh_from_db()
            self.assertEqual((run.status, run.error), ("cancelled", ""))
            self.assertEqual((run.result["cancellation_reason"], run.result["cancelled_by"]),
                ("article_discarded", "owner"))
        for call in self.mock_remote.call_args_list:
            self.assertEqual((call.kwargs["payload"]["cancellation_reason"], call.kwargs["payload"]["cancelled_by"]),
                ("article_discarded", "owner"))
        self.assertEqual(len(self.mock_remote.call_args_list), 2)


class _Remote(SimpleNamespace):
    def json(self):
        return self.body


@override_settings(**FREE)
class SystemStopReasonTests(EntitlementFixture, TestCase):
    """Reconciliation's two article stops tell Content Factory, and the run, it was the system."""

    def running(self, **result):
        payload = {"domain": self.org.domain, "github_repo": "", "client_request_id": f"key-{uuid4().hex[:8]}",
            "delivery_mode": "content_only"}
        return self.make_run(payload, result={"generation": 0, "state_version": 4, **result})

    def worker_reply(self, run, **result):
        """Content Factory's stop reply; an older worker echoes no reason."""
        return {"status": "cancelled", "article_cancellation_v1": self.stop_receipt(run), "result": result}

    def test_a_heartbeat_lost_stop_sends_its_reason_and_the_worker_receipt_keeps_it(self):
        run = self.running(reconciliation_recovery={"requested_at": "2026-10-29T00:00:00+00:00"})
        reply = self.worker_reply(run)
        with patch.object(reconciliation.requests, "post", return_value=_Remote(status_code=200, body=reply)) as post:
            outcome = reconciliation._stop_stalled_article(run, base_url="https://cf.example.test", headers={},
                now=timezone.now())
        self.assertEqual(outcome, "cancelled_stalled")
        body = post.call_args.kwargs["json"]
        self.assertEqual((body["cancellation_reason"], body["cancelled_by"]), ("worker_heartbeat_lost", "system"))
        run.refresh_from_db()
        self.assertEqual(run.status, "cancelled")
        self.assertEqual((run.result["cancellation_reason"], run.result["cancelled_by"]),
            ("worker_heartbeat_lost", "system"))
        # A later worker receipt or snapshot saying 'unspecified' never erases it.
        _apply_cancellation_receipt(run.run_id, {"domain": run.domain, "status": "cancelled",
            "result": {"cancellation_reason": "unspecified", "cancelled_by": "staff"}})
        run.refresh_from_db()
        self.assertEqual((run.result["cancellation_reason"], run.result["cancelled_by"]),
            ("worker_heartbeat_lost", "system"))
        merged = _merge_django_owned_run_result(run.result, {"status": "cancelled", "cancellation_reason": "unspecified"})
        self.assertEqual(merged["cancellation_reason"], "worker_heartbeat_lost")

    def test_a_settlement_stop_sends_system_stop(self):
        run = self.running(error_code="RUN_BUDGET_EXHAUSTED", budget={"exhausted": True})
        ContentFactoryRun.objects.filter(pk=run.pk).update(status="blocked", current_step="draft_section:intro",
            updated_at=timezone.now() - timedelta(hours=3))
        run.refresh_from_db()
        replies = []
        def stop(**kwargs):
            replies.append(kwargs["payload"])
            return self.worker_reply(run)
        with patch.object(views, "_call_content_factory_run_action", side_effect=stop):
            count = reconciliation._settle_exhausted_article_runs(limit=10, now=timezone.now())
        self.assertEqual(count, 1)
        self.assertEqual((replies[0]["cancellation_reason"], replies[0]["cancelled_by"]), ("system_stop", "system"))
        run.refresh_from_db()
        self.assertEqual((run.status, run.result["cancellation_reason"], run.result["cancelled_by"]),
            ("cancelled", "system_stop", "system"))

    def test_a_website_connection_change_records_superseded(self):
        from . import website_cancellation_retirement as cancel_work
        run = self.running()
        op = SimpleNamespace(receipt={})
        self.assertTrue(cancel_work.acknowledge(op, run, run.run_id, 200, self.worker_reply(run)))
        run.refresh_from_db()
        self.assertEqual((run.status, run.result["cancellation_reason"], run.result["cancelled_by"]),
            ("cancelled", "superseded", "system"))

    def test_a_receipt_for_a_run_with_no_saved_reason_takes_the_workers(self):
        run = self.running()
        _apply_cancellation_receipt(run.run_id, {"domain": run.domain, "status": "cancelled",
            "result": {"cancellation_reason": "superseded", "cancelled_by": "system", "code": "ignored"}})
        run.refresh_from_db()
        self.assertEqual((run.result["cancellation_reason"], run.result["cancelled_by"]), ("superseded", "system"))


@override_settings(**FREE, **ALERTS)
class CancellationAlertTests(EntitlementFixture, TestCase):
    """BE2's sweep alerts system and unspecified stops, and never an owner's own cancel."""

    def sweep(self):
        def acknowledged(**kwargs):
            return {"ok": True, "ts": "1700000000.000001"}
        with patch("integrations.services.slack.SlackService.get_client") as client:
            client.return_value.chat_postMessage.side_effect = acknowledged
            alerts.run_founder_journey_alert_sweep(now=timezone.now())
        return client.return_value.chat_postMessage.call_args_list

    def alerted(self, run):
        run.refresh_from_db()
        return set((run.result or {}).get("ops_alerts", {}))

    def test_a_cancel_with_no_reason_is_alerted_and_an_owner_cancel_is_not(self):
        payload = {"domain": self.org.domain, "github_repo": "", "delivery_mode": "content_only"}
        silent = self.make_run({**payload, "client_request_id": "silent"})
        owner = self.make_run({**payload, "client_request_id": "owner"})
        views._cancel_local_article_run(run=silent, organization=self.org, remote_data={})
        views._cancel_local_article_run(run=owner, organization=self.org, remote_data={},
            cancellation_reason="owner_cancelled", cancelled_by="owner")
        calls = self.sweep()
        self.assertEqual(self.alerted(silent), {"run_terminal"})
        self.assertEqual(self.alerted(owner), set())
        self.assertEqual(len([call for call in calls if call.kwargs["client_msg_id"]
            == silent.result["ops_alerts"]["run_terminal"]["alert_id"]]), 1)
        self.assertEqual(self.sweep(), [])


@override_settings(**FREE, **ALERTS)
class NoWorkDispatchRowTests(EntitlementFixture, TestCase):
    """The prod 'never received' and 'dispatch_rejected editorial_run_conflict' rows.

    Each one tells ops once and gives a free first article its attempt back once,
    however many times the sweeps and polls replay it.
    """

    def sweep_alerts(self):
        with patch("integrations.services.slack.SlackService.get_client") as client:
            client.return_value.chat_postMessage.return_value = {"ok": True, "ts": "1700000000.000001"}
            alerts.run_founder_journey_alert_sweep(now=timezone.now())
        return client.return_value.chat_postMessage.call_args_list

    def assert_released_once(self):
        self.assertEqual(keys(self.org), ["attempt:1", "attempt:1:released"])
        self.assertFalse(Ledger.objects.filter(kind__in=["SPEND", "REFUND"]).exists())

    def assert_alerted_once(self, run):
        first = self.sweep_alerts()
        run.refresh_from_db()
        alert_id = run.result["ops_alerts"]["run_terminal"]["alert_id"]
        self.assertEqual([call.kwargs["client_msg_id"] for call in first].count(alert_id), 1)
        self.assertEqual(self.sweep_alerts(), [])

    def test_a_dispatch_content_factory_never_received_alerts_and_releases_once(self):
        payload = self.started()
        key = payload["client_request_id"]
        run = ContentFactoryRun.objects.create(run_id=key, workflow="article_generation", domain=self.org.domain,
            organization=self.org, status="queued", current_step="queued",
            run_request={**payload, "dispatch_pending_resolution": True}, result={})
        ContentFactoryRun.objects.filter(pk=run.pk).update(created_at=timezone.now() - timedelta(minutes=10))
        run.refresh_from_db()
        row = ContentFactoryDispatchOutbox.objects.get(client_request_id=key)
        row.state, row.next_attempt_at = "awaiting_resolution", timezone.now() - timedelta(minutes=1)
        row.save(update_fields=["state", "next_attempt_at"])
        absent = ("not_received", {"detail": "No dispatch with that key."})
        with patch.object(views, "_content_factory_remote_config", return_value={"enabled": True,
                "base_url": "https://cf.example.test"}), \
                patch.object(views, "_lookup_content_factory_dispatch_by_key", return_value=absent):
            for _ in range(2):
                process_dispatch_outbox(now=timezone.now() + timedelta(hours=1))
                polled = ContentFactoryRun.objects.get(pk=run.pk)
                if views._run_pending_remote_dispatch(polled):  # as the status poll does
                    views._resolve_dispatch_token_run(polled)
                reconciliation._settle_sweep_refunds(limit=10)
        row.refresh_from_db()
        run.refresh_from_db()
        self.assertEqual((row.state, row.last_error), ("failed", "dispatch_never_received"))
        self.assertEqual((run.status, run.result["error_code"]), ("failed", "dispatch_child_missing"))
        self.assertIn("did not receive", run.error)
        self.assert_released_once()
        self.assert_alerted_once(run)

    def test_a_dispatch_rejected_editorial_run_conflict_alerts_and_releases_once(self):
        payload = self.started()
        run = self.make_run(payload, status="failed", step="dispatch_rejected",
            result={"status": "failed", "error_code": "dispatch_rejected", "backend_code": "editorial_run_conflict"})
        run.error = "Dispatch rejected by backend: editorial_run_conflict"
        run.save(update_fields=["error"])
        ContentFactoryRun.objects.filter(pk=run.pk).update(updated_at=timezone.now() - timedelta(hours=2))
        row = ContentFactoryDispatchOutbox.objects.get(client_request_id=payload["client_request_id"])
        row.state, row.last_error = "refund_pending", "dispatch_rejected"
        row.save(update_fields=["state", "last_error"])
        record = {"run_id": run.run_id, "run_status": "failed", "error": "backend_rejected:editorial_run_conflict"}
        with patch.object(views, "_content_factory_remote_config", return_value={"enabled": True}), \
                patch.object(views, "_lookup_content_factory_dispatch_by_key", return_value=("rejected", record)):
            for _ in range(2):
                process_dispatch_outbox(now=timezone.now() + timedelta(hours=1))
                reconciliation._queue_dispatch_rejected_article_refunds(limit=10, now=timezone.now())
                reconciliation._settle_sweep_refunds(limit=10)
        row.refresh_from_db()
        self.assertEqual(row.state, "failed")
        self.assert_released_once()
        self.assert_alerted_once(run)


class EditorialRunConflictLogTests(TestCase):
    def test_a_refused_worker_snapshot_names_the_guard_in_the_log(self):
        from . import service_views
        from .editorial_run_state import EditorialRunConflict
        guard = "Worker snapshots cannot change original article identity or delivery consent"
        with patch.object(service_views, "_sync_content_factory_run_snapshot", side_effect=EditorialRunConflict(guard)), \
                self.assertLogs("content_factory.service_views", level="WARNING") as logs:
            response = service_views._apply_run_snapshot("run-conflict", {"workflow": "article_generation",
                "status": "running", "domain": "conflict.example.test"})
        self.assertEqual((response.status_code, response.data["code"]), (409, "editorial_run_conflict"))
        self.assertIn(f"run_id=run-conflict workflow=article_generation guard={guard} field=-", "\n".join(logs.output))

    def test_the_identity_guard_names_the_field_it_refused(self):
        from .editorial_run_state import EditorialRunConflict
        from .service_views import _merge_django_owned_run_request
        with self.assertRaises(EditorialRunConflict) as refused:
            _merge_django_owned_run_request({"client_request_id": "key", "delivery_mode": "content_only"},
                {"client_request_id": "key", "delivery_mode": "review_draft"})
        self.assertEqual(refused.exception.guard_field, "delivery_mode")
        # The content-only "" vs null repository is not a conflict (be#1232).
        _merge_django_owned_run_request({"github_repo": ""}, {"github_repo": None})
