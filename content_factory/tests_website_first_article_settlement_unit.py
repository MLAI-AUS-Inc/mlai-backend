"""Real parent billing decisions with actual worker-produced stop receipts."""
import json
from datetime import timedelta
from contextlib import contextmanager, nullcontext
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase, override_settings

from . import website_first_article_billing as billing, website_first_article_refunds as refunds
from . import website_first_article_settlement as settlement, website_goal as goals, website_setup_consent as consent
from .article_publication_evidence import cancellation_result
from . import tests_website_first_article_billing_unit as fixtures
from .tests_website_setup_consent_unit import answers


@override_settings(ARTICLES_FIRST_ARTICLE_GOAL_ENABLED=True, ARTICLES_SETUP_CONSENT_ENABLED=True,
    ARTICLES_WEBSITE_GOAL_RUNNER_ENABLED=True, ARTICLES_WEBSITE_GOAL_ENROLLED_ORGANIZATIONS=["7"],
    ARTICLES_FAILURE_TAXONOMY_POLICY_ENABLED=True, ARTICLES_FAILURE_TAXONOMY_ENROLLED_ORGANIZATIONS=["7"])
class ParentSettlementTests(SimpleTestCase):
    reserve = fixtures.FirstArticleBillingTests.reserve

    def setUp(self):
        fixtures.FirstArticleBillingTests.setUp(self)
        self.connection.pk = "connection-original"
        self.connection.generation = 2
        self.connection.repository_id = 17
        self.connection.github_repo = "fixture/site"
        self.op.pk = "aade3210-6b5b-4e6e-9519-abcd22334455"
        self.op.generation = 2
        goal = goals.new_goal(self.op.pk, goals.scope_for(self.connection, 9), target="first_article_live", now=self.now)
        goal["consent"] = consent.seal(goal, answers(), user_id=4, now=self.now)
        self.op.receipt = {"goal": goal}
        self.ledger.reference_id = billing.request_key(goal)
        self.ledger.idempotency_key = "content_factory:charge:" + billing.request_key(goal)
        self.held = self.reserve()
        self.op.state = "needs_attention"
        self.refunds.return_value.first.return_value = self.ledger
        self.credit = self.stack.enter_context(patch("roo.services.PointsService.refund",
            return_value=(SimpleNamespace(id=22, pk=22), True)))
        self.stack.enter_context(patch("content_factory.models.ContentFactoryJob.objects.filter"))
        self.stack.enter_context(patch("workflow_runs.models.ContentFactoryRun.objects.filter"))
        self.children = []
        self.locked = self.stack.enter_context(patch("workflow_runs.models.ContentFactoryRun.objects.select_for_update"))
        def rows(*args, **kwargs):
            query = Mock()
            query.order_by.return_value = list(self.children)
            query.exclude.return_value.order_by.return_value = []
            return query
        self.locked.return_value.filter.side_effect = rows
        self.dispatch_rows = self.stack.enter_context(patch("content_factory.dispatch_models.ContentFactoryDispatchOutbox.objects.select_for_update"))
        self.dispatch_rows.return_value.filter.return_value.first.return_value = None
        parents = self.stack.enter_context(patch("content_factory.website_models.WebsiteConnectionOperation.objects.select_related"))
        parents.return_value.filter.return_value.first.return_value = self.op

    def child(self, name="article-goal-no-value-v1.json"):
        fixture = json.loads((Path(__file__).parent / "test_fixtures" / name).read_text())
        request = fixture["original_request"]
        self.assertEqual(request["client_request_id"], self.held["client_request_id"])
        child = SimpleNamespace(pk=21, run_id=request["run_id"], workflow="article_generation", organization_id=7,
            domain="fixture.test", github_repo="fixture/site", run_request=request, status="failed", result={},
            acceptance_summary={}, save=Mock())
        child.result = cancellation_result(child, fixture["cancellation_response"])
        child.status = "cancelled"
        self.children.append(child)
        self.op.receipt["first_article_run_id"] = child.run_id
        self.dispatch_rows.return_value.filter.return_value.first.return_value = SimpleNamespace(
            payload={"request": deepcopy(request)}, state="delivered", run_id=child.run_id, last_error="")
        return child

    def test_actual_pending_publication_receipt_prevents_parent_refund(self):
        self.child("article-goal-pending-publication-v1.json")
        billing.release(self.op, reason="Goal exhausted")
        self.credit.assert_not_called()
        self.assertEqual(self.op.receipt["first_article_billing"]["status"], "reserved")

    def test_actual_no_value_receipt_refunds_original_parent_once(self):
        self.child()
        billing.release(self.op, reason="Goal exhausted")
        billing.release(self.op, reason="Duplicate settlement")
        self.credit.assert_called_once()
        self.assertEqual(self.credit.call_args.kwargs["user"], self.payer)
        self.assertEqual(self.credit.call_args.kwargs["delta"], 6)
        self.assertEqual(self.credit.call_args.kwargs["original_spend_key"], self.ledger.idempotency_key)

    def test_lost_dispatch_response_never_means_no_child_effect(self):
        self.dispatch_rows.return_value.filter.return_value.first.return_value = SimpleNamespace(
            payload={"request": {"first_article_goal_operation_id": self.op.pk, "first_article_goal_id": self.op.pk,
                "client_request_id": self.held["client_request_id"]}}, state="delivering", run_id="", attempts=0, last_error="")
        billing.release(self.op, reason="Owner cancelled")
        self.credit.assert_not_called()

    def test_failed_status_without_stop_receipt_cannot_refund_parent(self):
        child = self.child()
        child.status = "failed"
        child.result = {"refundable": True}
        billing.release(self.op, reason="Generation failed")
        self.credit.assert_not_called()

    def test_missing_known_child_cannot_refund_parent(self):
        self.op.receipt["first_article_publish_run_id"] = "lost-publish-child"
        billing.release(self.op, reason="Goal stopped")
        self.credit.assert_not_called()

    def test_foreign_payment_proof_cannot_refund_parent(self):
        child = self.child()
        child.run_request["roo_points_ledger_id"] = "999"
        billing.release(self.op, reason="Goal stopped")
        self.credit.assert_not_called()

    def refund_row(self):
        row = SimpleNamespace(pk="refund", action="goal-refund", state="pending", attempts=0,
            next_attempt_at=None, connection_id=self.connection.pk, generation=2,
            payload={"goal_operation_id":self.op.pk,"goal_id":self.op.pk}, receipt={}, save=Mock())
        self.rows.return_value.filter.return_value.first.return_value = row
        self.stack.enter_context(patch.object(refunds.transaction, "atomic", side_effect=lambda: nullcontext()))
        return row

    def test_actual_unknown_receipt_keeps_parent_outbox_unresolved(self):
        self.child("article-goal-pending-publication-v1.json")
        row = self.refund_row()
        self.assertEqual(refunds.advance_refund(row.pk, now=self.now), "needs_attention")
        self.assertEqual(row.receipt["status"], "credit_needs_reconciliation")
        self.assertIsNone(row.next_attempt_at)
        self.credit.assert_not_called()

    def test_unknown_stop_attempts_are_bounded_and_never_credit(self):
        child = self.child()
        child.status, child.result = "failed", {"refundable": True}
        row = self.refund_row()
        with patch("content_factory.vibe_marketing_views._call_content_factory_run_action", side_effect=RuntimeError("unavailable")) as remote, \
            patch("workflow_runs.models.ContentFactoryRun.objects.filter") as selected:
            selected.return_value.first.return_value = child
            for attempt in range(4):
                refunds.advance_refund(row.pk, now=self.now + timedelta(minutes=6 * attempt))
        self.assertEqual(remote.call_count, 3)
        self.assertEqual(row.state, "needs_attention")
        self.assertEqual(child.result, {"refundable": True})
        self.credit.assert_not_called()

    def test_stop_obtains_real_producer_receipt_outside_credit_transaction(self):
        child = self.child()
        fixture = json.loads((Path(__file__).parent / "test_fixtures" / "article-goal-no-value-v1.json").read_text())
        child.status, child.result = "failed", {"refundable": True}
        row = self.refund_row()
        depth = [0]
        @contextmanager
        def atomic():
            depth[0] += 1
            try:
                yield
            finally:
                depth[0] -= 1
        def stopped(**kwargs):
            self.assertEqual(depth[0], 0)
            return fixture["cancellation_response"]
        def credited(**kwargs):
            self.assertGreater(depth[0], 0)
            return SimpleNamespace(id=22, pk=22), True
        self.credit.side_effect = credited
        def applied(run_id, payload):
            child.result = cancellation_result(child, payload["result"])
            child.status = "cancelled"
            return SimpleNamespace(status_code=200)
        with patch.object(refunds.transaction, "atomic", side_effect=atomic), \
            patch("content_factory.vibe_marketing_views._call_content_factory_run_action", side_effect=stopped) as remote, \
            patch("content_factory.service_views._apply_cancellation_receipt", side_effect=applied), \
            patch("workflow_runs.models.ContentFactoryRun.objects.filter") as selected:
            selected.return_value.first.return_value = child
            self.assertEqual(refunds.advance_refund(row.pk, now=self.now), "pending")
            self.credit.assert_not_called()
            self.assertEqual(refunds.advance_refund(row.pk, now=self.now + timedelta(minutes=6)), "completed")
        remote.assert_called_once()
        self.credit.assert_called_once()
        self.assertEqual(row.receipt["status"], "credited")

    def test_organization_and_connection_locks_precede_parent_and_child_credit(self):
        self.child()
        row = self.refund_row()
        order = []
        from organizations.models import Organization
        from .website_models import WebsiteConnection
        with patch.object(Organization.objects, "select_for_update") as organizations, \
            patch.object(WebsiteConnection.objects, "select_for_update") as connections:
            organizations.return_value.get.side_effect = lambda **kwargs: order.append("organization")
            connections.return_value.get.side_effect = lambda **kwargs: order.append("connection")
            self.rows.return_value.filter.return_value.first.side_effect = lambda: order.append("refund") or row
            self.rows.return_value.select_related.return_value.filter.return_value.first.side_effect = lambda: order.append("parent") or self.op
            self.credit.side_effect = lambda **kwargs: (order.append("credit") or SimpleNamespace(id=22, pk=22), True)
            self.assertEqual(refunds.advance_refund(row.pk, now=self.now), "completed")
        self.assertEqual(order, ["organization", "connection", "refund", "parent", "credit"])

    def test_useful_review_retains_parent_charge_after_technical_stop(self):
        child = self.child()
        child.result.update(preview_content_verified=True, preview_url="https://preview.example.test/article")
        row = self.refund_row()
        self.assertEqual(refunds.advance_refund(row.pk, now=self.now), "completed")
        self.assertEqual(row.receipt["status"], "retained_after_draft")
        self.credit.assert_not_called()

    def test_erasure_keeps_unknown_obligation_after_child_and_dispatch_are_removed(self):
        self.child("article-goal-pending-publication-v1.json")
        evidence = settlement.capture(self.op, self.held, erasing=True)
        self.assertNotIn("github_repo", json.dumps(evidence))
        self.op.receipt = {key: self.op.receipt[key] for key in ("goal", "first_article_billing", settlement.KEY)}
        self.children.clear()
        self.dispatch_rows.return_value.filter.return_value.first.return_value = None
        billing.release(self.op, reason="Erased goal")
        self.credit.assert_not_called()
        self.assertEqual(self.op.receipt[settlement.KEY]["status"], "needs_reconciliation")

    def test_erasure_keeps_positive_stop_proof_and_original_credit_once(self):
        self.child()
        evidence = settlement.capture(self.op, self.held, erasing=True)
        self.assertTrue(evidence["children"][0]["stop_receipt_id"])
        self.op.receipt = {key: self.op.receipt[key] for key in ("goal", "first_article_billing", settlement.KEY)}
        self.children.clear()
        self.dispatch_rows.return_value.filter.return_value.first.return_value = None
        billing.release(self.op, reason="Erased no-value goal")
        billing.release(self.op, reason="Erased replay")
        self.credit.assert_called_once()

    def test_disappearing_unerased_or_corrupt_history_never_becomes_no_dispatch(self):
        self.child()
        settlement.capture(self.op, self.held)
        self.op.receipt.pop("first_article_run_id")
        self.children.clear()
        self.dispatch_rows.return_value.filter.return_value.first.return_value = None
        billing.release(self.op, reason="History unavailable")
        self.credit.assert_not_called()
        self.op.receipt[settlement.KEY]["digest"] = "corrupt"
        billing.release(self.op, reason="Invalid history")
        billing.release(self.op, reason="Repeated invalid history")
        self.credit.assert_not_called()

    def test_overflow_remains_unresolved_after_erasure_and_replay(self):
        child = self.child()
        self.children[:] = [SimpleNamespace(**{**vars(child), "run_id": f"child-{number}"}) for number in range(33)]
        settlement.capture(self.op, self.held, erasing=True)
        self.children.clear()
        self.dispatch_rows.return_value.filter.return_value.first.return_value = None
        billing.release(self.op, reason="Bounded history overflow")
        billing.release(self.op, reason="Repeated overflow")
        self.assertFalse(self.op.receipt[settlement.KEY]["history_complete"])
        self.credit.assert_not_called()

    def test_owner_review_merge_and_deploy_waits_do_not_enter_settlement(self):
        from .website_contract import WebsiteAuthorityError
        for action in ("review_draft", "merge_pr", "deploy"):
            with self.subTest(action=action):
                self.op.state = "needs_user"
                self.op.receipt["user_action"] = {"id": action}
                with self.assertRaises(WebsiteAuthorityError):
                    billing.release(self.op, reason="Still waiting for owner")
        self.locked.assert_not_called()
        self.credit.assert_not_called()

    def test_source_stop_does_not_prove_lost_publish_handoff_stopped(self):
        source = self.child()
        for marker in ({"publish_handoff_pending": True}, {"publish_child_run_id": "missing-publisher"}):
            with self.subTest(marker=marker):
                source.result.update(marker)
                billing.release(self.op, reason="Publication response lost")
                self.credit.assert_not_called()
                self.assertEqual(self.op.receipt[settlement.KEY]["status"], "needs_reconciliation")

    def test_exact_publisher_child_needs_its_own_stop_and_original_lineage(self):
        source = self.child()
        child = SimpleNamespace(**{**vars(source), "pk": 22, "run_id": "publisher-child", "result": {},
            "run_request": {**source.run_request, "source_run_id": source.run_id}})
        source.result.update(publish_handoff_pending=False, publish_handoff_status="queued", publish_child_run_id=child.run_id)
        self.children.append(child)
        billing.release(self.op, reason="Publisher not stopped")
        self.credit.assert_not_called()
        self.assertEqual(self.op.receipt[settlement.KEY]["status"], "stop_required")
        proof = deepcopy(source.result["article_cancellation_v1"])
        proof["run_id"] = child.run_id
        proof["receipt_id"] = goals.digest({key: value for key, value in proof.items() if key != "receipt_id"})
        child.result = {"article_cancellation_v1": proof}
        billing.release(self.op, reason="Both stopped without value")
        self.credit.assert_called_once()

    def test_conflicting_domain_repository_or_source_facts_stay_unknown(self):
        child = self.child()
        for obj, key, changed in ((child, "domain", "foreign.test"), (child, "github_repo", "other/repository"),
                (child.run_request, "github_repo", "other/repository"), (child.run_request, "github_repo", None), (child.run_request, "branch", "other"),
                (child.run_request, "app_root", "other")):
            with self.subTest(key=key):
                original = obj[key] if isinstance(obj, dict) else getattr(obj, key)
                if isinstance(obj, dict): obj[key] = changed
                else: setattr(obj, key, changed)
                billing.release(self.op, reason="Conflicting original identity")
                self.credit.assert_not_called()
                self.assertEqual(self.op.receipt[settlement.KEY]["status"], "needs_reconciliation")
                if isinstance(obj, dict): obj[key] = original
                else: setattr(obj, key, original)

    def test_child_cannot_hide_missing_or_conflicting_original_ancestor(self):
        source = self.child()
        child = SimpleNamespace(**{**vars(source), "pk": 22, "run_id": "publisher-child", "result": {},
            "run_request": {**source.run_request, "source_run_id": source.run_id}})
        self.children.append(child)
        for key, value in (("source_run_id", "missing-source"), ("expected_source_sha", "b" * 40)):
            with self.subTest(key=key):
                previous = child.run_request.get(key)
                child.run_request[key] = value
                billing.release(self.op, reason="Publication child lineage mismatch")
                self.assertEqual(self.op.receipt[settlement.KEY]["status"], "needs_reconciliation")
                self.credit.assert_not_called()
                child.run_request[key] = previous
        child.run_request.pop("github_repo")
        child.github_repo = "foreign/site"
        self.assertFalse(settlement._owned(child, self.op, self.held, {source.run_id: source, child.run_id: child}))

    def test_identity_change_after_capture_never_stops_rebound_work(self):
        child = self.child()
        child.status, child.result = "failed", {}
        evidence = settlement.capture(self.op, self.held)
        child.run_request["operation_attempt"] += 1
        with patch("workflow_runs.models.ContentFactoryRun.objects.filter") as selected, \
            patch("content_factory.vibe_marketing_views._call_content_factory_run_action") as remote:
            selected.return_value.first.return_value = child
            settlement.stop_children(evidence, parent_id=self.op.pk)
        remote.assert_not_called()

    def test_parent_stop_sends_captured_scope_before_provider_rebind(self):
        from .website_cancellation_retirement import expected_article_cancellation
        child = self.child()
        child.status, child.result = "failed", {}
        evidence = settlement.capture(self.op, self.held)
        expected = deepcopy(expected_article_cancellation(child))
        def rebind_after_send(**kwargs):
            self.assertEqual(kwargs["payload"]["expected_cancellation_v1"], expected)
            child.run_request["operation_attempt"] += 1
            return {"status": "scope_mismatch"}
        with patch("workflow_runs.models.ContentFactoryRun.objects.filter") as selected, \
            patch("content_factory.vibe_marketing_views._call_content_factory_run_action", side_effect=rebind_after_send) as remote, \
            patch("content_factory.service_views._apply_cancellation_receipt") as apply:
            selected.return_value.first.return_value = child
            settlement.stop_children(evidence, parent_id=self.op.pk)
        remote.assert_called_once()
        apply.assert_not_called()
        self.credit.assert_not_called()

    def test_completed_published_child_is_not_cancelled_for_missing_stop_proof(self):
        child = self.child("article-goal-pending-publication-v1.json")
        child.status = "completed"
        child.result.pop("article_cancellation_v1", None)
        child.result["publication_evidence_v1"]["state"] = "observed"
        row = self.refund_row()
        with patch("content_factory.vibe_marketing_views._call_content_factory_run_action") as remote:
            self.assertEqual(refunds.advance_refund(row.pk, now=self.now), "needs_attention")
        remote.assert_not_called()
        self.credit.assert_not_called()

    def test_late_verified_stop_wakes_quarantine_without_resetting_budget_or_duplicate_wake(self):
        child = self.child()
        stopped = deepcopy(child.result)
        child.result, child.status = {}, "failed"
        settlement.capture(self.op, self.held)
        row = self.refund_row()
        row.state, row.attempts = "needs_attention", 4
        child.result, child.status = stopped, "cancelled"
        with patch("workflow_runs.models.ContentFactoryRun.objects.select_for_update") as selected:
            selected.return_value.filter.return_value.first.return_value = child
            refunds.wake_settlement(parent_id=self.op.pk, organization_id=7, child_run_id=child.run_id, now=self.now)
            self.assertEqual((row.state, row.attempts, row.next_attempt_at), ("pending", 4, self.now))
            row.state = "needs_attention"
            refunds.wake_settlement(parent_id=self.op.pk, organization_id=7, child_run_id=child.run_id, now=self.now)
            self.assertEqual(row.state, "needs_attention")
        self.credit.assert_not_called()
        row.state = "pending"
        self.assertEqual(refunds.advance_refund(row.pk, now=self.now), "completed")
        self.assertEqual(row.attempts, 5)
        self.credit.assert_called_once()

    def test_late_publish_child_receipt_wakes_with_its_owned_source_lineage(self):
        source = self.child()
        child = SimpleNamespace(**{**vars(source), "pk": 22, "run_id": "publisher-child", "result": {},
            "run_request": {**source.run_request, "source_run_id": source.run_id}})
        source.result["publish_child_run_id"] = child.run_id
        self.children.append(child)
        settlement.capture(self.op, self.held)
        proof = deepcopy(source.result["article_cancellation_v1"])
        proof["run_id"] = child.run_id
        proof["receipt_id"] = goals.digest({key: value for key, value in proof.items() if key != "receipt_id"})
        child.result = {"article_cancellation_v1": proof}
        row = self.refund_row()
        row.state, row.attempts = "needs_attention", 4
        with patch("workflow_runs.models.ContentFactoryRun.objects.select_for_update") as selected:
            selected.return_value.filter.return_value.first.side_effect = [child, source]
            refunds.wake_settlement(parent_id=self.op.pk, organization_id=7, child_run_id=child.run_id, now=self.now)
        self.assertEqual((row.state, row.attempts), ("pending", 4))
        self.assertEqual(refunds.advance_refund(row.pk, now=self.now), "completed")
        self.credit.assert_called_once()

    def test_unknown_or_already_captured_stop_never_restarts_quarantine(self):
        child = self.child("article-goal-pending-publication-v1.json")
        settlement.capture(self.op, self.held)
        row = self.refund_row()
        row.state, row.attempts = "needs_attention", 4
        with patch("workflow_runs.models.ContentFactoryRun.objects.select_for_update") as selected:
            selected.return_value.filter.return_value.first.return_value = child
            refunds.wake_settlement(parent_id=self.op.pk, organization_id=7, child_run_id=child.run_id)
        self.assertEqual((row.state, row.attempts), ("needs_attention", 4))
        self.credit.assert_not_called()

    def test_positive_observation_wakes_existing_parent_without_resetting_budget_or_refunding(self):
        child = self.child("article-goal-pending-publication-v1.json")
        settlement.capture(self.op, self.held)
        row = self.refund_row()
        row.state, row.attempts = "needs_attention", 4
        child.result["publication_evidence_v1"].update(state="observed", kind="branch",
            branch_name="content/original", head_sha="b" * 40)
        child.result["publication_observation_intent_id"] = "d" * 64
        with patch("workflow_runs.models.ContentFactoryRun.objects.select_for_update") as selected:
            selected.return_value.filter.return_value.first.return_value = child
            refunds.wake_settlement(parent_id=self.op.pk, organization_id=7, child_run_id=child.run_id,
                publication_intent_id="d" * 64, now=self.now)
            self.assertEqual((row.state, row.attempts), ("pending", 4))
            row.state = "needs_attention"
            refunds.wake_settlement(parent_id=self.op.pk, organization_id=7, child_run_id=child.run_id,
                publication_intent_id="d" * 64, now=self.now)
            self.assertEqual((row.state, row.attempts), ("needs_attention", 4))
        row.state = "pending"
        self.assertEqual(refunds.advance_refund(row.pk, now=self.now), "needs_attention")
        self.credit.assert_not_called()

    def test_late_dispatch_resolution_wakes_without_new_cancel_budget(self):
        child = self.child()
        dispatch = self.dispatch_rows.return_value.filter.return_value.first.return_value
        dispatch.pk, dispatch.state, dispatch.run_id = 91, "awaiting_resolution", ""
        settlement.capture(self.op, self.held)
        row = self.refund_row()
        row.state, row.attempts = "needs_attention", 4
        dispatch.state, dispatch.run_id = "delivered", child.run_id
        refunds.wake_settlement(parent_id=self.op.pk, organization_id=7, dispatch_id=91, now=self.now)
        self.assertEqual((row.state, row.attempts), ("pending", 4))
        self.assertEqual(refunds.advance_refund(row.pk, now=self.now), "completed")
        self.credit.assert_called_once()

    def test_receipt_wakeup_occurs_only_after_child_transaction_commits(self):
        from . import service_views
        child = self.child()
        child.error = ""
        payload = {"domain": child.domain, "result": child.result}
        callbacks = []
        with patch.object(service_views.transaction, "atomic", side_effect=lambda: nullcontext()), \
            patch.object(service_views.transaction, "on_commit", side_effect=lambda callback, **kwargs: callbacks.append(callback)), \
            patch("workflow_runs.models.ContentFactoryRun.objects.select_for_update") as selected, \
            patch("content_factory.website_operations.observe_workflow_status"), \
            patch.object(service_views, "_serialize_content_factory_run", return_value={}), \
            patch.object(refunds, "wake_settlement") as wake:
            selected.return_value.filter.return_value.first.return_value = child
            self.assertEqual(service_views._apply_cancellation_receipt(child.run_id, payload).status_code, 200)
            wake.assert_not_called()
            self.assertEqual(len(callbacks), 1)
            callbacks[0]()
            wake.assert_called_once_with(parent_id=self.op.pk, organization_id=7, child_run_id=child.run_id)

    def test_terminal_child_and_dispatch_refund_entrypoints_keep_parent_responsible(self):
        from .dispatch_outbox import refund_dispatch
        from integrations.services.article_generation import refund_content_factory_request_for_user
        child = self.child("article-goal-pending-publication-v1.json")
        with patch("content_factory.website_models.WebsiteConnectionOperation.objects.select_related") as parent, \
            patch("content_factory.website_models.WebsiteConnectionOperation.objects.get_or_create", return_value=(Mock(), True)) as reserved:
            parent.return_value.filter.return_value.first.return_value = self.op
            refund_content_factory_request_for_user(user=self.payer, actor_id="founder:4", article_request=child.run_request,
                resolved_domain="fixture.test", reason="Synchronous dispatch refusal")
            refund_dispatch(SimpleNamespace(payload={"request": child.run_request}, organization=self.organization), reason="dispatch_rejected")
        self.assertEqual(reserved.call_count, 2)
        self.credit.assert_not_called()
