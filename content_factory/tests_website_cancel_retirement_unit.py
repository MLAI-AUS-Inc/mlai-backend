"""The real lifecycle scheduler retires bounded work without inferring no effects."""
import json
from copy import deepcopy
from pathlib import Path
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase
from . import tests_cancel_preview_stop_unit as fixtures
from . import website_cancellation_retirement as policy, website_goal as goals


class CancelRetirementTests(SimpleTestCase):
    setUp = fixtures.CancelPreviewStopDeadlineTests.setUp
    _operation = fixtures.CancelPreviewStopDeadlineTests._operation
    _process = fixtures.CancelPreviewStopDeadlineTests._process

    def pure(self):
        op = self._operation(age=timedelta(days=9), cancel_run_ids=[fixtures.RUN])
        op.payload["stop_preview_run_ids"] = []
        op.connection.organization_id = 7
        return op

    def saved_run(self, op, workflow="repo_scan"):
        run = SimpleNamespace(run_id=fixtures.RUN, organization_id=7, workflow=workflow, status="cancelled", result={},
            domain="fixture.test", github_repo="fixture/site", run_request={"website_connection_id": str(op.connection_id), "connection_generation": 3,
                "operation_id": op.payload["cancelled_operation_id"], "operation_attempt": 1, "repository_id": 17, "deletion_epoch": 0})
        op.payload["cancel_run_bindings"] = policy.bindings_for([run])
        return run

    def test_generic_202_cannot_repeat_pure_cancellation_forever(self):
        op = self.pure()
        posts = 0
        with patch("workflow_runs.models.ContentFactoryRun.objects.filter") as runs:
            runs.return_value.first.return_value = self.saved_run(op)
            for _ in range(3):
                written, posted = self._process(op, cancel_status=202)
                posts += posted.call_count
                self.now += timedelta(hours=1)
        self.assertLessEqual(posts, 3)
        self.assertEqual(written["state"], "needs_attention")
        self.assertTrue(written["receipt"]["remote_cleanup_pending"])
        self.assertIn(fixtures.RUN, written["receipt"]["unconfirmed_cancel_run_ids"])

    def test_deterministic_409_is_bounded_without_fabricating_success(self):
        op = self.pure()
        with patch("workflow_runs.models.ContentFactoryRun.objects.filter") as runs:
            runs.return_value.first.return_value = self.saved_run(op)
            for _ in range(3):
                written, _ = self._process(op, cancel_status=409)
                self.now += timedelta(hours=1)
        self.assertEqual(written["state"], "needs_attention")
        self.assertNotEqual(written["receipt"].get("status"), "completed")

    def test_article_404_is_not_proof_of_no_publication_or_cancel_success(self):
        op = self.pure()
        request = {"website_connection_id": str(op.connection_id), "connection_generation": 3,
            "operation_id": op.payload["cancelled_operation_id"], "operation_attempt": 1}
        run = SimpleNamespace(run_id=fixtures.RUN, organization_id=7, workflow="article_generation", run_request=request,
            status="cancelled", result={}, domain="fixture.test")
        with patch("workflow_runs.models.ContentFactoryRun.objects.filter") as runs:
            runs.return_value.first.return_value = run
            written, _ = self._process(op, cancel_status=404)
        self.assertNotEqual(written["state"], "completed")
        self.assertIn(fixtures.RUN, written["payload"]["cancel_run_ids"])

    def test_actual_typed_article_stop_retires_cancel_and_retains_unknown_obligations(self):
        fixture = json.loads((Path(__file__).parent / "test_fixtures/article-cancellation-no-value-v1.json").read_text())
        for outcome in ("no_article_effect", "article_effect", "unknown"):
            with self.subTest(outcome=outcome):
                op = self.pure()
                request = fixture["original_request"]
                op.connection_id, op.generation = request["website_connection_id"], request["connection_generation"]
                op.payload["cancel_run_ids"] = [request["run_id"]]
                op.payload["cancelled_operation_id"] = request["operation_id"]
                op.payload["cancelled_action"] = "workflow"
                body = deepcopy(fixture["cancellation_response"])
                body["cleanup_pending"] = False
                body["article_cancellation_v1"]["outcome"] = outcome
                proof = body["article_cancellation_v1"]
                proof["receipt_id"] = goals.digest({key: value for key, value in proof.items() if key != "receipt_id"})
                run = SimpleNamespace(run_id=request["run_id"], organization_id=7, workflow="article_generation", status="cancelled",
                    result={}, domain="fixture.test", run_request=request)
                op.payload["cancel_run_bindings"] = policy.bindings_for([run])
                with patch("workflow_runs.models.ContentFactoryRun.objects.filter") as runs, \
                    patch("content_factory.service_views._apply_cancellation_receipt", return_value=SimpleNamespace(status_code=200)) as applied:
                    runs.return_value.first.return_value = run
                    written, posted = self._process(op, cancel_status=200 if outcome == "no_article_effect" else 202, cancel_body=body)
                self.assertEqual(posted.call_count, 1)
                self.assertTrue(posted.call_args.args[0].endswith(f"/api/runs/{run.run_id}/cancel"))
                self.assertEqual(posted.call_args.kwargs["json"]["expected_cancellation_v1"], policy.expected_article_cancellation(run))
                applied.assert_called_once()
                self.assertEqual(written["payload"]["cancel_run_ids"], [])
                self.assertEqual(written["state"], "completed" if outcome == "no_article_effect" else "needs_attention")
                self.assertEqual(written["receipt"]["article_stop_proofs"][run.run_id]["outcome"], outcome)
                self.assertNotIn("refunded", json.dumps(written["receipt"]))

    def test_third_claim_crash_cannot_buy_a_fourth_remote_attempt(self):
        op = self.pure()
        for _ in range(3):
            policy.claim(op, now=self.now)
            self.assertTrue(policy.can_call(op, now=self.now))
        with patch("workflow_runs.models.ContentFactoryRun.objects.filter") as runs:
            runs.return_value.first.return_value = self.saved_run(op)
            written, posted = self._process(op, cancel_status=503)
        posted.assert_not_called()
        self.assertEqual(written["state"], "needs_attention")
        self.assertEqual(written["receipt"][policy.KEY]["attempts"], 3)

    def test_exhausted_cancel_preserves_independent_preview_and_token_work(self):
        op = self.pure()
        op.payload["stop_preview_run_ids"] = [fixtures.RUN]
        for _ in range(3):
            policy.claim(op, now=self.now)
        with patch("workflow_runs.models.ContentFactoryRun.objects.filter") as runs:
            runs.return_value.first.return_value = self.saved_run(op)
            written, posted = self._process(op, tokens={"pending": 1, "revoked": 0})
        self.assertEqual(posted.call_count, 1)
        self.assertTrue(posted.call_args.args[0].endswith("/preview/stop"))
        self.assertEqual(written["state"], "pending")
        self.assertEqual(written["payload"]["cancel_run_ids"], [])
        self.assertEqual(written["payload"]["stop_preview_run_ids"], [fixtures.RUN])
        self.assertEqual(written["receipt"]["tokens"]["pending"], 1)
        self.assertEqual(written["receipt"]["unconfirmed_cancel_run_ids"], [fixtures.RUN])

    def test_scoped_nonarticle_absence_retires_work_without_article_financial_claims(self):
        op = self.pure()
        with patch("workflow_runs.models.ContentFactoryRun.objects.filter") as runs:
            runs.return_value.first.return_value = self.saved_run(op)
            written, _ = self._process(op, cancel_status=404,
                cancel_body={"run_id": fixtures.RUN, "workflow": "repo_scan", "status": "run_missing",
                    "operation_id": op.payload["cancelled_operation_id"], "operation_attempt": 1})
        self.assertEqual(written["state"], "completed")
        self.assertNotIn("article_stop_proofs", written["receipt"])

    def test_expired_or_corrupt_budget_never_resets_and_rebound_work_is_not_cancelled(self):
        for mutation in ("deadline", "identity", "scope"):
            with self.subTest(mutation=mutation):
                op = self.pure()
                policy.claim(op, now=self.now)
                run = self.saved_run(op)
                if mutation == "deadline": self.now += timedelta(hours=24)
                elif mutation == "identity": op.receipt[policy.KEY]["connection_id"] = "foreign"
                else: run.run_request["connection_generation"] += 1
                before = deepcopy(op.receipt[policy.KEY])
                with patch("workflow_runs.models.ContentFactoryRun.objects.filter") as runs:
                    runs.return_value.first.return_value = run
                    written, posted = self._process(op)
                posted.assert_not_called()
                self.assertEqual(written["receipt"][policy.KEY]["started_at"], before["started_at"])
                self.assertNotEqual(written["state"], "completed")

    def test_new_payload_identity_cannot_borrow_an_older_claim_budget(self):
        op = self.pure()
        policy.claim(op, now=self.now)
        original = deepcopy(op.receipt[policy.KEY]["run_ids"])
        op.payload["cancel_run_ids"] = ["different-run"]
        run = self.saved_run(op)
        run.run_id = "different-run"
        with patch("workflow_runs.models.ContentFactoryRun.objects.filter") as runs:
            runs.return_value.first.return_value = run
            written, posted = self._process(op)
        posted.assert_not_called()
        self.assertEqual(written["receipt"][policy.KEY]["run_ids"], original)
        self.assertNotEqual(written["state"], "completed")

    def test_known_nonarticle_fence_acknowledges_only_exact_operation_attempt(self):
        for attempt in (1, 2, True):
            with self.subTest(attempt=attempt):
                op = self.pure()
                run = self.saved_run(op)
                body = {"schema_version": 1, "status": "cancel_requested", "operation_id": run.run_request["operation_id"],
                    "operation_attempt": attempt, "cleanup_pending": False}
                with patch("workflow_runs.models.ContentFactoryRun.objects.filter") as runs:
                    runs.return_value.first.return_value = run
                    written, posted = self._process(op, cancel_body=body)
                self.assertTrue(posted.call_args.args[0].endswith(f"/api/connections/{op.connection_id}/cancel-operation"))
                self.assertEqual(posted.call_args.kwargs["json"]["operation_attempt"], 1)
                self.assertEqual(written["state"], "completed" if type(attempt) is int and attempt == 1 else "pending")

    def test_same_owner_with_new_attempt_epoch_repo_or_domain_never_receives_old_cancel(self):
        for field, value in (("operation_attempt", 2), ("deletion_epoch", 8), ("repository_id", 99),
                ("domain", "another.test"), ("github_repo", "other/repo")):
            with self.subTest(field=field):
                op = self.pure()
                run = self.saved_run(op)
                if field in {"domain", "github_repo"}: setattr(run, field, value)
                else: run.run_request[field] = value
                with patch("workflow_runs.models.ContentFactoryRun.objects.filter") as runs:
                    runs.return_value.first.return_value = run
                    written, posted = self._process(op)
                posted.assert_not_called()
                self.assertNotEqual(written["state"], "completed")

    def test_legacy_unbound_cancellation_is_retired_unknown_without_remote_guess(self):
        op = self.pure()
        run = self.saved_run(op)
        op.payload.pop("cancel_run_bindings")
        with patch("workflow_runs.models.ContentFactoryRun.objects.filter") as runs:
            runs.return_value.first.return_value = run
            for _ in range(3):
                written, posted = self._process(op)
                posted.assert_not_called()
                self.now += timedelta(hours=1)
        self.assertEqual(written["state"], "needs_attention")

    def test_invalid_goal_binding_cannot_match_an_absent_legacy_digest(self):
        op = self.pure()
        run = self.saved_run(op)
        run.run_request.update(first_article_goal_id="first", strategy_goal_id="other")
        op.payload.pop("cancel_run_bindings")
        with patch("workflow_runs.models.ContentFactoryRun.objects.filter") as runs:
            runs.return_value.first.return_value = run
            written, posted = self._process(op)
        posted.assert_not_called()
        self.assertNotEqual(written["state"], "completed")

    def test_nonarticle_saved_boolean_attempt_cannot_match_integer_acknowledgement(self):
        op = self.pure()
        run = self.saved_run(op)
        run.run_request["operation_attempt"] = True
        self.assertFalse(policy.acknowledge(op, run, run.run_id, 200, {
            "schema_version": 1, "status": "cancel_requested", "operation_id": run.run_request["operation_id"],
            "operation_attempt": 1}))
