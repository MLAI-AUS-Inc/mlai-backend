"""Publication-purpose and original-charge regressions without DB or network."""
from contextlib import nullcontext
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase, override_settings
from django.utils import timezone

from . import vibe_marketing_views as views  # Load decorated production paths before patching DB boundaries.
from . import website_goal as goals, website_goal_runner as runner, website_prepare as prepare
from . import tests_website_goal_unit as fixtures


@override_settings(ARTICLES_WEBSITE_GOAL_RUNNER_ENABLED=True,
                   ARTICLES_WEBSITE_GOAL_ENROLLED_ORGANIZATIONS=["7"],
                   ARTICLES_FAILURE_TAXONOMY_POLICY_ENABLED=True,
                   ARTICLES_FAILURE_TAXONOMY_ENROLLED_ORGANIZATIONS=["7"])
class CompletedArticleSourceWakeTests(SimpleTestCase):
    def operation(self):
        op = fixtures.GoalRunnerRegressions().operation()
        op.state = "completed"
        op.connection.verified_sha = "b" * 40
        op.connection.authorized_by = SimpleNamespace(pk=4)
        op.connection.authorized_by_id = 4
        goal = goals.new_goal(str(op.pk), fixtures.scope(), target="first_article_live", now=timezone.now())
        goal.update(current_source_sha="a" * 40, completed_source_sha="a" * 40)
        op.receipt = {"goal": goal, "first_article_billing": {"client_request_id": "original-paid-goal", "spend_ledger_id": 123}}
        rows = Mock()
        rows.select_for_update.return_value = rows
        rows.filter.return_value = rows
        rows.order_by.return_value = rows
        rows.first.return_value = op
        op.connection.operations = rows
        return op

    def test_completed_first_article_own_merge_push_cannot_reserve_a_successor(self):
        from . import website_connections as authority
        op = self.operation()
        before = deepcopy(op.receipt)
        with patch.object(authority, "contract_for", return_value={}), \
                patch.object(authority, "authority_guard", return_value=nullcontext(op.connection)), \
                patch.object(prepare, "start_prepare") as start:
            self.assertIsNone(runner.wake_goal_for_source(SimpleNamespace(website_connection=op.connection),
                company_id=9, user=op.connection.authorized_by, now=timezone.now()))
        start.assert_not_called()
        op.save.assert_not_called()
        self.assertEqual(op.receipt, before)

    @override_settings(ARTICLES_WEBSITE_GOAL_RUNNER_ENABLED=False)
    def test_rollback_cannot_send_completed_paid_goal_through_legacy_reopening(self):
        from .website_models import WebsiteConnection, WebsiteConnectionOperation
        from workflow_runs.models import ContentFactoryRun
        from .models import OrganizationContentConfig
        from founder_tools.models import VibeRaisingCompany
        op = self.operation()
        with patch.object(WebsiteConnection.objects, "select_related") as connections, \
                patch.object(OrganizationContentConfig.objects, "filter") as configs, \
                patch.object(VibeRaisingCompany.objects, "filter") as companies, \
                patch.object(ContentFactoryRun.objects, "filter") as runs, \
                patch.object(WebsiteConnectionOperation.objects, "filter") as writes, \
                patch.object(prepare, "start_prepare", return_value=op) as start:
            runs.return_value.exists.return_value = False
            writes.return_value.update.return_value = 0
            connections.return_value.filter.return_value.first.return_value = op.connection
            configs.return_value.first.return_value = SimpleNamespace(website_connection=op.connection)
            companies.return_value.first.return_value = SimpleNamespace(pk=9)
            self.assertIsNone(prepare.wake_prepare_for_source(op.connection.pk, organization_id=7,
                owned_merge_run_id="first-article-publish"))
        start.assert_not_called()
        op.connection.operations.update.assert_not_called()
        op.save.assert_not_called()


class ArticleCancellationSettlementTests(SimpleTestCase):
    def setUp(self):
        from hashlib import sha256
        from .website_failure_contract import canonical_bytes
        self.run = SimpleNamespace(pk=12, run_id="paid-article", organization_id=7, domain="fixture.test",
            workflow="article_generation", github_repo="fixture/site", status="blocked", current_step="verify_static",
            approval_state="not_required", resume_available=False, error="structure rejected", steps=Mock(), save=Mock(),
            acceptance_summary={}, verification_summary={}, run_request={
                "client_request_id": "original-charge", "website_connection_id": "connection-7", "connection_generation": 3,
                "github_repo": "fixture/site", "repository_id": 19, "expected_source_sha": "a" * 40,
                "operation_id": "operation-7", "operation_attempt": 2, "deletion_epoch": 0,
                "roo_points_cost": 6, "roo_points_ledger_id": "123"},
            result={"generation": 1, "state_version": 8, "saved_proof": {"sha": "a" * 40},
                "pr_url": "https://github.com/fixture/site/pull/40"})
        self.run.steps.all.return_value = []
        scope = {key: value for key, value in self.run.run_request.items() if not key.startswith("roo_points")}
        self.setup = {"version": 1, "purpose": "setup", "run_id": self.run.run_id, "goal_id": None,
            "scope": scope, "kind": "pr", "state": "observed", "pr_url": self.run.result["pr_url"]}
        self.run.result["publication_evidence_v1"] = self.setup
        self.receipt = {"version": 1, "purpose": "article_cancellation", "run_id": self.run.run_id, "goal_id": None,
            "scope": scope, "generation": 1, "state_version": 9, "outcome": "no_article_effect", "cancelled": True,
            "delivered_value": False, "watermark": {"operation_id": "operation-7", "attempt": 2, "cancelled": True}}
        self.receipt["receipt_id"] = sha256(canonical_bytes(self.receipt)).hexdigest()

    def cancel(self, receipt=None):
        with patch.object(views.transaction, "atomic", return_value=nullcontext()), \
                patch.object(views.ContentFactoryRun.objects, "select_for_update") as rows, \
                patch.object(views.VibeMarketingComponentComment.objects, "filter"), \
                patch.object(views, "_release_cancelled_article_keyword"):
            rows.return_value.get.return_value = self.run
            return views._cancel_local_article_run(run=self.run, organization=SimpleNamespace(pk=7),
                remote_data={"article_cancellation_v1": receipt or self.receipt})

    def test_setup_pr_is_not_article_publication(self):
        self.assertFalse(views._run_has_external_publish_evidence(self.run))

    def test_source_stop_cannot_refund_an_unresolved_publication_handoff(self):
        from .article_publication_evidence import refund_reconciliation_reason
        self.cancel()
        for fields in ({"publish_handoff_pending": True}, {"publish_handoff_status": "stale"},
                       {"publish_child_run_id": "publisher-child"}, {"promoted_publish_job_id": "publisher-child"}):
            with self.subTest(fields=fields):
                original = deepcopy(self.run.result)
                self.run.result.update(fields)
                self.assertEqual(refund_reconciliation_reason(self.run), "needs_reconciliation")
                self.run.result = original

    def test_cancellation_keeps_original_proof_and_reserves_no_value_refund(self):
        original = deepcopy(self.run.run_request)
        self.cancel()
        self.assertEqual(self.run.status, "cancelled")
        self.assertEqual(self.run.run_request, original)
        self.assertEqual(self.run.result.get("saved_proof"), {"sha": "a" * 40})
        self.assertTrue(self.run.result.get("reconciliation_refund_pending"))
        self.assertEqual(self.run.result.get("article_terminal_refund", {}).get("status"), "pending")

    def test_repeat_cancellation_cannot_erase_settled_refund(self):
        self.run.result.update(article_terminal_refund={"status": "refunded", "refund_ledger_id": "222"},
            reconciliation_refund_pending=False)
        self.cancel()
        self.assertEqual(self.run.result.get("article_terminal_refund"), {"status": "refunded", "refund_ledger_id": "222"})
        self.assertFalse(self.run.result["reconciliation_refund_pending"])

    def reseal(self, receipt):
        from hashlib import sha256
        from .website_failure_contract import canonical_bytes
        receipt = deepcopy(receipt)
        receipt.pop("receipt_id", None)
        receipt["receipt_id"] = sha256(canonical_bytes(receipt)).hexdigest()
        return receipt

    def test_original_identity_version_and_digest_are_required(self):
        from .article_publication_evidence import cancellation_receipt
        cases = [dict(self.receipt, run_id="another-run"), dict(self.receipt, goal_id="another-goal"),
            dict(self.receipt, generation=2), dict(self.receipt, state_version=7),
            dict(self.receipt, watermark={"operation_id": "operation-7", "attempt": 3, "cancelled": True}),
            dict(self.receipt, scope={**self.receipt["scope"], "app_root": ""}),
            dict(self.receipt, scope={**self.receipt["scope"], "repository_id": True})]
        for receipt in cases:
            with self.subTest(receipt=receipt), self.assertRaises(ValueError):
                cancellation_receipt(self.run, {"article_cancellation_v1": self.reseal(receipt)})
        with self.assertRaises(ValueError):
            cancellation_receipt(self.run, {"article_cancellation_v1": {**self.receipt, "receipt_id": "wrong"}})

    def test_useful_draft_article_effect_and_ambiguous_effect_keep_charge(self):
        from .article_publication_evidence import cancellation_result
        original = deepcopy(self.run.result)
        self.run.run_request.update(delivery_mode="content_only", delivery_mode_confirmed=True)
        for changes in ({"content_package": {"content_packaged": True}},
                        {"status": "preview_ready", "preview_content_verified": True, "preview_url": "https://preview.fixture.test"},
                        {"pr_url": "https://github.com/fixture/site/pull/99"},
                        {"publication_evidence_v1": {**self.setup, "purpose": "article"}}):
            with self.subTest(changes=changes):
                self.run.result = {**original, **changes}
                result = cancellation_result(self.run, {"article_cancellation_v1": self.receipt})
                self.assertNotIn("reconciliation_refund_pending", result)
                for key, value in changes.items():
                    self.assertEqual(result[key], value)
        self.run.result = original
        result = cancellation_result(self.run, {"article_cancellation_v1": self.reseal({**self.receipt, "delivered_value": True})})
        self.assertNotIn("reconciliation_refund_pending", result)

    def test_child_goal_keeps_payment_owner_and_does_not_queue_late_refund(self):
        self.run.run_request["first_article_goal_operation_id"] = "prepare-7"
        self.cancel()
        self.assertEqual(self.run.result["article_settlement_owner"], "first_article_goal")
        self.assertNotIn("reconciliation_refund_pending", self.run.result)

    def test_real_cancel_control_stops_known_effect_without_claiming_refund(self):
        receipt = self.reseal({**self.receipt, "outcome": "article_effect", "delivered_value": True})
        self.run.result["publication_evidence_v1"] = {**self.setup, "purpose": "article"}
        with patch.object(views.transaction, "atomic", return_value=nullcontext()), \
                patch.object(views.ContentFactoryRun.objects, "select_for_update") as locked, \
                patch.object(views.ContentFactoryRun.objects, "prefetch_related") as selected, \
                patch.object(views, "founder_actor_id_for_user", return_value="founder:4"), \
                patch.object(views, "_release_cancelled_article_keyword"), \
                patch.object(views, "_serialize_run", side_effect=lambda run, **kw: {"status": run.status}), \
                patch.object(views, "_call_content_factory_run_action", return_value={"article_cancellation_v1": receipt}) as remote:
            locked.return_value.get.return_value = self.run
            selected.return_value.get.return_value = self.run
            response = views.control_article_run(user=SimpleNamespace(pk=4), context=SimpleNamespace(organization=SimpleNamespace(pk=7)),
                run=self.run, action="cancel", data={})
        self.assertEqual(response.status_code, 202)
        self.assertEqual(self.run.status, "cancelled")
        remote.assert_called_once()
        self.assertTrue(self.run.result["reconciliation_required"])
        self.assertNotIn("reconciliation_refund_pending", self.run.result)
        self.assertEqual(self.run.result["publication_evidence_v1"]["purpose"], "article")
        self.assertNotIn("artifacts", self.run.steps.exclude.return_value.update.call_args.kwargs)

    def test_legacy_unknown_effect_without_tombstone_stays_protected(self):
        self.run.result.pop("publication_evidence_v1")
        self.assertTrue(views._article_cancel_receipt_conflict(self.run, {"status": "cancelled"}))
        self.assertFalse(views._article_cancel_receipt_conflict(self.run, {"article_cancellation_v1": self.receipt}))
        self.cancel()
        self.assertNotIn("reconciliation_refund_pending", self.run.result)
        self.assertTrue(self.run.result["reconciliation_required"])

    def test_actual_callback_keeps_credit_after_duplicate_and_rejects_wrong_source(self):
        from . import service_views
        before = deepcopy(self.run.run_request)
        with patch.object(service_views.transaction, "atomic", return_value=nullcontext()), \
                patch.object(service_views.ContentFactoryRun.objects, "select_for_update") as rows, \
                patch.object(service_views, "_serialize_content_factory_run", side_effect=lambda run: {"status": run.status}), \
                patch("content_factory.website_operations.observe_workflow_status"):
            rows.return_value.filter.return_value.first.return_value = self.run
            payload = {"domain": self.run.domain, "status": "cancelled", "result": {"article_cancellation_v1": self.receipt}}
            self.assertEqual(service_views._apply_cancellation_receipt(self.run.run_id, payload).status_code, 200)
            self.run.result["article_terminal_refund"].update(status="refunded", refund_ledger_id="222")
            self.run.result["reconciliation_refund_pending"] = False
            self.assertEqual(service_views._apply_cancellation_receipt(self.run.run_id, payload).status_code, 200)
            wrong = self.reseal({**self.receipt, "scope": {**self.receipt["scope"], "expected_source_sha": "b" * 40}})
            self.assertEqual(service_views._apply_cancellation_receipt(self.run.run_id, {**payload,
                "result": {"article_cancellation_v1": wrong}}).status_code, 409)
        self.assertEqual(self.run.run_request, before)
        self.assertEqual(self.run.result["article_terminal_refund"]["refund_ledger_id"], "222")
        self.assertFalse(self.run.result["reconciliation_refund_pending"])

    def test_worker_snapshot_cannot_invent_or_erase_backend_refund_work(self):
        from .service_views import _merge_django_owned_run_result
        owned = {"article_terminal_refund": {"status": "pending"}, "reconciliation_refund_pending": True,
                 "article_settlement_attempts": 2}
        self.assertEqual(_merge_django_owned_run_result({}, owned), {})
        self.assertEqual(_merge_django_owned_run_result(owned, {"reconciliation_refund_pending": False,
            "article_terminal_refund": {"status": "refunded"}, "article_settlement_attempts": 0}), owned)

    def test_permitted_owner_waits_are_never_automatic_settlement(self):
        from .article_publication_evidence import exhausted_article_candidate
        self.run.result["error_code"] = "RUN_BUDGET_EXHAUSTED"
        self.assertTrue(exhausted_article_candidate(self.run))
        for step, failure_class in (("await_merge", "needs_user"), ("await_deploy", "waiting"), ("await_review", "needs_user")):
            self.run.current_step = step
            self.run.result["failure_class"] = failure_class
            self.assertFalse(exhausted_article_candidate(self.run))
        self.run.current_step = "verify_static"
        self.run.result.pop("failure_class")
        self.run.result["error_code"] = "UNCLASSIFIED"
        self.assertFalse(exhausted_article_candidate(self.run))

    def test_exhausted_sweep_obtains_real_stop_receipt_before_reserving_credit(self):
        from . import reconciliation, service_views
        from datetime import timedelta
        self.run.result["error_code"] = "RUN_BUDGET_EXHAUSTED"
        self.run.updated_at, self.run.reconciled_at = timezone.now() - timedelta(hours=1), None
        rows = Mock()
        rows.filter.return_value = rows
        rows.order_by.return_value = rows
        rows.__getitem__ = Mock(return_value=[self.run])
        def claim(**changes):
            self.run.result = changes["result"]
            self.run.reconciled_at = changes["reconciled_at"]
            return 1
        rows.update.side_effect = claim
        with patch.object(reconciliation.ContentFactoryRun.objects, "filter", return_value=rows), \
                patch.object(reconciliation.ContentFactoryRun.objects, "select_for_update") as locked, \
                patch.object(reconciliation.transaction, "atomic", return_value=nullcontext()), \
                patch.object(views, "_call_content_factory_run_action", return_value={"article_cancellation_v1": self.receipt}) as remote, \
                patch.object(service_views, "_serialize_content_factory_run", side_effect=lambda run: {"status": run.status}), \
                patch("content_factory.website_operations.observe_workflow_status"):
            locked.return_value.filter.return_value.first.return_value = self.run
            self.assertEqual(reconciliation._settle_exhausted_article_runs(limit=10, now=timezone.now()), 1)
        remote.assert_called_once()
        from .website_cancellation_retirement import expected_article_cancellation
        self.assertEqual(remote.call_args.kwargs["payload"]["expected_cancellation_v1"], expected_article_cancellation(self.run))
        self.assertEqual(self.run.status, "cancelled")
        self.assertTrue(self.run.result["reconciliation_refund_pending"])
        self.assertEqual(self.run.result["article_settlement_attempts"], 1)

    def test_exhausted_sweep_bounds_unknown_responses_without_refund(self):
        from . import reconciliation
        from datetime import timedelta
        self.run.result["error_code"] = "RUN_BUDGET_EXHAUSTED"
        self.run.updated_at, self.run.reconciled_at = timezone.now() - timedelta(hours=1), None
        rows = Mock()
        rows.filter.return_value = rows
        rows.order_by.return_value = rows
        rows.__getitem__ = Mock(return_value=[self.run])
        def claim(**changes):
            self.run.result = changes["result"]
            self.run.reconciled_at = changes["reconciled_at"]
            return 1
        rows.update.side_effect = claim
        with patch.object(reconciliation.ContentFactoryRun.objects, "filter", return_value=rows), \
                patch.object(views, "_call_content_factory_run_action", return_value={"status": "unknown"}) as remote:
            for _ in range(5):
                self.assertEqual(reconciliation._settle_exhausted_article_runs(limit=10, now=timezone.now()), 0)
        self.assertEqual(remote.call_count, 3)
        self.assertEqual(self.run.status, "blocked")
        self.assertNotIn("reconciliation_refund_pending", self.run.result)
        self.assertEqual(self.run.result["article_settlement_status"], "needs_reconciliation")

    def test_exhausted_sweep_cannot_replace_a_later_refund_receipt(self):
        from . import reconciliation
        self.run.status = "cancelled"
        self.run.updated_at, self.run.reconciled_at = timezone.now(), None
        self.run.result.update(reconciliation_refund_pending=True,
            article_terminal_refund={"status": "stop_required"})
        saved = deepcopy({key: getattr(self.run, key) for key in ("pk", "status", "updated_at", "reconciled_at", "result")})
        refunded = {"status": "refunded", "refund_ledger_id": "222"}
        candidates = Mock()
        candidates.filter.return_value = candidates
        candidates.order_by.return_value = candidates
        candidates.__getitem__ = Mock(return_value=[self.run])

        def select(**conditions):
            if "pk" not in conditions:
                return candidates
            saved["result"]["article_terminal_refund"] = deepcopy(refunded)
            saved["result"]["reconciliation_refund_pending"] = False

            def update(**changes):
                if any(saved.get(key) != value for key, value in conditions.items()):
                    return 0
                saved.update(deepcopy(changes))
                return 1

            return SimpleNamespace(update=update)

        with patch.object(reconciliation.ContentFactoryRun.objects, "filter", side_effect=select), \
                patch.object(views, "_call_content_factory_run_action", return_value={}) as remote:
            self.assertEqual(reconciliation._settle_exhausted_article_runs(limit=10, now=timezone.now()), 0)
        self.assertEqual(saved["result"]["article_terminal_refund"], refunded)
        self.assertFalse(saved["result"]["reconciliation_refund_pending"])
        self.assertNotIn("article_settlement_attempts", saved["result"])
        remote.assert_not_called()

    def test_worker_can_explain_legacy_setup_link_but_cannot_replace_article_effect(self):
        from .article_publication_evidence import cancellation_result
        self.run.result.pop("publication_evidence_v1")
        payload = {"article_cancellation_v1": self.receipt, "publication_evidence_v1": self.setup}
        result = cancellation_result(self.run, payload)
        self.assertTrue(result["reconciliation_refund_pending"])
        self.run.result["publication_evidence_v1"] = {**self.setup, "purpose": "article", "state": "pending"}
        result = cancellation_result(self.run, payload)
        self.assertEqual(result["publication_evidence_v1"]["purpose"], "article")
        self.assertNotIn("reconciliation_refund_pending", result)

    def test_terminal_failure_flag_cannot_bypass_effect_or_useful_delivery_protection(self):
        from integrations.services import article_generation
        job = SimpleNamespace(job_id=self.run.run_id, domain=self.run.domain, request_meta={},
            billing_status="charged", client_request_id="original-charge", billing_ledger_id=123)
        original = deepcopy(self.run.result)
        self.run.run_request.update(delivery_mode="content_only", delivery_mode_confirmed=True)
        for changes in ({"publication_evidence_v1": {**self.setup, "purpose": "article"}},
                        {"pr_url": "https://github.com/fixture/site/pull/99"},
                        {"content_package": {"content_packaged": True}}):
            self.run.result = {**original, **changes}
            with patch.object(views.ContentFactoryRun.objects, "filter") as rows, \
                    patch.object(article_generation, "_record_terminal_article_refund") as reserve, \
                    patch.object(article_generation, "_refund_content_factory_request") as credit:
                rows.return_value.first.return_value = self.run
                self.assertEqual(article_generation.maybe_auto_refund_terminal_failure(job,
                    error_code="RUN_BUDGET_EXHAUSTED", error_message="Failed", refundable=True), (False, 0))
            reserve.assert_not_called()
            credit.assert_not_called()

    def test_exact_cf_producer_receipt_cancels_legacy_setup_link_and_queues_original_charge(self):
        import json
        from pathlib import Path
        from . import service_views
        fixture = json.loads((Path(__file__).parent / "test_fixtures/article-cancellation-no-value-v1.json").read_text())
        original = fixture["original_request"]
        self.run.run_id = original["run_id"]
        self.run.run_request = {**original, "roo_points_cost": 6, "roo_points_ledger_id": "123"}
        self.run.result = {"generation": 0, "state_version": 1, "pr_url": fixture["setup_publication"]["pr_url"],
            "pr_number": 40, "article_system_setup": {"setup_run_id": "setup-child"}}
        with patch.object(service_views.transaction, "atomic", return_value=nullcontext()), \
                patch.object(service_views.ContentFactoryRun.objects, "select_for_update") as rows, \
                patch.object(service_views, "_serialize_content_factory_run", side_effect=lambda run: {"status": run.status}), \
                patch("content_factory.website_operations.observe_workflow_status"):
            rows.return_value.filter.return_value.first.return_value = self.run
            response = service_views._apply_cancellation_receipt(self.run.run_id,
                {"domain": self.run.domain, "status": "cancelled", "result": fixture["cancellation_response"]})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.run.status, "cancelled")
        self.assertTrue(self.run.result["reconciliation_refund_pending"])
        self.assertEqual(self.run.result["article_terminal_refund"]["cancellation_receipt_id"],
            "4b45413e5b0a6633e1ac930c915835763dfe7b0e620747f10e1445b36b7061d4")
        self.assertEqual(self.run.run_request["client_request_id"], "paid-original-request")
        self.assertEqual(self.run.run_request["roo_points_ledger_id"], "123")
        self.assertFalse(views._run_has_external_publish_evidence(self.run))

    def test_planned_review_marker_alone_does_not_claim_useful_delivery(self):
        from .article_publication_evidence import useful_delivery
        self.run.current_step = "await_review"
        self.run.result["review_surface_kind"] = "component_live_preview"
        self.assertFalse(useful_delivery(self.run))

    def test_publication_metadata_cannot_persist_credentials_or_external_links(self):
        from .article_publication_evidence import cancellation_result
        for change in ({"github_token": "synthetic-secret"}, {"pr_url": "https://github.com/fixture/site/pull/40?token=synthetic-secret"},
                       {"pr_url": "https://example.test/other/pull/40"}, {"setup_run_id": "x" * 256}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                cancellation_result(self.run, {"article_cancellation_v1": self.receipt,
                    "publication_evidence_v1": {**self.setup, **change}})

    def test_validated_setup_only_does_not_block_terminal_budget_refund(self):
        from integrations.services import article_generation
        job = SimpleNamespace(job_id=self.run.run_id, domain=self.run.domain, request_meta={},
            billing_status="charged", client_request_id="original-charge", billing_ledger_id=123)
        with patch.object(views.ContentFactoryRun.objects, "filter") as rows, \
                patch.object(article_generation, "_record_terminal_article_refund") as reserve, \
                patch.object(article_generation, "_get_content_factory_user_for_job", return_value=SimpleNamespace(pk=5)), \
                patch("content_factory.reconciliation._refund_failed_run",
                    return_value=SimpleNamespace(pk=222, delta_microroo=6_000_000)) as credit:
            rows.return_value.first.return_value = self.run
            self.assertEqual(article_generation.maybe_auto_refund_terminal_failure(job,
                error_code="RUN_BUDGET_EXHAUSTED", error_message="Failed", refundable=False), (True, 6))
        credit.assert_called_once()
        self.assertIs(credit.call_args.args[0], self.run)
        self.assertEqual(reserve.call_args.kwargs["status"], "pending")

    def test_internal_publish_package_and_silent_fallback_are_not_delivered_value(self):
        from .article_publication_evidence import cancellation_result
        self.run.run_request.update(delivery_mode="publish_code", delivery_mode_confirmed=True,
            resolved_delivery_mode="content_only")
        self.run.acceptance_summary = {"content_packaged": True}
        self.run.result["content_package"] = {"content_packaged": True}
        result = cancellation_result(self.run, {"article_cancellation_v1": self.receipt})
        self.assertTrue(result["reconciliation_refund_pending"])

    def test_resume_keeps_original_scope_presence_and_payment_key(self):
        from . import website_operations as operations, website_connections as authority
        from uuid import uuid4
        website = SimpleNamespace(pk=uuid4(), generation=3, repository_id=19, github_repo="fixture/site",
            branch="main", app_root="", blockers=[], organization=SimpleNamespace(domain="fixture.test"))
        identifier = uuid4()
        self.run.run_request.update(website_connection_id=str(website.pk), operation_id=str(identifier))
        op = SimpleNamespace(pk=identifier, state="failed", payload={"attempt": 2}, save=Mock())
        with patch.object(authority, "authority_guard", return_value=nullcontext(website)), \
                patch.object(authority, "extend_owner_operation_contract"), \
                patch.object(operations.WebsiteConnectionOperation.objects, "select_for_update") as rows:
            rows.return_value.get.return_value = op
            fields = operations.advance_workflow_attempt(self.run)
        self.assertEqual(fields["operation_attempt"], 3)
        self.assertEqual(self.run.run_request["client_request_id"], "original-charge")
        self.assertNotIn("app_root", self.run.run_request)
        self.assertNotIn("branch", self.run.run_request)

    def test_worker_snapshot_cannot_replace_original_payment_request_key(self):
        from .service_views import _merge_django_owned_run_request
        from .editorial_run_state import EditorialRunConflict
        self.assertEqual(_merge_django_owned_run_request({"client_request_id": "paid-original"}, {}),
            {"client_request_id": "paid-original"})
        with self.assertRaises(EditorialRunConflict):
            _merge_django_owned_run_request({"client_request_id": "paid-original"}, {"client_request_id": "new-key"})

    def test_stall_deadline_with_unknown_effect_never_calls_legacy_failure_refund(self):
        from datetime import timedelta
        from . import reconciliation, service_views
        self.run.status = "running"
        self.run.updated_at = timezone.now() - timedelta(minutes=20)
        self.run.result["reconciliation_recovery"] = {"requested_at": (timezone.now() - timedelta(minutes=10)).isoformat()}
        receipt = self.reseal({**self.receipt, "outcome": "unknown", "delivered_value": None})
        with patch.object(reconciliation.transaction, "atomic", return_value=nullcontext()), \
                patch.object(reconciliation.ContentFactoryRun.objects, "select_for_update") as locked, \
                patch.object(reconciliation.ContentFactoryRun.objects, "filter") as rows, \
                patch.object(reconciliation.requests, "post", return_value=SimpleNamespace(status_code=202,
                    json=lambda: {"article_cancellation_v1": receipt})) as remote, \
                patch.object(reconciliation, "_finalize_run_failure") as fail, \
                patch.object(service_views, "_serialize_content_factory_run", side_effect=lambda run: {"status": run.status}), \
                patch("content_factory.website_operations.observe_workflow_status"):
            locked.return_value.filter.return_value.first.return_value = self.run
            rows.return_value.update.return_value = 1
            outcome = reconciliation.recover_stalled_run(self.run, base_url="https://worker.fixture.test", headers={}, now=timezone.now())
        fail.assert_not_called()
        from .website_cancellation_retirement import expected_article_cancellation
        self.assertEqual(remote.call_args.kwargs["json"]["expected_cancellation_v1"], expected_article_cancellation(self.run))
        self.assertEqual(outcome, "cancelled_stalled")
        self.assertEqual(self.run.status, "cancelled")
        self.assertTrue(self.run.result["reconciliation_required"])
        self.assertNotIn("reconciliation_refund_pending", self.run.result)

    def stalled_stop_store(self, attempts=0):
        self.run.status = "running"
        self.run.updated_at = timezone.now()
        self.run.result["article_stop_attempts"] = attempts
        saved = deepcopy({key: getattr(self.run, key) for key in ("pk", "status", "updated_at", "result")})

        def select(**conditions):
            expected = deepcopy(conditions)

            def update(**changes):
                if any(saved.get(key) != value for key, value in expected.items()):
                    return 0
                saved.update(deepcopy(changes))
                return 1

            return SimpleNamespace(update=update)

        return saved, select

    def test_overlapping_stalled_stops_claim_only_one_attempt_without_renewing_heartbeat(self):
        from . import reconciliation
        saved, select = self.stalled_stop_store()
        duplicate = []

        def stop(*args, **kwargs):
            if not duplicate:
                duplicate.append("in_flight")
                duplicate[0] = reconciliation._stop_stalled_article(self.run,
                    base_url="https://worker.fixture.test", headers={}, now=timezone.now())
            return SimpleNamespace(status_code=202, json=lambda: {})

        with patch.object(reconciliation.ContentFactoryRun.objects, "filter", side_effect=select), \
                patch.object(reconciliation.requests, "post", side_effect=stop) as remote:
            outcome = reconciliation._stop_stalled_article(self.run,
                base_url="https://worker.fixture.test", headers={}, now=timezone.now())
        self.assertEqual(outcome, "recovery_unconfirmed")
        self.assertEqual(duplicate, ["callback_won"])
        remote.assert_called_once()
        self.assertEqual(saved["result"]["article_stop_attempts"], 1)
        self.assertEqual(saved["updated_at"], self.run.updated_at)

    def test_final_stalled_stop_does_not_overwrite_evidence_recorded_during_cancel(self):
        from . import reconciliation
        saved, select = self.stalled_stop_store(attempts=2)
        observed = {"purpose": "article", "state": "observed", "head_sha": "d" * 40}

        def stop(*args, **kwargs):
            saved["result"]["publication_evidence_v1"] = observed
            return SimpleNamespace(status_code=202, json=lambda: {})

        with patch.object(reconciliation.ContentFactoryRun.objects, "filter", side_effect=select), \
                patch.object(reconciliation.requests, "post", side_effect=stop):
            outcome = reconciliation._stop_stalled_article(self.run,
                base_url="https://worker.fixture.test", headers={}, now=timezone.now())
        self.assertEqual(saved["result"]["publication_evidence_v1"], observed)
        self.assertEqual(saved["status"], "running")
        self.assertEqual(saved["result"]["article_stop_attempts"], 3)
        self.assertEqual(saved["updated_at"], self.run.updated_at)
        self.assertEqual(outcome, "callback_won")

    def test_later_unknown_receipt_quarantines_pending_credit_without_losing_obligation(self):
        from . import reconciliation
        self.cancel()
        later = self.reseal({**self.receipt, "state_version": 10, "outcome": "unknown", "delivered_value": None})
        self.cancel(later)
        with patch.object(reconciliation.transaction, "atomic", return_value=nullcontext()), \
                patch("organizations.models.Organization.objects.select_for_update"), \
                patch.object(reconciliation.ContentFactoryRun.objects, "select_for_update") as locked, \
                patch("content_factory.billing_lineage.original_article_spend_for_run") as resolve, \
                patch("integrations.services.article_generation.refund_content_factory_request_for_user") as credit:
            locked.return_value.filter.return_value.first.return_value = self.run
            reconciliation._refund_failed_run(self.run, reason="test")
        resolve.assert_not_called()
        credit.assert_not_called()
        self.assertEqual(self.run.result["article_terminal_refund"]["status"], "needs_reconciliation")
        self.assertTrue(self.run.result["article_terminal_refund"]["paid_obligation"])
        self.assertTrue(self.run.result["reconciliation_refund_pending"])

    def test_unresolved_original_spend_remains_pending(self):
        from . import reconciliation
        self.cancel()
        with patch.object(reconciliation.transaction, "atomic", return_value=nullcontext()), \
                patch("organizations.models.Organization.objects.select_for_update"), \
                patch.object(reconciliation.ContentFactoryRun.objects, "select_for_update") as locked, \
                patch("content_factory.billing_lineage.original_article_spend_for_run", return_value=None), \
                self.assertRaises(ValueError):
            locked.return_value.filter.return_value.first.return_value = self.run
            reconciliation._refund_failed_run(self.run, reason="test")
        self.assertTrue(self.run.result["reconciliation_refund_pending"])
        self.assertEqual(self.run.result["article_terminal_refund"]["status"], "pending")

    def test_exact_resumed_cf_receipt_requires_current_attempt_and_original_payment(self):
        import json
        from pathlib import Path
        from .article_publication_evidence import cancellation_result
        fixture = json.loads((Path(__file__).parent / "test_fixtures/article-cancellation-resumed-no-value-v1.json").read_text())
        self.run.run_id = fixture["resumed_request"]["run_id"]
        self.run.result = {"generation": 0, "state_version": 1}
        self.run.run_request = {**fixture["initial_request"], "roo_points_cost": 6}
        with self.assertRaises(ValueError):
            cancellation_result(self.run, fixture["cancellation_response"])
        self.run.run_request = {**fixture["resumed_request"], "roo_points_cost": 6}
        result = cancellation_result(self.run, fixture["cancellation_response"])
        self.assertTrue(result["reconciliation_refund_pending"])
        self.assertEqual(result["article_terminal_refund"]["cancellation_receipt_id"],
            "e34dcdddf09fdcdb5e25f13831747842822cb5adcc9e3f6cfea95dc37f04e0d3")

    def test_terminal_callback_rechecks_new_effect_inside_shared_credit_lock(self):
        from integrations.services import article_generation
        from . import reconciliation
        job = SimpleNamespace(job_id=self.run.run_id, domain=self.run.domain, request_meta={},
            billing_status="charged", client_request_id="original-charge", billing_ledger_id=123)
        self.run.status = "failed"
        def publication_arrives(*args, **kwargs):
            self.run.result.update(publication_evidence_v1={**self.setup, "purpose": "article"},
                article_terminal_refund={"status": "pending", "paid_obligation": True}, reconciliation_refund_pending=True)
        with patch.object(reconciliation.transaction, "atomic", return_value=nullcontext()), \
                patch("organizations.models.Organization.objects.select_for_update"), \
                patch.object(views.ContentFactoryRun.objects, "filter") as rows, \
                patch.object(views.ContentFactoryRun.objects, "select_for_update") as locked, \
                patch.object(article_generation, "_record_terminal_article_refund", side_effect=publication_arrives), \
                patch("content_factory.billing_lineage.original_article_spend_for_run") as resolve, \
                patch.object(article_generation, "_refund_content_factory_request") as credit:
            rows.return_value.first.return_value = self.run
            locked.return_value.filter.return_value.first.return_value = self.run
            self.assertEqual(article_generation.maybe_auto_refund_terminal_failure(job,
                error_code="RUN_BUDGET_EXHAUSTED", error_message="Failed", refundable=True), (False, 0))
        resolve.assert_not_called()
        credit.assert_not_called()
        self.assertEqual(self.run.result["article_terminal_refund"]["status"], "needs_reconciliation")

    def test_unproved_terminal_failure_queues_stop_before_any_ledger_resolution(self):
        from integrations.services import article_generation
        from . import reconciliation
        job = SimpleNamespace(job_id=self.run.run_id, domain=self.run.domain, request_meta={},
            billing_status="charged", client_request_id="original-charge", billing_ledger_id=123)
        self.run.status = "failed"
        self.run.result = {"refundable": True, "error_code": "PUBLISH_FAILED"}
        with patch.object(reconciliation.transaction, "atomic", return_value=nullcontext()), \
                patch("organizations.models.Organization.objects.select_for_update"), \
                patch.object(views.ContentFactoryRun.objects, "filter") as rows, \
                patch.object(views.ContentFactoryRun.objects, "select_for_update") as locked, \
                patch("content_factory.billing_lineage.original_article_spend_for_run") as resolve:
            rows.return_value.first.return_value = self.run
            locked.return_value.filter.return_value.first.return_value = self.run
            self.assertEqual(article_generation.maybe_auto_refund_terminal_failure(job,
                error_code="PUBLISH_FAILED", error_message="Lost response", refundable=True), (False, 0))
        resolve.assert_not_called()
        self.assertEqual(self.run.result["article_terminal_refund"]["status"], "stop_required")
        self.assertTrue(self.run.result["reconciliation_refund_pending"])

    def test_failed_stop_required_uses_bounded_receipt_consumer(self):
        self.run.status = "failed"
        self.run.result.update(article_terminal_refund={"status": "stop_required", "paid_obligation": True},
            reconciliation_refund_pending=True)
        self.test_exhausted_sweep_obtains_real_stop_receipt_before_reserving_credit()
        self.assertEqual(self.run.result["article_terminal_refund"]["status"], "pending")

    def test_failed_stop_required_exhausts_into_quarantine_preserving_obligation(self):
        from . import reconciliation
        from datetime import timedelta
        self.run.status = "failed"
        self.run.result.update(article_terminal_refund={"status": "stop_required", "paid_obligation": True},
            reconciliation_refund_pending=True)
        self.run.updated_at, self.run.reconciled_at = timezone.now() - timedelta(hours=1), None
        rows = Mock()
        rows.filter.return_value = rows
        rows.order_by.return_value = rows
        rows.__getitem__ = Mock(return_value=[self.run])
        def claim(**changes):
            self.run.result, self.run.reconciled_at = changes["result"], changes["reconciled_at"]
            return 1
        rows.update.side_effect = claim
        with patch.object(reconciliation.ContentFactoryRun.objects, "filter", return_value=rows), \
                patch.object(views, "_call_content_factory_run_action", return_value={"status": "unknown"}) as remote:
            for _ in range(5):
                reconciliation._settle_exhausted_article_runs(limit=10, now=timezone.now())
        self.assertEqual(remote.call_count, 3)
        self.assertEqual(self.run.result["article_terminal_refund"]["status"], "needs_reconciliation")
        self.assertTrue(self.run.result["reconciliation_refund_pending"])
        self.assertTrue(self.run.result["article_terminal_refund"]["paid_obligation"])

    def test_actual_failed_publisher_snapshot_and_later_generic_updates_cannot_credit(self):
        import json
        from pathlib import Path
        from .service_views import _merge_django_owned_run_result
        from integrations.services import article_generation
        fixture = json.loads((Path(__file__).parent / "test_fixtures/article-publication-failed-pending-v1.json").read_text())
        self.run.run_id = fixture["original_request"]["run_id"]
        self.run.run_request = fixture["original_request"]
        self.run.status = "failed"
        original = fixture["failed_snapshot"]["result"]
        job = SimpleNamespace(job_id=self.run.run_id, domain=self.run.domain, request_meta={},
            billing_status="charged", client_request_id="paid-original-request", billing_ledger_id=123)
        for snapshot in (original, {"error_code": "PUBLISH_FAILED", "refundable": True},
                {"publication_evidence_v1": {**original["publication_evidence_v1"], "purpose": "setup", "state": "observed"}}):
            self.run.result = _merge_django_owned_run_result(original, snapshot)
            with patch.object(views.ContentFactoryRun.objects, "filter") as rows, \
                    patch.object(article_generation, "_record_terminal_article_refund") as reserve, \
                    patch("content_factory.reconciliation._refund_failed_run") as credit:
                rows.return_value.first.return_value = self.run
                self.assertEqual(article_generation.maybe_auto_refund_terminal_failure(job,
                    error_code="PUBLISH_FAILED", error_message="Lost provider response", refundable=True), (False, 0))
            reserve.assert_not_called()
            credit.assert_not_called()
            self.assertEqual(self.run.result["publication_evidence_v1"], original["publication_evidence_v1"])

    def test_partial_snapshot_preserves_exact_scope_goal_and_initial_delivery_consent(self):
        from .service_views import _merge_django_owned_run_request
        from .editorial_run_state import EditorialRunConflict
        original = {**self.run.run_request, "strategy_goal_id": "goal-7", "goal_budget": {"goal_id": "goal-7", "max_attempts": 3},
            "delivery_mode": "publish_code", "requested_delivery_mode": "publish_code", "delivery_mode_confirmed": True}
        merged = _merge_django_owned_run_request(original, {"keyword": "updated", "app_root": "", "branch": "main"})
        for key, value in original.items():
            self.assertEqual(merged[key], value)
        self.assertNotIn("app_root", merged)
        self.assertNotIn("branch", merged)
        for field, value in (("expected_source_sha", "b" * 40), ("github_repo", "fixture/other"),
                ("operation_attempt", 3), ("deletion_epoch", False), ("strategy_goal_id", "goal-other"),
                ("goal_budget", {}), ("delivery_mode", "content_only"), ("requested_delivery_mode", "content_only"), ("delivery_mode_confirmed", 1)):
            with self.subTest(field=field), self.assertRaises(EditorialRunConflict):
                _merge_django_owned_run_request(original, {field: value})

    def test_repeat_terminal_failure_cannot_reopen_settled_or_quarantined_credit(self):
        from integrations.services.article_generation import _record_terminal_article_refund
        job = SimpleNamespace(job_id=self.run.run_id, domain=self.run.domain)
        for status in ("refunded", "not_required", "needs_reconciliation", "stop_required", "deferred_to_goal"):
            self.run.result = {"article_terminal_refund": {"status": status, "paid_obligation": True}}
            with patch.object(views.transaction, "atomic", return_value=nullcontext()), \
                    patch.object(views.ContentFactoryRun.objects, "select_for_update") as rows:
                rows.return_value.filter.return_value.first.return_value = self.run
                _record_terminal_article_refund(job, status="pending", error_code="FAILED")
            self.assertEqual(self.run.result["article_terminal_refund"]["status"], status)

    def test_credit_recheck_does_not_restart_exhausted_stop_attempts(self):
        from . import reconciliation
        self.run.status = "failed"
        self.run.result = {"article_settlement_attempts": 3,
            "article_terminal_refund": {"status": "needs_reconciliation", "paid_obligation": True},
            "reconciliation_refund_pending": True}
        with patch.object(reconciliation.transaction, "atomic", return_value=nullcontext()), \
                patch("organizations.models.Organization.objects.select_for_update"), \
                patch.object(views.ContentFactoryRun.objects, "select_for_update") as locked, \
                patch("content_factory.billing_lineage.original_article_spend_for_run") as resolve:
            locked.return_value.filter.return_value.first.return_value = self.run
            reconciliation._refund_failed_run(self.run, reason="repeated callback")
        resolve.assert_not_called()
        self.assertEqual(self.run.result["article_terminal_refund"]["status"], "needs_reconciliation")
        self.assertEqual(self.run.result["article_settlement_attempts"], 3)

    def test_old_child_refund_obligation_cannot_credit_the_goal_owned_spend(self):
        from . import reconciliation
        self.run.run_request["first_article_goal_operation_id"] = "prepare-7"
        self.run.result.update(article_terminal_refund={"status": "pending", "paid_obligation": True},
            reconciliation_refund_pending=True)
        self.cancel()
        with patch.object(reconciliation.transaction, "atomic", return_value=nullcontext()), \
                patch("organizations.models.Organization.objects.select_for_update"), \
                patch.object(views.ContentFactoryRun.objects, "select_for_update") as locked, \
                patch("content_factory.billing_lineage.original_article_spend_for_run") as resolve:
            locked.return_value.filter.return_value.first.return_value = self.run
            reconciliation._refund_failed_run(self.run, reason="late child settlement")
        resolve.assert_not_called()
        self.assertEqual(self.run.result["article_terminal_refund"]["status"], "deferred_to_goal")
        self.assertTrue(self.run.result["article_terminal_refund"]["paid_obligation"])
        self.assertTrue(self.run.result["reconciliation_refund_pending"])
        self.assertEqual(self.run.result["article_settlement_owner"], "first_article_goal")

    def deny_before_delivery(self):
        """Production shape: the first repository read refused after a merge moved the source."""
        refusals = [{"at": "2026-10-20T03:20:33+00:00", "code": "website_source_changed"}] * 5
        self.run.status, self.run.current_step = "denied", "scan_repository"
        self.run.error = "Website connection is disconnected, changed or no longer authorized."
        self.run.run_request["roo_points_billing_status"] = "charged"
        self.run.result = {"generation": 1, "state_version": 8, "status": "denied", "failure": {},
            "error_code": "website_source_changed", "authority_refusals": refusals}

    def queue_denied(self, *, now=None):
        from . import reconciliation
        from datetime import timedelta
        self.run.updated_at, self.run.reconciled_at = timezone.now() - timedelta(hours=1), None
        rows = Mock()
        rows.filter.return_value = rows
        rows.order_by.return_value = rows
        rows.__getitem__ = Mock(return_value=[self.run])
        with patch.object(reconciliation.ContentFactoryRun.objects, "filter", return_value=rows) as select, \
                patch.object(reconciliation.ContentFactoryRun.objects, "select_for_update") as locked, \
                patch.object(reconciliation.transaction, "atomic", return_value=nullcontext()), \
                patch("organizations.models.Organization.objects.select_for_update"), \
                patch("content_factory.billing_lineage.original_article_spend_for_run") as resolve:
            locked.return_value.filter.return_value.first.return_value = self.run
            queued = reconciliation._queue_denied_article_refunds(limit=10, now=now or timezone.now())
        resolve.assert_not_called()
        self.assertEqual(select.call_args_list[0].kwargs["status"], "denied")
        return queued

    def test_website_source_changed_failure_reaches_no_value_settlement(self):
        from integrations.services import article_generation
        job = SimpleNamespace(job_id=self.run.run_id, domain=self.run.domain, request_meta={},
            billing_status="charged", client_request_id="original-charge", billing_ledger_id=123)
        for code in ("website_source_changed", "SOURCE_CHANGED"):
            with self.subTest(code=code), patch.object(views.ContentFactoryRun.objects, "filter") as rows, \
                    patch.object(article_generation, "_record_terminal_article_refund") as reserve, \
                    patch("content_factory.reconciliation._refund_failed_run",
                        return_value=SimpleNamespace(pk=222, delta_microroo=6_000_000)) as credit:
                rows.return_value.first.return_value = self.run
                self.assertEqual(article_generation.maybe_auto_refund_terminal_failure(job,
                    error_code=code, error_message="The repository changed", refundable=False), (True, 6))
            credit.assert_called_once()
            self.assertEqual(reserve.call_args.kwargs["status"], "pending")

    def test_denied_callback_queues_worker_stop_instead_of_quarantine(self):
        from integrations.services import article_generation
        from . import reconciliation
        self.deny_before_delivery()
        job = SimpleNamespace(job_id=self.run.run_id, domain=self.run.domain, request_meta={},
            billing_status="charged", client_request_id="original-charge", billing_ledger_id=123)
        with patch.object(reconciliation.transaction, "atomic", return_value=nullcontext()), \
                patch("organizations.models.Organization.objects.select_for_update"), \
                patch.object(views.ContentFactoryRun.objects, "filter") as rows, \
                patch.object(views.ContentFactoryRun.objects, "select_for_update") as locked, \
                patch("content_factory.billing_lineage.original_article_spend_for_run") as resolve:
            rows.return_value.first.return_value = self.run
            locked.return_value.filter.return_value.first.return_value = self.run
            self.assertEqual(article_generation.maybe_auto_refund_terminal_failure(job,
                error_code="website_source_changed", error_message="Changed", refundable=False), (False, 0))
        resolve.assert_not_called()
        self.assertEqual(self.run.result["article_terminal_refund"]["status"], "stop_required")
        self.assertTrue(self.run.result["reconciliation_refund_pending"])

    def test_denied_refusal_without_callback_reserves_obligation_and_requires_stop(self):
        from .article_publication_evidence import exhausted_article_candidate
        self.deny_before_delivery()
        self.assertEqual(self.queue_denied(), 1)
        receipt = self.run.result["article_terminal_refund"]
        self.assertEqual(receipt["status"], "stop_required")
        self.assertTrue(receipt["paid_obligation"])
        self.assertEqual(receipt["error_code"], "WEBSITE_SOURCE_CHANGED")
        self.assertTrue(self.run.result["reconciliation_refund_pending"])
        self.assertEqual(self.run.status, "denied")
        self.assertTrue(exhausted_article_candidate(self.run))
        # A repeated sweep, or one that loses to a callback, cannot reopen the obligation.
        self.run.save.reset_mock()
        self.assertEqual(self.queue_denied(), 0)
        self.run.save.assert_not_called()

    def test_denied_refusal_with_content_factorys_default_code_owes_its_charge_back(self):
        # Content Factory names a refusal CONNECTION_AUTHORIZATION_FAILED when this
        # backend refused without a code; such runs kept their charge (29 Oct 2026 audit).
        from .article_publication_evidence import denied_refund_code
        self.deny_before_delivery()
        self.run.result.update(error_code="CONNECTION_AUTHORIZATION_FAILED",
            authority_refusals=[{"at": "2026-10-20T03:20:33+00:00", "code": "CONNECTION_AUTHORIZATION_FAILED"}])
        self.assertEqual(denied_refund_code(self.run), "CONNECTION_AUTHORIZATION_FAILED")
        self.assertEqual(self.queue_denied(), 1)
        receipt = self.run.result["article_terminal_refund"]
        self.assertEqual((receipt["status"], receipt["error_code"]), ("stop_required", "CONNECTION_AUTHORIZATION_FAILED"))

    def test_denied_refusal_with_effect_delivery_other_code_or_no_charge_keeps_charge(self):
        from .article_publication_evidence import denied_refund_code
        self.deny_before_delivery()
        denied = deepcopy({"result": self.run.result, "run_request": self.run.run_request})
        cases = (
            ("article", {"publication_evidence_v1": {**self.setup, "purpose": "article", "state": "observed"}}, {}),
            ("unexplained_pr", {"pr_url": "https://github.com/fixture/site/pull/41"}, {}),
            ("handoff", {"publish_child_run_id": "publisher-child"}, {}),
            ("reviewed_preview", {"preview_content_verified": True, "preview_url": "https://preview.fixture.test/a"}, {}),
            ("draft_body", {"markdown": "Saved reviewable draft"}, {}),
            ("other_code", {"error_code": "website_connection_required"}, {}),
            ("unpaid", {}, {"roo_points_cost": None, "roo_points_ledger_id": None, "roo_points_billing_status": "free"}),
            ("goal_child", {}, {"first_article_goal_operation_id": "prepare-7"}),
        )
        for label, result, request in cases:
            with self.subTest(label):
                self.run.result = {**deepcopy(denied["result"]), **result}
                self.run.run_request = {**deepcopy(denied["run_request"]), **request}
                self.assertIsNone(denied_refund_code(self.run))
                self.run.save.reset_mock()
                self.assertEqual(self.queue_denied(), 0)
                self.run.save.assert_not_called()
                self.assertNotIn("article_terminal_refund", self.run.result)

    def test_denied_stop_receipt_settles_through_original_spend(self):
        from . import reconciliation
        self.deny_before_delivery()
        self.queue_denied()
        # The worker's bound stop is the only no-value proof; the same consumer as failed runs.
        self.test_exhausted_sweep_obtains_real_stop_receipt_before_reserving_credit()
        self.assertEqual(self.run.result["article_terminal_refund"]["status"], "pending")
        self.assertTrue(self.run.result["article_terminal_refund"]["paid_obligation"])
        spend = SimpleNamespace(user=SimpleNamespace(pk=5), idempotency_key="content_factory:charge:original-charge",
            created_by_slack_id="U-payer")
        with patch.object(reconciliation.transaction, "atomic", return_value=nullcontext()), \
                patch("organizations.models.Organization.objects.select_for_update"), \
                patch.object(views.ContentFactoryRun.objects, "select_for_update") as locked, \
                patch("content_factory.billing_lineage.original_article_spend_for_run", return_value=spend), \
                patch("integrations.services.article_generation.refund_content_factory_request_for_user",
                    return_value=SimpleNamespace(pk=222)) as credit:
            locked.return_value.filter.return_value.first.return_value = self.run
            reconciliation._refund_failed_run(self.run, reason="Refused before delivery")
        credit.assert_called_once()
        self.assertEqual(credit.call_args.kwargs["article_request"]["client_request_id"], "original-charge")
        self.assertEqual(self.run.result["article_terminal_refund"]["status"], "refunded")
        self.assertEqual(self.run.result["article_terminal_refund"]["refund_ledger_id"], "222")
        self.assertFalse(self.run.result["reconciliation_refund_pending"])

    def test_denied_stop_that_never_seals_quarantines_with_obligation(self):
        self.deny_before_delivery()
        self.queue_denied()
        self.assert_unsealed_stops_quarantine()

    def assert_unsealed_stops_quarantine(self):
        from . import reconciliation
        from datetime import timedelta
        self.run.updated_at, self.run.reconciled_at = timezone.now() - timedelta(hours=1), None
        rows = Mock()
        rows.filter.return_value = rows
        rows.order_by.return_value = rows
        rows.__getitem__ = Mock(return_value=[self.run])
        def claim(**changes):
            self.run.result, self.run.reconciled_at = changes["result"], changes["reconciled_at"]
            return 1
        rows.update.side_effect = claim
        with patch.object(reconciliation.ContentFactoryRun.objects, "filter", return_value=rows) as select, \
                patch.object(views, "_call_content_factory_run_action", return_value={"status": "unknown"}) as remote:
            for _ in range(5):
                reconciliation._settle_exhausted_article_runs(limit=10, now=timezone.now())
        self.assertIn(self.run.status, select.call_args_list[0].kwargs["status__in"])
        self.assertEqual(remote.call_count, 3)
        self.assertEqual(remote.call_args.kwargs["payload"]["reason"], "article_terminal_no_value")
        self.assertEqual(self.run.result["article_terminal_refund"]["status"], "needs_reconciliation")
        self.assertTrue(self.run.result["article_terminal_refund"]["paid_obligation"])
        self.assertTrue(self.run.result["reconciliation_refund_pending"])

    def test_refund_sweep_retries_a_denied_obligation(self):
        from . import reconciliation
        self.deny_before_delivery()
        self.run.result.update(article_terminal_refund={"status": "pending", "paid_obligation": True},
            reconciliation_refund_pending=True)
        rows = Mock()
        rows.filter.return_value = rows
        rows.__getitem__ = Mock(return_value=[self.run])
        with patch.object(reconciliation.ContentFactoryRun.objects, "filter", return_value=rows) as select, \
                patch.object(reconciliation, "_refund_failed_run") as settle:
            reconciliation._settle_sweep_refunds(limit=10)
        self.assertIn("denied", select.call_args.kwargs["status__in"])
        settle.assert_called_once_with(self.run, reason="run_reconciliation_failed")


class DispatchRejectedRefundTests(SimpleTestCase):
    """PawShare run 1433c2cf (29 Oct 2026): the backend refused Content Factory's first snapshot.

    Content Factory failed the run locally with "Dispatch rejected by backend"
    and sent no terminal callback, so its 12-point charge was never reserved.
    """

    KEY = "vibe-article-job:221caeaa"

    def setUp(self):
        self.run = SimpleNamespace(pk=31, run_id="1433c2cf-pawshare", organization_id=124, domain="pawshare.test",
            workflow="article_generation", github_repo="", status="failed", current_step="dispatch_rejected",
            approval_state="not_required", resume_available=False, acceptance_summary={}, verification_summary={},
            error="Dispatch rejected by backend: editorial_run_conflict", save=Mock(), run_request={
                "client_request_id": self.KEY, "domain": "pawshare.test", "github_repo": "",
                "delivery_mode": "content_only", "delivery_mode_confirmed": True,
                "roo_points_cost": 12, "roo_points_ledger_id": "5474", "roo_points_billing_status": "charged"},
            # What a status poll records from Content Factory's local receipt.
            result={"status": "failed", "error_code": "dispatch_rejected", "backend_code": "editorial_run_conflict"})
        # Content Factory's GET /api/runs/lookup reply for the refused key.
        self.key_record = {"client_request_id": self.KEY, "run_id": self.run.run_id, "job_id": self.run.run_id,
            "key_status": "failed", "workflow": "direct_generate", "endpoint": "article", "domain": "pawshare.test",
            "attempt": 1, "error": "backend_rejected:editorial_run_conflict", "run_status": "failed", "status": "failed"}

    def queue(self, *, lookup=None, lookup_error=None, child_exists=False):
        from datetime import timedelta
        from . import reconciliation
        self.run.updated_at, self.run.reconciled_at = timezone.now() - timedelta(hours=1), None
        rows = Mock()
        rows.filter.return_value = rows
        rows.order_by.return_value = rows
        rows.exists.return_value = child_exists
        rows.__getitem__ = Mock(return_value=[self.run])
        body = self.key_record if lookup is None else lookup
        response = SimpleNamespace(status_code=body.pop("_status", 200), content=b"{}", json=lambda: body)
        spend = SimpleNamespace(user=SimpleNamespace(pk=5), idempotency_key=f"content_factory:charge:{self.KEY}",
            created_by_slack_id="U-payer")
        with patch.object(reconciliation.ContentFactoryRun.objects, "filter", return_value=rows) as select, \
                patch.object(reconciliation.ContentFactoryRun.objects, "select_for_update") as locked, \
                patch.object(reconciliation.transaction, "atomic", return_value=nullcontext()), \
                patch("organizations.models.Organization.objects.select_for_update"), \
                patch.object(views, "_content_factory_remote_config", return_value={"enabled": True, "base_url": "https://cf.test"}), \
                patch.object(views.http_client, "get", return_value=response, side_effect=lookup_error) as remote, \
                patch("content_factory.billing_lineage.original_article_spend_for_run", return_value=spend), \
                patch("integrations.services.article_generation.refund_content_factory_request_for_user",
                    return_value=SimpleNamespace(pk=6001)) as credit:
            locked.return_value.filter.return_value.first.return_value = self.run
            queued = reconciliation._queue_dispatch_rejected_article_refunds(limit=10, now=timezone.now())
        return SimpleNamespace(queued=queued, select=select, rows=rows, remote=remote, credit=credit)

    def test_refused_first_snapshot_has_no_settlement_without_key_proof(self):
        from .article_publication_evidence import refund_reconciliation_reason
        # The stop is the only proof today, and the worker cannot bind a
        # scope the backend never merged, so the obligation stays stranded.
        self.assertEqual(refund_reconciliation_reason(self.run), "stop_required")

    def test_refused_first_snapshot_credits_original_spend_once_key_proves_refusal(self):
        outcome = self.queue()
        self.assertEqual(outcome.queued, 1)
        selection = outcome.select.call_args_list[0].kwargs
        self.assertEqual((selection["status"], selection["result__error_code"]), ("failed", "dispatch_rejected"))
        self.assertTrue(selection["result__article_terminal_refund__isnull"])
        self.assertEqual(outcome.remote.call_args.kwargs["params"], {"client_request_id": self.KEY})
        outcome.credit.assert_called_once()
        self.assertEqual(outcome.credit.call_args.kwargs["article_request"]["client_request_id"], self.KEY)
        self.assertEqual(outcome.credit.call_args.kwargs["user"].pk, 5)
        receipt = self.run.result["article_terminal_refund"]
        self.assertEqual((receipt["status"], receipt["refund_ledger_id"]), ("refunded", "6001"))
        self.assertTrue(receipt["paid_obligation"])
        self.assertEqual(receipt["settlement_reason"], "dispatch_rejected_without_delivery")
        self.assertFalse(self.run.result["reconciliation_refund_pending"])
        proof = self.run.result["dispatch_rejection_v1"]
        self.assertEqual((proof["run_id"], proof["client_request_id"], proof["backend_code"]),
            (self.run.run_id, self.KEY, "editorial_run_conflict"))
        # A settled obligation is never selected again.
        self.run.save.reset_mock()
        self.assertEqual(self.queue().queued, 0)
        self.run.save.assert_not_called()

    def test_unproved_key_record_keeps_charge_for_reconciliation(self):
        cases = (
            # A later attempt reclaimed the key and started another run.
            ("reclaimed", {**self.key_record, "key_status": "dispatched", "run_id": "replacement-run", "error": None}),
            ("other_run", {**self.key_record, "run_id": "other-run"}),
            ("not_backend_refusal", {**self.key_record, "error": "article_admission:INTERNAL_ERROR"}),
            ("worker_not_failed", {**self.key_record, "run_status": "running"}),
            ("never_received", {"detail": "No dispatch for client_request_id", "_status": 404}),
        )
        for label, lookup in cases:
            with self.subTest(label):
                self.setUp()
                outcome = self.queue(lookup=deepcopy(lookup))
                self.assertEqual(outcome.queued, 1)
                outcome.credit.assert_not_called()
                receipt = self.run.result["article_terminal_refund"]
                self.assertEqual((receipt["status"], receipt["settlement_reason"]),
                    ("needs_reconciliation", "dispatch_rejection_unconfirmed"))
                self.assertTrue(receipt["paid_obligation"])
                self.assertTrue(self.run.result["reconciliation_refund_pending"])
                self.assertNotIn("dispatch_rejection_v1", self.run.result)

    def test_unreachable_worker_retries_later_without_reserving(self):
        outcome = self.queue(lookup_error=views.http_client.RequestException("timeout"))
        self.assertEqual(outcome.queued, 0)
        outcome.credit.assert_not_called()
        self.run.save.assert_not_called()
        self.assertNotIn("article_terminal_refund", self.run.result)
        self.assertIn("reconciled_at", outcome.rows.update.call_args.kwargs)

    def test_reused_charge_effect_delivery_or_settled_payment_keeps_charge(self):
        cases = (
            ("restart_pending", {"restart_receipt": {"state": "pending", "reused_authorization": "reserved"}}, {}),
            ("restart_completed", {"restart_receipt": {"state": "completed"}, "restart_child_run_id": "child"}, {}),
            ("unexplained_pr", {"pr_url": "https://github.com/pawshare/site/pull/3"}, {}),
            ("draft_body", {"markdown": "Saved reviewable draft"}, {}),
            ("packaged", {"content_package": {"content_packaged": True}}, {}),
            ("other_failure", {"error_code": "editorial_run_conflict"}, {}),
            ("unpaid", {}, {"roo_points_cost": None, "roo_points_ledger_id": None, "roo_points_billing_status": "free"}),
            ("refunded", {}, {"roo_points_billing_status": "refunded"}),
            ("dispatch_refund_owned", {}, {"pending_billing_refund": {"kind": "article_generation"}}),
            ("goal_child", {}, {"first_article_goal_operation_id": "prepare-7"}),
        )
        for label, result, request in cases:
            with self.subTest(label):
                self.setUp()
                self.run.result.update(result)
                self.run.run_request.update(request)
                outcome = self.queue()
                self.assertEqual(outcome.queued, 0)
                outcome.remote.assert_not_called()
                outcome.credit.assert_not_called()
                self.run.save.assert_not_called()
                self.assertNotIn("article_terminal_refund", self.run.result)
        with self.subTest("token_keyed"):
            self.setUp()
            self.run.run_id = self.KEY
            self.assertEqual(self.queue().queued, 0)
        with self.subTest("child_reused_charge"):
            # A child run bills through this run's charge.
            self.setUp()
            outcome = self.queue(child_exists=True)
            self.assertEqual(outcome.queued, 0)
            outcome.credit.assert_not_called()
            self.run.save.assert_not_called()
            self.assertNotIn("article_terminal_refund", self.run.result)

    def test_forged_or_mismatched_rejection_proof_never_credits(self):
        from .article_publication_evidence import refund_reconciliation_reason
        valid = {"version": 1, "run_id": self.run.run_id, "client_request_id": self.KEY, "key_status": "failed",
            "backend_code": "editorial_run_conflict", "checked_at": "2026-10-29T05:00:00+00:00"}
        self.run.result["dispatch_rejection_v1"] = valid
        self.assertIsNone(refund_reconciliation_reason(self.run))
        for label, change in (("run", {"run_id": "other"}), ("key", {"client_request_id": "other-key"}),
                ("status", {"key_status": "dispatched"}), ("code", {"backend_code": ""}),
                ("extra", {"amount": 12}), ("version", {"version": 2})):
            with self.subTest(label):
                self.run.result["dispatch_rejection_v1"] = {**valid, **change}
                self.assertEqual(refund_reconciliation_reason(self.run), "stop_required")
        self.run.result["dispatch_rejection_v1"] = valid
        self.run.result["publication_evidence_v1"] = {"purpose": "article", "state": "observed"}
        self.assertEqual(refund_reconciliation_reason(self.run), "needs_reconciliation")

    def test_status_poll_cannot_erase_or_invent_refund_work(self):
        owned = {"article_terminal_refund": {"status": "pending", "paid_obligation": True},
            "reconciliation_refund_pending": True, "dispatch_rejection_v1": {"version": 1}}
        remote = {"status": "failed", "error_code": "dispatch_rejected", "backend_code": "editorial_run_conflict"}
        merged = views._merge_django_owned_article_result({**self.run.result, **owned}, remote)
        self.assertEqual({key: merged.get(key) for key in owned}, owned)
        invented = views._merge_django_owned_article_result(self.run.result, {**remote,
            "article_terminal_refund": {"status": "refunded"}, "dispatch_rejection_v1": {"version": 1}})
        self.assertNotIn("article_terminal_refund", invented)
        self.assertNotIn("dispatch_rejection_v1", invented)

    def test_worker_snapshot_cannot_invent_dispatch_rejection_proof(self):
        from .service_views import _merge_django_owned_run_result
        proof = {"dispatch_rejection_v1": {"version": 1, "run_id": self.run.run_id}}
        self.assertEqual(_merge_django_owned_run_result({}, proof), {})
        self.assertEqual(_merge_django_owned_run_result(proof, {"dispatch_rejection_v1": None}), proof)

    def test_refund_sweep_retries_a_pending_dispatch_rejection(self):
        from . import reconciliation
        self.run.result.update(article_terminal_refund={"status": "pending", "paid_obligation": True},
            reconciliation_refund_pending=True)
        rows = Mock()
        rows.filter.return_value = rows
        rows.__getitem__ = Mock(return_value=[self.run])
        with patch.object(reconciliation.ContentFactoryRun.objects, "filter", return_value=rows) as select, \
                patch.object(reconciliation, "_refund_failed_run") as settle:
            reconciliation._settle_sweep_refunds(limit=10)
        self.assertIn("failed", select.call_args.kwargs["status__in"])
        settle.assert_called_once_with(self.run, reason="run_reconciliation_failed")


class UnrecoverableFailureRefundTests(SimpleTestCase):
    """PawShare run 9f479633 (29 Oct 2026): plan_article failed with CORPUS_INCOMPLETE.

    The failure needs a user action the run cannot offer: no resume, retry or
    scheduled recovery. Nothing reserved its 12-point charge, and the content-only
    draft's stop receipt could not bind its scope, so the charge stayed forever.
    """

    KEY = "vibe-article-job:9f479633"

    def setUp(self):
        self.run = SimpleNamespace(pk=41, run_id="9f479633-pawshare", organization_id=124, domain="pawshare.test",
            workflow="article_generation", github_repo="", status="failed", current_step="plan_article",
            approval_state="not_required", resume_available=False, acceptance_summary={}, verification_summary={},
            error="The saved operation requires the recorded recovery action.", save=Mock(), run_request={
                "client_request_id": self.KEY, "domain": "pawshare.test", "github_repo": "",
                "delivery_mode": "content_only", "delivery_mode_confirmed": True,
                "roo_points_cost": 12, "roo_points_ledger_id": "5481", "roo_points_billing_status": "charged"},
            result={"status": "failed", "generation": 0, "state_version": 4, "error_code": "CORPUS_INCOMPLETE",
                "requires_user_action": True, "retryable": False, "retry_available": False, "recovery": {},
                "recovery_intents": {}, "failure": {"version": 1, "code": "CORPUS_INCOMPLETE", "step": "plan_article",
                    "retryable": False, "requires_user_action": True, "retry_policy": "explicit_action",
                    "next_action": "restore_complete_production_corpus"}})

    def worker_receipt(self, **changes):
        """Content Factory's stop receipt: its request model records each unset scope key as null."""
        from hashlib import sha256
        from .website_failure_contract import canonical_bytes
        scope = {"website_connection_id": None, "connection_generation": None, "connection_target_id": None,
            "repository_id": None, "github_repo": None, "expected_source_sha": None, "operation_id": None,
            "operation_attempt": None, "deletion_epoch": None, "client_request_id": self.KEY}
        receipt = {"version": 1, "purpose": "article_cancellation", "run_id": self.run.run_id, "goal_id": None,
            "scope": {**scope, **changes.pop("scope", {})}, "generation": 0, "state_version": 5,
            "outcome": "no_article_effect", "cancelled": True, "delivered_value": False,
            "watermark": {"operation_id": None, "attempt": None, "cancelled": True}, **changes}
        receipt["receipt_id"] = sha256(canonical_bytes(receipt)).hexdigest()
        return receipt

    def queue(self, *, child_exists=False):
        from datetime import timedelta
        from . import reconciliation
        self.run.updated_at, self.run.reconciled_at = timezone.now() - timedelta(hours=1), None
        rows = Mock()
        rows.filter.return_value = rows
        rows.order_by.return_value = rows
        rows.exists.return_value = child_exists
        rows.__getitem__ = Mock(return_value=[self.run])
        with patch.object(reconciliation.ContentFactoryRun.objects, "filter", return_value=rows) as select, \
                patch.object(reconciliation.ContentFactoryRun.objects, "select_for_update") as locked, \
                patch.object(reconciliation.transaction, "atomic", return_value=nullcontext()), \
                patch("organizations.models.Organization.objects.select_for_update"), \
                patch("content_factory.billing_lineage.original_article_spend_for_run") as resolve:
            locked.return_value.filter.return_value.first.return_value = self.run
            queued = reconciliation._queue_unrecoverable_article_refunds(limit=10, now=timezone.now())
        resolve.assert_not_called()
        return SimpleNamespace(queued=queued, select=select)

    def stop(self, receipt):
        from datetime import timedelta
        from . import reconciliation, service_views
        self.run.updated_at, self.run.reconciled_at = timezone.now() - timedelta(hours=1), None
        rows = Mock()
        rows.filter.return_value = rows
        rows.order_by.return_value = rows
        rows.__getitem__ = Mock(return_value=[self.run])
        def claim(**changes):
            self.run.result, self.run.reconciled_at = changes["result"], changes["reconciled_at"]
            return 1
        rows.update.side_effect = claim
        with patch.object(reconciliation.ContentFactoryRun.objects, "filter", return_value=rows), \
                patch.object(reconciliation.ContentFactoryRun.objects, "select_for_update") as locked, \
                patch.object(reconciliation.transaction, "atomic", return_value=nullcontext()), \
                patch.object(views, "_call_content_factory_run_action", return_value={"article_cancellation_v1": receipt}) as remote, \
                patch.object(service_views, "_serialize_content_factory_run", side_effect=lambda run: {"status": run.status}), \
                patch("content_factory.website_operations.observe_workflow_status"):
            locked.return_value.filter.return_value.first.return_value = self.run
            settled = reconciliation._settle_exhausted_article_runs(limit=10, now=timezone.now())
        return SimpleNamespace(settled=settled, remote=remote)

    def credit(self):
        from . import reconciliation
        spend = SimpleNamespace(user=SimpleNamespace(pk=5), idempotency_key=f"content_factory:charge:{self.KEY}",
            created_by_slack_id="U-payer")
        with patch.object(reconciliation.transaction, "atomic", return_value=nullcontext()), \
                patch("organizations.models.Organization.objects.select_for_update"), \
                patch.object(views.ContentFactoryRun.objects, "select_for_update") as locked, \
                patch("content_factory.billing_lineage.original_article_spend_for_run", return_value=spend), \
                patch("integrations.services.article_generation.refund_content_factory_request_for_user",
                    return_value=SimpleNamespace(pk=6101)) as credit:
            locked.return_value.filter.return_value.first.return_value = self.run
            reconciliation._refund_failed_run(self.run, reason="run_reconciliation_failed")
        return credit

    def test_no_recovery_failure_reserves_credit_stops_worker_and_refunds_original_spend(self):
        from .article_publication_evidence import exhausted_article_candidate
        from .website_cancellation_retirement import expected_article_cancellation
        outcome = self.queue()
        self.assertEqual(outcome.queued, 1)
        selection = outcome.select.call_args_list[0].kwargs
        self.assertEqual((selection["status"], selection["resume_available"]), ("failed", False))
        self.assertTrue(selection["result__article_terminal_refund__isnull"])
        receipt = self.run.result["article_terminal_refund"]
        self.assertEqual((receipt["status"], receipt["error_code"], receipt["settlement_reason"]),
            ("stop_required", "CORPUS_INCOMPLETE", "failed_without_recovery"))
        self.assertTrue(receipt["paid_obligation"])
        self.assertTrue(exhausted_article_candidate(self.run))
        # The worker's stop is still the only no-delivery proof.
        stopped = self.stop(self.worker_receipt())
        self.assertEqual(stopped.settled, 1)
        payload = stopped.remote.call_args.kwargs["payload"]
        self.assertEqual(payload["reason"], "article_terminal_no_value")
        self.assertEqual(payload["expected_cancellation_v1"], expected_article_cancellation(self.run))
        self.assertEqual(self.run.status, "cancelled")
        self.assertEqual(self.run.result["article_terminal_refund"]["status"], "pending")
        self.assertTrue(self.run.result["reconciliation_refund_pending"])
        credit = self.credit()
        credit.assert_called_once()
        self.assertEqual(credit.call_args.kwargs["article_request"]["client_request_id"], self.KEY)
        self.assertEqual((self.run.result["article_terminal_refund"]["status"],
            self.run.result["article_terminal_refund"]["refund_ledger_id"]), ("refunded", "6101"))
        self.assertFalse(self.run.result["reconciliation_refund_pending"])
        # A settled obligation is never reserved again.
        self.run.save.reset_mock()
        self.assertEqual(self.queue().queued, 0)
        self.run.save.assert_not_called()

    def test_content_only_stop_receipt_binds_unset_scope_but_not_a_different_one(self):
        from .article_publication_evidence import cancellation_receipt
        self.assertEqual(cancellation_receipt(self.run, {"article_cancellation_v1": self.worker_receipt()})["outcome"],
            "no_article_effect")
        for label, scope in (("repository", {"github_repo": "pawshare/site"}), ("connection", {"website_connection_id": "c-1"}),
                ("explicit_root", {"app_root": ""}), ("other_key", {"client_request_id": "another-charge"})):
            with self.subTest(label), self.assertRaises(ValueError):
                cancellation_receipt(self.run, {"article_cancellation_v1": self.worker_receipt(scope=scope)})

    def test_recoverable_delivered_reused_or_unpaid_failure_keeps_charge(self):
        from .article_publication_evidence import unrecoverable_refund_code
        cases = (
            ("resumable", {}, {}, {"resume_available": True}),
            ("retryable", {"retryable": True, "failure": {**self.run.result["failure"], "retryable": True}}, {}, {}),
            ("no_user_action", {"requires_user_action": False,
                "failure": {**self.run.result["failure"], "requires_user_action": False}}, {}, {}),
            ("retry_offered", {"retry_available": True}, {}, {}),
            ("recovery_scheduled", {"recovery": {"state": "pending", "step": "plan_article"}}, {}, {}),
            ("recovery_intent", {"recovery_intents": {"plan_article": {"state": "pending"}}}, {}, {}),
            ("owner_action_code", {"failure": {**self.run.result["failure"], "code": "AUTH_REQUIRED"}}, {}, {}),
            ("repairable_code", {"failure": {**self.run.result["failure"], "code": "ARTICLE_SEED_SCAFFOLD_FAILED"}}, {}, {}),
            ("unregistered_code", {"failure": {**self.run.result["failure"], "code": "NOT_A_REGISTERED_CODE"}}, {}, {}),
            ("restart_pending", {"restart_receipt": {"state": "pending", "reused_authorization": "reserved"}}, {}, {}),
            ("restart_completed", {"restart_receipt": {"state": "completed"}, "restart_child_run_id": "child"}, {}, {}),
            ("draft_body", {"markdown": "Saved reviewable draft"}, {}, {}),
            ("packaged", {"content_package": {"content_packaged": True}}, {}, {}),
            ("unexplained_pr", {"pr_url": "https://github.com/pawshare/site/pull/3"}, {}, {}),
            ("unpaid", {}, {"roo_points_cost": None, "roo_points_ledger_id": None, "roo_points_billing_status": "free"}, {}),
            ("refunded", {}, {"roo_points_billing_status": "refunded"}, {}),
            ("dispatch_refund_owned", {}, {"pending_billing_refund": {"kind": "article_generation"}}, {}),
            ("goal_child", {}, {"first_article_goal_operation_id": "prepare-7"}, {}),
            ("blocked", {}, {}, {"status": "blocked"}),
        )
        for label, result, request, fields in cases:
            with self.subTest(label):
                self.setUp()
                self.run.result.update(result)
                self.run.run_request.update(request)
                for key, value in fields.items():
                    setattr(self.run, key, value)
                self.assertIsNone(unrecoverable_refund_code(self.run))
                self.assertEqual(self.queue().queued, 0)
                self.run.save.assert_not_called()
                self.assertNotIn("article_terminal_refund", self.run.result)
        with self.subTest("child_reused_charge"):
            # A replacement bills through this run's charge.
            self.setUp()
            self.assertEqual(self.queue(child_exists=True).queued, 0)
            self.run.save.assert_not_called()
            self.assertNotIn("article_terminal_refund", self.run.result)

    def test_restart_cannot_reuse_a_charge_the_refund_owns(self):
        self.run.result["article_terminal_refund"] = {"status": "stop_required", "paid_obligation": True}
        with patch.object(views, "_article_draft_title_keyword", return_value=("Dog beach playdates", "dog beach")):
            self.assertFalse(views._article_restart_available(self.run))
            for status in ("not_required", "needs_reconciliation"):
                # A stop that never seals must not strand the draft without a restart.
                self.run.result["article_terminal_refund"]["status"] = status
                self.assertTrue(views._article_restart_available(self.run))
        self.run.result["article_terminal_refund"]["status"] = "stop_required"
        with patch.object(views.transaction, "atomic", return_value=nullcontext()), \
                patch.object(views.Organization.objects, "select_for_update"), \
                patch.object(views.ContentFactoryRun.objects, "select_for_update") as locked:
            locked.return_value.get.return_value = self.run
            response = views._reserve_article_restart(run=self.run, context=SimpleNamespace(organization=SimpleNamespace(pk=124)),
                payload={"topic": "Dog beach playdates"}, receipt={"state": "pending"})
        self.assertEqual((response.status_code, response.data["code"]), (409, "roo_points_billing_refunded"))
        self.run.save.assert_not_called()
        self.assertNotIn("restart_receipt", self.run.result)

    def test_stop_cannot_credit_a_charge_a_restart_already_reused(self):
        from .article_publication_evidence import cancellation_result, refund_reconciliation_reason
        self.run.result["article_terminal_refund"] = {"status": "stop_required", "paid_obligation": True}
        self.run.result.update(restart_receipt={"state": "completed"}, restart_child_run_id="replacement")
        self.run.result = cancellation_result(self.run, {"article_cancellation_v1": self.worker_receipt()})
        self.run.status = "cancelled"
        self.assertEqual(refund_reconciliation_reason(self.run), "needs_reconciliation")
        credit = self.credit()
        credit.assert_not_called()
        self.assertEqual(self.run.result["article_terminal_refund"]["status"], "needs_reconciliation")
        self.assertTrue(self.run.result["reconciliation_refund_pending"])
