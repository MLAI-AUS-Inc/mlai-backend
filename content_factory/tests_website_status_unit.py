"""Public status and consistency invariants without database or provider access."""
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch
from contextlib import nullcontext
from django.test import SimpleTestCase, override_settings
from django.utils import timezone
from . import website_goal as goals, website_status as status, website_consistency as consistency
from . import tests_website_goal_unit as fixtures
from . import tests_prepare_journey_unit as journey_fixtures


@override_settings(ARTICLES_WEBSITE_GOAL_RUNNER_ENABLED=True, ARTICLES_WEBSITE_GOAL_ENROLLED_ORGANIZATIONS=["7"],
                   ARTICLES_FAILURE_TAXONOMY_POLICY_ENABLED=True, ARTICLES_FAILURE_TAXONOMY_ENROLLED_ORGANIZATIONS=["7"])
class WebsiteStatusTests(SimpleTestCase):
    def setUp(self):
        self.now = timezone.now()
        self.op = fixtures.GoalRunnerRegressions().operation()
        self.op.action = "prepare"
        self.op.connection.verified_sha = "a" * 40
        self.op.connection.github_repo = "fixture/site"
        self.op.updated_at = self.now
        self.op.next_attempt_at = self.now + timedelta(seconds=30)
        self.journey = {"prerequisites": {"account": {"status": "complete"}, "repository": {"status": "complete"}},
                        "capabilities": {"canPublishArticle": True}}

    def project(self):
        return status.project_status(self.op, scope=fixtures.scope(), source_sha="a" * 40, journey=self.journey, now=self.now)

    def test_four_states_and_one_action_from_the_persisted_goal(self):
        for state, expected in (("pending", "working"), ("running", "working"), ("needs_user", "needs_you"),
                                ("cancelled", "stopped"), ("failed", "stopped"), ("needs_attention", "stopped")):
            with self.subTest(state=state):
                self.op.state = state
                self.op.receipt["user_action"] = {"id": "review_setup", "run_id": "bound"}
                self.op.receipt["setup_run_id"] = "bound"
                result = self.project()
                self.assertEqual(result["state"], expected)
                self.assertEqual(result["nextAttemptAt"] is not None, expected == "working")
                if expected == "needs_you":
                    self.assertEqual(result["action"]["runId"], "bound")
        self.op.state = "completed"
        self.op.receipt["goal"] = goals.refresh_goal(self.op.receipt["goal"], fixtures.proof_set(self.now), scope=fixtures.scope(), source_sha="a" * 40, now=self.now)
        result = self.project()
        self.assertEqual(result["state"], "done")
        self.assertIsNone(result["action"])
        self.assertIsNone(result["nextAttemptAt"])

    def test_stale_unproved_malformed_disabled_or_timerless_work_never_claims_ready(self):
        self.op.state = "completed"
        self.assertEqual(self.project()["state"], "stopped")
        self.op.state = "pending"
        self.op.next_attempt_at = None
        self.assertEqual(self.project()["state"], "stopped")
        self.op.next_attempt_at = self.now
        self.op.receipt["goal"]["scope"]["connection_generation"] = 4
        self.assertEqual(self.project()["state"], "stopped")
        self.op.receipt["goal"] = goals.new_goal("prepare", fixtures.scope(), now=self.now)
        with override_settings(ARTICLES_WEBSITE_GOAL_RUNNER_ENABLED=False):
            self.assertEqual(self.project()["state"], "stopped")

    def test_disconnect_completion_is_done_even_without_repository_access(self):
        self.op.action, self.op.state, self.op.receipt = "disconnect", "completed", {}
        self.journey = {}
        result = self.project()
        self.assertEqual((result["state"], result["title"]), ("done", "Website disconnected"))

    def test_owner_actions_never_copy_unbound_runs_or_secret_urls(self):
        for raw in ({"id": "review_setup", "run_id": "foreign"}, {"id": "authorize_github", "url": "https://github.com/path?token=secret"},
                    {"id": "review_setup", "url": "https://worker.test/body"}):
            result = status._action(raw, "prepare", owner=True, run_ids=())
            self.assertNotIn("runId", result)
            self.assertNotIn("url", result)
        self.assertEqual(status._action({"id": "merge_pr"}, "prepare", owner=True, run_ids=("bound",))["runId"], "bound")

    def test_terminal_parent_wins_over_active_child(self):
        self.op.state = "cancelled"
        child = SimpleNamespace(status="running", result={})
        self.assertIn("terminal_parent_active_child", consistency.findings_for(self.op, child))
        self.assertEqual(self.project()["state"], "stopped")


@override_settings(ARTICLES_WEBSITE_CONSISTENCY_ENABLED=True)
class ConsistencyTests(SimpleTestCase):
    def operation(self):
        op = fixtures.GoalRunnerRegressions().operation()
        op.connection_id = op.connection.pk
        op.action, op.next_attempt_at = "prepare", None
        return op

    def test_applied_revision_without_an_applied_change_is_report_only(self):
        op = self.operation()
        child = SimpleNamespace(status="blocked", result={"revision_batches": {"batch": {"status": "applied", "revision_applied": False}}})
        self.assertIn("applied_revision_without_proof", consistency.findings_for(op, child))
        child.result["revision_batches"]["batch"]["revision_applied"] = True
        self.assertNotIn("applied_revision_without_proof", consistency.findings_for(op, child))

    def test_dry_run_writes_nothing_and_apply_requires_exact_scope(self):
        from .website_models import WebsiteConnectionOperation
        op = self.operation()
        before = deepcopy(op.receipt)
        rows = journey_fixtures.FrozenPrepareContextTests.rows()
        rows.__getitem__.return_value = [op]
        with patch.object(WebsiteConnectionOperation.objects, "select_related", return_value=rows), patch.object(consistency, "_child", return_value=None):
            result = consistency.report_consistency()
            self.assertEqual(result["applied"], 0)
            self.assertIn("missing_progress_timer", result["findings"][0]["findings"])
            self.assertEqual(op.receipt, before)
            op.save.assert_not_called()
            rows.update.assert_not_called()
            with self.assertRaises(ValueError):
                consistency.report_consistency(apply=True)

    def test_apply_rechecks_generation_and_is_idempotent(self):
        from .website_models import WebsiteConnection, WebsiteConnectionOperation
        from organizations.models import Organization
        op = self.operation()
        rows = journey_fixtures.FrozenPrepareContextTests.rows(first=op)
        with patch.object(WebsiteConnectionOperation.objects, "select_related", return_value=rows), \
             patch.object(WebsiteConnectionOperation.objects, "select_for_update", return_value=rows), \
             patch.object(WebsiteConnection.objects, "select_for_update") as connections, \
             patch.object(Organization.objects, "select_for_update"), \
             patch.object(consistency.transaction, "atomic", return_value=nullcontext()), \
             patch.object(consistency, "_child", return_value=None):
            connections.return_value.get.return_value = op.connection
            kwargs = {"expected_generation": op.generation, "expected_updated_at": op.updated_at}
            op.connection.generation += 1
            self.assertEqual(consistency.repair_operation(op.pk, **kwargs), [])
            op.save.assert_not_called()
            op.connection.generation -= 1
            self.assertEqual(consistency.repair_operation(op.pk, **kwargs), ["missing_progress_timer"])
            self.assertEqual(consistency.repair_operation(op.pk, **kwargs), [])
            self.assertEqual(len(op.receipt["consistency_repairs"]), 1)

class ProofReaderTests(SimpleTestCase):
    def test_real_native_proof_readers_require_current_ci_and_deployment_marker(self):
        from uuid import uuid4
        from organizations.models import Organization
        from .models import OrganizationContentConfig
        from .website_models import WebsiteConnection, WebsiteConnectionTarget, WebsiteScanSnapshot
        from .website_connections import contract_for
        from .website_goal_evidence import collect_proofs
        now, sha, operation_id = timezone.now(), "a" * 40, uuid4()
        org = Organization(pk=7, domain="fixture.test")
        connection = WebsiteConnection(organization=org, repository_id=123, github_repo="fixture/site", installation_id="4",
            generation=3, configuration_version=2, branch="main", verified_sha=sha, last_verified_at=now)
        identity = {**contract_for(connection), "operation_id": str(operation_id), "operation_attempt": 1, "deletion_epoch": 0,
            "source_sha": sha, "target_id": "articles", "contract_digest": "c" * 64, "evidence_digest": "e" * 64}
        ci = {**identity, "status": "verified", "allowed": True, "attestation_origin": "authenticated_ci", "checked_at": now.isoformat(),
            "artifact_digest": "f" * 64, "run_id": "verification-child"}
        target = WebsiteConnectionTarget(connection=connection, generation=3, target_key="articles", source_sha=sha,
            verified_at=now, contract={"contract_digest": "c" * 64, "verification": ci, "live_marker": {"kind": "artifact_digest", "value": "f" * 64}})
        target.updated_at = now
        snapshot = WebsiteScanSnapshot(connection=connection, generation=3, source_sha=sha, fingerprint="b" * 64,
            run_id="scan-child", evidence={"repository_inventory": {"discovery_complete": True}})
        snapshot.created_at = now
        config = OrganizationContentConfig(organization=org, website_connection=connection, default_publish_target_id="articles")
        context = SimpleNamespace(company=SimpleNamespace(pk=9))
        op = SimpleNamespace(receipt={}, connection=connection)
        workflow = SimpleNamespace(pk=operation_id, payload={"attempt": 1}, state="completed", generation=3)
        deployment = SimpleNamespace(receipt={"status": "passed", "provider_verified": True, "checked_at": now.isoformat(),
            "source_sha": sha, "connection_generation": 3, "target_id": "articles", "contract_digest": "c" * 64,
            "ci_evidence_digest": "e" * 64, "artifact_digest": "f" * 64})
        rows = journey_fixtures.FrozenPrepareContextTests.rows()
        def select(**query):
            found = SimpleNamespace(receipt=ci) if query.get("action") == "ci-verify" else deployment if query.get("action") == "deployment-verify" else workflow
            return journey_fixtures.FrozenPrepareContextTests.rows(first=found)
        rows.filter.side_effect = select
        journey = {"connectionId": str(connection.pk), "connectionGeneration": 3, "repository": {"observedSourceSha": sha},
            "prerequisites": {"account": {"status": "complete"}, "repository": {"status": "complete"}}, "capabilities": {"canGenerateArticle": True}}
        with patch.object(WebsiteConnection, "operations", rows), \
             patch.object(WebsiteConnection, "scan_snapshots", journey_fixtures.FrozenPrepareContextTests.rows(first=snapshot)), \
             patch.object(WebsiteConnection, "targets", journey_fixtures.FrozenPrepareContextTests.rows(first=target)):
            proofs, source = collect_proofs(op, context, config, journey, now=now)
            scope = goals.scope_for(connection, 9)
            goal = goals.refresh_goal(goals.new_goal("prepare", scope, now=now), proofs, scope=scope, source_sha=source, now=now)
            self.assertTrue(goals.complete(goal))
            for bad in ({"provider_verified": False}, {"artifact_digest": "0" * 64}, {"source_sha": "0" * 40},
                        {"checked_at": (now - timedelta(days=8)).isoformat()}):
                with self.subTest(bad=bad):
                    original = deployment.receipt
                    deployment.receipt = {**original, **bad}
                    self.assertNotIn("live", collect_proofs(op, context, config, journey, now=now)[0])
                    deployment.receipt = original
            ci["attestation_origin"] = "worker_claim"
            self.assertNotIn("previewed", collect_proofs(op, context, config, journey, now=now)[0])
