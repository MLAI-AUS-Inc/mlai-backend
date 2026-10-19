"""Actual controller/transport observations with only storage and HTTP replaced."""
from contextlib import nullcontext
from copy import deepcopy
from datetime import timedelta
import json
import os
from pathlib import Path
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

from . import website_execution_observation as journal, website_execution_transport as transport
from . import website_prepare as prepare, website_goal_runner as runner
from . import tests_website_goal_audit_unit as audit_fixtures
from .tests_website_runtime_observation_unit import response
from .website_models import WebsiteConnection, WebsiteConnectionOperation
from .website_operations import operation_summary


@override_settings(ARTICLES_WEBSITE_DOCTOR_ENABLED=True, ARTICLES_WEBSITE_GOAL_RUNNER_ENABLED=True,
    ARTICLES_WEBSITE_GOAL_ENROLLED_ORGANIZATIONS=["7"], ARTICLES_FAILURE_TAXONOMY_POLICY_ENABLED=True,
    ARTICLES_FAILURE_TAXONOMY_ENROLLED_ORGANIZATIONS=["7"], ARTICLES_SETUP_CONSENT_ENABLED=False)
class BackendExecutionTests(SimpleTestCase):
    def setUp(self):
        self.fixture = audit_fixtures.GoalAuditTests("runTest")
        self.fixture.setUp(); self.addCleanup(self.fixture.doCleanups)
        self.op, self.now = self.fixture.op, self.fixture.now
        self.enterContext(patch.dict(os.environ, {"APP_RELEASE": "a" * 40}))

    def admit(self):
        self.op.receipt = {"status": "working"}
        with patch.object(WebsiteConnection, "operations", audit_fixtures.rows()), patch.object(
                WebsiteConnectionOperation.objects, "get_or_create", return_value=(self.op, True)):
            return runner.start_goal(self.op.connection, company_id=9, user=self.fixture.user,
                retry=False, target="ready", now=self.now)

    def claim(self):
        return prepare._claim_prepare(self.op.pk, self.now)

    def finish(self, changed=1):
        query = audit_fixtures.rows(self.op); query.update.return_value = changed
        with patch.object(runner, "finish_goal"), patch.object(WebsiteConnectionOperation.objects, "filter", return_value=query):
            return prepare._finish_prepare(self.op, now=self.now)

    def test_real_admission_claim_and_cas_outcome_produce_original_execution_evidence(self):
        self.admit(); self.assertIs(self.claim(), self.op)
        self.op.state = "pending"
        self.assertIs(self.finish(), self.op)
        value = journal.project(self.op)
        self.assertEqual(value["coverage"], "complete", value)
        self.assertEqual([row["event"] for row in value["records"]], ["admission", "claim", "outcome"])
        self.assertEqual([row["attempt"] for row in value["records"]], [0, 1, 1])
        self.assertTrue(all(row["revision"] == "a" * 40 for row in value["records"]))
        self.assertTrue(all(row["new_work_goal_enabled"] for row in value["records"]))
        self.assertNotIn(journal.KEY, operation_summary(self.op)["receipt"])

    def test_original_snapshot_survives_later_flag_changes(self):
        self.admit(); self.claim(); self.op.state = "pending"; self.finish()
        before = journal.project(self.op)
        with override_settings(ARTICLES_WEBSITE_GOAL_RUNNER_ENABLED=False):
            self.assertEqual(journal.project(self.op), before)
        self.assertTrue(before["records"][0]["flags"]["ARTICLES_WEBSITE_GOAL_RUNNER_ENABLED"])

    def test_crash_or_disabled_observation_leaves_attempt_gap_instead_of_qualification(self):
        self.admit(); self.claim()
        self.assertIn("backend_execution_attempts_incomplete", journal.project(self.op)["gaps"])
        self.op.attempts = 2
        with override_settings(ARTICLES_WEBSITE_DOCTOR_ENABLED=False):
            journal.record(self.op, "claim", now=self.now)
        self.assertIn("backend_execution_attempts_incomplete", journal.project(self.op)["gaps"])

    def test_outcome_losing_cas_does_not_commit_a_false_completed_attempt(self):
        self.admit(); self.claim()
        saved = deepcopy(self.op.receipt)
        self.op.state = "completed"
        self.assertIsNone(self.finish(changed=0))
        self.op.receipt = saved
        self.assertIn("backend_execution_attempts_incomplete", journal.project(self.op)["gaps"])

    def test_expired_claim_records_its_new_lease_fence_and_can_complete_after_recovery(self):
        self.admit(); self.claim()
        self.now += timedelta(minutes=6)
        self.assertIs(self.claim(), self.op)
        self.op.state = "pending"; self.finish()
        value = journal.project(self.op)
        self.assertEqual(value["coverage"], "complete", value)
        self.assertEqual([row["event"] for row in value["records"]],
            ["admission", "claim", "superseded", "claim", "outcome"])
        self.assertEqual([row["attempt"] for row in value["records"]], [0, 1, 1, 2, 2])

    def test_actual_lifecycle_cancellation_records_revoked_authority_instead_of_a_return(self):
        from . import website_first_article_billing as billing
        self.admit();self.claim()
        self.op.receipt["first_article_billing"] = {"synthetic": True}
        query = audit_fixtures.rows(self.op)
        query.exclude.return_value = [self.op]
        with patch.object(WebsiteConnectionOperation.objects,"select_for_update",return_value=query), \
                patch("content_factory.website_first_article_refunds.reserve_refund"):
            billing.cancel_held_goals(self.op.connection)
        value = journal.project(self.op)
        self.assertEqual(value["coverage"], "complete", value)
        self.assertEqual([row["event"] for row in value["records"]], ["admission", "claim", "authority_revoked"])
        saved = deepcopy(self.op.receipt)
        self.assertIsNone(self.finish(changed=0))
        self.op.receipt = saved
        self.assertEqual(journal.project(self.op)["records"][-1]["event"], "authority_revoked")

    def test_historical_missing_malformed_and_capacity_limited_journals_are_partial(self):
        self.assertEqual(journal.project(self.op)["coverage"], "partial")
        self.admit(); self.claim(); self.op.state = "pending"; self.finish()
        original = deepcopy(self.op.receipt)
        for key, value in (("from_start", False), ("overflow", True), ("goal_id", "foreign")):
            self.op.receipt = deepcopy(original); self.op.receipt[journal.KEY][key] = value
            self.assertEqual(journal.project(self.op)["coverage"], "partial")
        self.op.receipt = deepcopy(original)
        self.op.receipt[journal.KEY]["records"][1]["flags"]["private"] = "github_pat_canary"
        self.assertNotIn("github_pat_canary", json.dumps(journal.project(self.op)))

    def test_unknown_revision_and_optional_observation_errors_do_not_affect_controller(self):
        with patch.dict(os.environ, {"APP_RELEASE": "main"}):
            self.admit()
        self.assertIn("backend_execution_revision_unknown", journal.project(self.op)["gaps"])
        before = deepcopy(self.op.receipt)
        with patch("content_factory.website_runtime_observation.backend_process", side_effect=ValueError("private")):
            journal.record(self.op, "claim", now=self.now)
        self.assertEqual(self.op.receipt, before)

    def test_disabled_defaults_add_no_snapshot_or_retroactive_journal(self):
        with override_settings(ARTICLES_WEBSITE_DOCTOR_ENABLED=False), patch(
                "content_factory.website_runtime_observation.backend_process") as process:
            journal.record(self.op, "admission", now=self.now, from_start=True)
        process.assert_not_called()
        self.assertNotIn(journal.KEY, self.op.receipt)
        self.op.attempts = 4
        journal.record(self.op, "claim", now=self.now)
        self.assertFalse(journal.project(self.op)["from_start"])


@override_settings(CONTENT_FACTORY_URL="https://cf.example.test", CONTENT_FACTORY_API_KEY="synthetic-cf-read-key")
class CFExecutionTransportTests(SimpleTestCase):
    def setUp(self):
        from .tests_website_zoo_scope_unit import ZooScopeTests
        self.fixture = ZooScopeTests(); self.fixture.setUp()
        self.original = self.fixture.report()["cases"][0]
        self.locator = self.original["runs"][0]
        from django.utils import timezone
        self.now = timezone.now()
        base = {"sequence": 1, "execution_id": "a" * 32, "event": "entry", "outcome": None,
            "task": "run_scan", "instance_id": "b" * 32, "observed_at": self.now.isoformat(), "revision": "c" * 40,
            "configuration": "observed", "flags": {key: True for key in transport.rollout.CF_FLAGS},
            "run_predicates": {key: True for key in transport.rollout.SCOPED_PREDICATES},
            "lease_proof": None, "handoff_proof": None, "generation": 1, "state_version": 8,
            "request_scope_digest": self.locator["request_scope_digest"], "goal_lineage_digest": self.locator["goal_lineage_digest"]}
        self.value = {"schema_version": 1, "basis": "cf_admitted_task_execution_v1",
            "run_id": self.locator["run_id"], "goal_id": self.original["original_goal_id"],
            **{key: self.locator[key] for key in ("scope_fields", "request_scope_digest", "lineage_fields", "goal_lineage_digest")},
            "overflow": False, "coverage": "complete", "coverage_scope": "admitted_task_body_journal_only", "gaps": [],
            "records": [base, {**base, "sequence": 2, "event": "outcome", "outcome": "returned", "state_version": 9}]}
        request = self.fixture.run.run_request
        self.value["scope_epochs"] = [{**{key: self.locator[key] for key in
            ("request_scope_digest", "goal_lineage_digest", "scope_fields", "lineage_fields")},
            "scope": {key: request[key] for key in self.locator["scope_fields"]},
            "lineage": {key: request[key] for key in self.locator["lineage_fields"]}}]

    def test_authenticated_read_uses_exact_saved_run_and_binds_all_digest_and_execution_fields(self):
        with patch.object(transport.requests.Session, "send", return_value=response(self.value)) as send:
            result = transport.fetch(self.original)
        self.assertEqual(result["coverage"], "complete", result)
        observed = result["runs"][0]
        self.assertEqual(observed["records"], self.value["records"])
        self.assertEqual(observed["scope_epochs"][0]["operation_attempt"], 1)
        self.assertNotIn("scope", observed["scope_epochs"][0])
        self.assertNotIn("github_pat_zooScopePrivateCanary", json.dumps(observed))
        self.assertNotIn("private/customer/source-tree", json.dumps(observed))
        prepared = send.call_args.args[0]
        self.assertEqual(prepared.url, "https://cf.example.test/api/internal/articles/execution-observations?run_id=fixture-run")
        self.assertEqual(prepared.headers["X-API-Key"], "synthetic-cf-read-key")
        self.assertFalse(send.call_args.kwargs["allow_redirects"])
        self.assertTrue(send.call_args.kwargs["verify"])
        self.assertEqual(send.call_args.kwargs["proxies"], {})

    def test_changed_scope_foreign_goal_stale_generation_future_version_and_malformed_records_refuse(self):
        for key, value in (("goal_id", "foreign"), ("request_scope_digest", "d" * 64), ("goal_lineage_digest", "d" * 64)):
            candidate = deepcopy(self.value); candidate[key] = value
            self.assertIsNone(transport.project(candidate, self.locator, goal_id=self.original["original_goal_id"], now=self.now))
        for key, value in (("generation", 2), ("state_version", 10), ("sequence", True), ("instance_id", "private"),
                ("observed_at", (self.now + timedelta(hours=1)).isoformat()), ("outcome", "returned")):
            candidate = deepcopy(self.value); candidate["records"][0][key] = value
            self.assertIsNone(transport.project(candidate, self.locator, goal_id=self.original["original_goal_id"], now=self.now))

    def test_unfinished_task_is_preserved_as_partial_without_inventing_return(self):
        self.value["records"].pop(); self.value.update(coverage="partial", gaps=["task_outcome_unobserved"])
        with patch.object(transport.requests.Session, "send", return_value=response(self.value)):
            result = transport.fetch(self.original)
        self.assertEqual(result["coverage"], "partial")
        self.assertEqual(result["runs"][0]["records"], self.value["records"])

    def test_prior_generation_interruption_requires_a_genuine_new_generation_fence(self):
        entry = self.value["records"][0]
        entry.update(generation=0, state_version=100)
        self.value["records"][1].update(outcome="generation_superseded")
        self.assertIsNotNone(transport.project(self.value, self.locator,
            goal_id=self.original["original_goal_id"], now=self.now))
        self.value["records"][1]["generation"] = 0
        self.assertIsNone(transport.project(self.value, self.locator,
            goal_id=self.original["original_goal_id"], now=self.now))

    def test_same_run_resume_preserves_old_attempt_scope_and_hides_both_raw_tuples(self):
        from . import website_goal as goals
        epoch = deepcopy(self.value["scope_epochs"][0])
        epoch["scope"]["operation_attempt"] = 2
        epoch["request_scope_digest"] = goals.digest(epoch["scope"])
        self.value["scope_epochs"].append(epoch)
        self.value["request_scope_digest"] = epoch["request_scope_digest"]
        locator = {**self.locator, "request_scope_digest": epoch["request_scope_digest"],
            "execution": {"generation": 2, "state_version": 3}}
        self.value["records"][1].update(outcome="generation_superseded", generation=2, state_version=1,
            request_scope_digest=epoch["request_scope_digest"])
        observed = transport.project(self.value, locator, goal_id=self.original["original_goal_id"], now=self.now)
        self.assertIsNotNone(observed)
        self.assertEqual([row["operation_attempt"] for row in observed["scope_epochs"]], [1, 2])
        epoch["scope"]["expected_source_sha"] = "e" * 40
        epoch["request_scope_digest"] = goals.digest(epoch["scope"])
        self.value["scope_epochs"][-1] = epoch
        self.assertIsNone(transport.project(self.value, locator,
            goal_id=self.original["original_goal_id"], now=self.now))

    def test_actual_cf_guarded_task_adoption_and_rewind_producer_matches_backend_reader(self):
        fixture = json.loads((Path(__file__).parent / "testdata/runtime-observation/cf-execution-resume-v1.json").read_text())
        value = fixture["journal"]
        now = max(transport.goals.stamp(row["observed_at"]) for row in value["records"]) + timedelta(seconds=1)
        observed = transport.project(value, fixture["locator"], goal_id=value["goal_id"], now=now)
        self.assertIsNotNone(observed)
        self.assertEqual(observed["coverage"], "complete")
        self.assertEqual([row["outcome"] for row in observed["records"]],
            [None, "generation_superseded", None, "returned"])
        self.assertEqual([row["operation_attempt"] for row in observed["scope_epochs"]], [1, 2])
        self.assertTrue(all("scope" not in epoch and "lineage" not in epoch for epoch in observed["scope_epochs"]))

    def test_missing_scope_auth_redirect_and_oversize_are_unknown_without_echoing_payload(self):
        for changes in ({"CONTENT_FACTORY_API_KEY": ""}, {"CONTENT_FACTORY_URL": "http://cf.example.test"},
                {"CONTENT_FACTORY_URL": "https://user:password@cf.example.test"}):
            with override_settings(**changes), patch.object(transport.requests.Session, "send") as send:
                self.assertEqual(transport.fetch(self.original)["coverage"], "partial")
            send.assert_not_called()
        for remote in (response({"private": "github_pat_canary"}, status=302), response({"private": "x" * (transport.MAX_BYTES + 1)})):
            with patch.object(transport.requests.Session, "send", return_value=remote):
                value = transport.fetch(self.original)
            self.assertEqual(value["coverage"], "partial")
            self.assertNotIn("github_pat_canary", json.dumps(value))

    def test_actual_same_generation_foreign_guard_takeover_is_bound_and_not_a_normal_return(self):
        fixture = json.loads((Path(__file__).parent / "testdata/runtime-observation/cf-execution-lease-v1.json").read_text())
        value, locator = fixture["journal"], fixture["locator"]
        now = max(transport.goals.stamp(row["observed_at"]) for row in value["records"]) + timedelta(seconds=1)
        observed = transport.project(value, locator, goal_id=value["goal_id"], now=now)
        self.assertIsNotNone(observed)
        self.assertEqual(observed["coverage"], "complete")
        self.assertEqual(observed["records"][1]["outcome"], "lease_superseded")
        for key, invalid in (("previous_lease_id", "f" * 32), ("ttl_seconds", 604801),
                ("previous_heartbeat_at", (now + timedelta(minutes=1)).isoformat()),
                ("previous_outcome", "github_pat_private-canary")):
            candidate = deepcopy(value); candidate["records"][1]["handoff_proof"][key] = invalid
            self.assertIsNone(transport.project(candidate, locator, goal_id=value["goal_id"], now=now))
