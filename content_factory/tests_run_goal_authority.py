"""Actual saved Run ownership fences for the legacy goal/doctor controllers."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone

from workflow_runs.models import ContentFactoryRun
from . import website_goal as goals, website_goal_runner as runner, website_prepare as prepare
from .run_event_authority import receive_authoritative_run_events
from .run_ownership import bind_admitted_run, reserve_admitted_run, user_principal, OWNER_EXECUTION_KEY
from .tests_run_event_authority import FLAGS, batch, advance_ticket
from .tests_website_connections import WebsiteDatabaseFixture
from .website_models import WebsiteConnectionOperation
from .website_operations import observe_workflow_status
from .website_run_authority import execution_request


@override_settings(**FLAGS)
class RunGoalAuthorityTests(WebsiteDatabaseFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.now = timezone.now()
        self.website.verified_sha = "a" * 40
        self.website.state = "connected"
        self.website.save(update_fields=["verified_sha", "state"])
        self.op = WebsiteConnectionOperation.objects.create(connection=self.website,
            generation=self.website.generation, action="prepare", state="running", idempotency_key="owned-goal",
            payload={"company_id": "9", "attempt": 1}, receipt={})
        self.op.receipt = {"goal": goals.new_goal(str(self.op.pk), goals.scope_for(self.website, 9), now=self.now),
            "current_step": "setup"}
        self.workflow = WebsiteConnectionOperation.objects.create(connection=self.website,
            generation=self.website.generation, action="workflow", state="pending", idempotency_key="owned-workflow",
            payload={"attempt": 1}, receipt={})
        raw = {**self.binding, "domain": self.org.domain, "github_repo": self.website.github_repo,
            "client_request_id": "owned-goal-child", "expected_source_sha": "a" * 40,
            "operation_id": str(self.workflow.pk), "operation_attempt": 1, "deletion_epoch": 0}
        self.run = bind_admitted_run(reserve_admitted_run(organization=self.org,
            principal=user_principal(123, "user:123"), workflow="article_system_setup", payload=raw), "owned-setup-worker")
        self.workflow.payload = {**self.workflow.payload, "run_id": self.run.run_id}
        self.workflow.save(update_fields=["payload"])
        self.op.receipt.update(child_run_id=self.run.run_id, setup_run_id=self.run.run_id)
        self.op.save(update_fields=["receipt"])

    def state(self, value):
        response = receive_authoritative_run_events(batch(self.run, [{"status": value}]))
        self.assertEqual(response.status_code, 200, response.data)
        self.run.refresh_from_db()

    def test_failed_owned_goal_retains_child_and_never_mints_a_retry_key_even_flags_off(self):
        self.state("failed")
        before = ContentFactoryRun.objects.values().get(pk=self.run.pk)
        payload, goal = deepcopy(self.op.payload), deepcopy(self.op.receipt["goal"])
        with override_settings(ARTICLES_RUN_OWNERSHIP_ENABLED=False, ARTICLES_RUN_EVENT_AUTHORITY_ENABLED=False):
            self.assertEqual(runner.retry_goal_action(self.op, goal, now=self.now, occurrence="repair"), goal)
        self.assertEqual(self.op.payload, payload)
        self.assertEqual(self.op.receipt["child_run_id"], self.run.run_id)
        self.assertEqual(self.op.state, "needs_user")
        self.assertEqual(ContentFactoryRun.objects.values().get(pk=self.run.pk), before)

    def test_goal_planner_stops_before_any_independent_failure_recovery(self):
        self.state("blocked")
        context = SimpleNamespace(company=SimpleNamespace(pk=9))
        with patch("content_factory.website_goal_evidence.collect_proofs") as collect:
            value = runner.goal_journey(self.op, context, self.config, {"nextAction": {"id": "setup"}}, now=self.now)
        collect.assert_not_called()
        self.assertIsNone(value["nextAction"])
        self.assertTrue(self.op._goal_wait)
        self.assertEqual(self.op.receipt["user_action"]["run_id"], self.run.run_id)

    def test_failure_accounting_does_not_dispatch_legacy_policy_or_article_resume(self):
        self.state("failed")
        self.op.receipt.update(failure_class="repairable", failure_occurrence_id="one-owned-failure")
        with patch("content_factory.failure_policy_engine.decide") as decide, \
                patch("content_factory.website_article_recovery.schedule") as schedule:
            runner.finish_goal(self.op, now=self.now)
        decide.assert_not_called(); schedule.assert_not_called()
        self.assertEqual(self.op.receipt["goal"]["attempts_by_class"]["repairable"], 1)
        self.assertEqual(self.op.state, "needs_user")

    @override_settings(ARTICLES_WEBSITE_GOAL_RUNNER_ENABLED=True)
    def test_source_wake_cannot_cancel_replace_or_reenroll_an_active_owned_child(self):
        self.op.receipt["goal"]["current_source_sha"] = "a" * 40
        self.op.save(update_fields=["receipt"])
        self.website.verified_sha = "b" * 40
        self.website.save(update_fields=["verified_sha"])
        before = WebsiteConnectionOperation.objects.values().get(pk=self.op.pk)
        with override_settings(ARTICLES_WEBSITE_GOAL_ENROLLED_ORGANIZATIONS=[str(self.org.pk)],
                ARTICLES_FAILURE_TAXONOMY_POLICY_ENABLED=True,
                ARTICLES_FAILURE_TAXONOMY_ENROLLED_ORGANIZATIONS=[str(self.org.pk)]), \
                patch("content_factory.website_prepare._cancel_prepare_child") as cancel, \
                patch("content_factory.website_prepare.start_prepare") as start:
            self.assertEqual(runner.wake_goal_for_source(self.config, company_id=9, user=None, now=self.now).pk, self.op.pk)
        cancel.assert_not_called(); start.assert_not_called()
        self.assertEqual(WebsiteConnectionOperation.objects.values().get(pk=self.op.pk), before)

    def test_prepare_never_implicitly_resumes_owned_failure(self):
        from .website_contract import WebsiteAuthorityError
        self.assertFalse(prepare._unverified_authorization_is_transient(self.op, self.run,
            prepare.UNVERIFIED_AUTHORIZATION_CODE, "repairable", "setup"))
        with patch("content_factory.vibe_marketing_views.VibeMarketingRunControlView.post") as resume, \
                self.assertRaises(WebsiteAuthorityError):
            prepare._resume_prepare_child(self.op, self.run, user=None, context=None)
        resume.assert_not_called()
        from .website_article_recovery import locator, resume as article_resume
        self.assertIsNone(locator(self.run))
        self.assertFalse(article_resume(self.op, self.run, None, user=None, now=self.now))

    def test_proven_completed_goal_is_not_reopened_by_a_retained_old_failure(self):
        self.state("completed")
        scope = goals.scope_for(self.website, 9)
        proofs = {name: goals.make_proof(name, sorted(goals.PROOF_KINDS[name])[0], scope=scope,
            source_sha="a" * 40, sha="a" * 40, run_id=self.run.run_id, evidence={"name": name}, at=self.now)
            for name in goals.MILESTONES[:7]}
        self.op.receipt["goal"] = goals.refresh_goal(self.op.receipt["goal"], proofs,
            scope=scope, source_sha="a" * 40, now=self.now)
        self.assertTrue(goals.complete(self.op.receipt["goal"]))
        self.op.receipt.update(failure_class="repairable", failure_occurrence_id="old-repair")
        self.op.state = "completed"
        runner.finish_goal(self.op, now=self.now)
        self.assertEqual(self.op.state, "completed")
        self.assertTrue(goals.complete(self.op.receipt["goal"]))

    def test_current_ticket_scope_drives_operation_observation_without_touching_run_inputs(self):
        ticket = advance_ticket(self.run, generation=1)
        ticket["scope"]["operation_attempt"] = 2
        result = deepcopy(self.run.result)
        result[OWNER_EXECUTION_KEY]["tickets"][ticket["execution_id"]] = ticket
        ContentFactoryRun.objects.filter(pk=self.run.pk).update(result=result)
        self.run.refresh_from_db()
        self.workflow.state = "failed"
        self.workflow.payload = {**self.workflow.payload, "attempt": 2, "resume_pending": True}
        self.workflow.save(update_fields=["state", "payload"])
        self.state("running")
        before = ContentFactoryRun.objects.values().get(pk=self.run.pk)
        from .website_goal_evidence import _child
        self.assertEqual(_child(self.op, self.website).pk, self.run.pk)
        from .website_doctor import read_doctor
        self.assertEqual(read_doctor(self.op, scope=goals.scope_for(self.website, 9),
            source_sha="a" * 40)["children"], [{"runId": self.run.run_id, "status": "running"}])
        observe_workflow_status(self.run, {"operation_attempt": 1})
        self.workflow.refresh_from_db()
        self.assertEqual(self.workflow.state, "running")
        self.assertFalse(self.workflow.payload["resume_pending"])
        self.assertEqual(execution_request(self.run)["operation_attempt"], 2)
        self.assertEqual(self.run.run_request["operation_attempt"], 1)
        self.assertEqual(ContentFactoryRun.objects.values().get(pk=self.run.pk), before)

    def test_completed_event_and_forged_ready_fields_cannot_prove_a_first_draft(self):
        from .website_first_article import _draft_verified
        self.state("completed")
        ContentFactoryRun.objects.filter(pk=self.run.pk).update(result={**self.run.result,
            "draft_ready_v1": {"version": 1, "revision": "c" * 64, "at": self.now.isoformat()}})
        self.run.refresh_from_db()
        self.assertFalse(_draft_verified(self.run))

    def test_independent_manual_merge_evidence_never_writes_owned_execution_status_or_clocks(self):
        from unittest.mock import Mock
        from .website_first_article import observe_manual_merge
        self.state("running")
        intent = {"pr_number": 42, "run_id": self.run.run_id, "website_connection_id": str(self.website.pk),
            "connection_generation": self.website.generation, "repository_id": self.website.repository_id,
            "base_branch": "main", "source_sha": "a" * 40, "head_sha": "d" * 40, "head_branch": "cf/article"}
        ContentFactoryRun.objects.filter(pk=self.run.pk).update(result={**self.run.result,
            "publish_merge_intent": intent, "merge_status": "manual_merge_required"})
        self.run.refresh_from_db()
        before = ContentFactoryRun.objects.values().get(pk=self.run.pk)
        repo = {"id": self.website.repository_id, "full_name": self.website.github_repo}
        response = Mock()
        response.json.return_value = {"number": 42, "merged": True, "merge_commit_sha": "e" * 40,
            "head": {"sha": "d" * 40, "ref": "cf/article", "repo": repo}, "base": {"ref": "main", "repo": repo}}
        with patch("integrations.services.github_app.create_installation_access_token",
                return_value=SimpleNamespace(token="synthetic")), \
                patch("integrations.http_client.get", return_value=response), \
                patch("integrations.http_client.delete") as revoke:
            self.assertTrue(observe_manual_merge(self.op, self.run, now=self.now))
        revoke.assert_called_once()
        after = ContentFactoryRun.objects.values().get(pk=self.run.pk)
        self.assertEqual(after["result"]["merge_response"], {"merged": True, "sha": "e" * 40})
        for field in ("status", "run_request", "created_at", "updated_at", "last_event_emitted_at", "reconciled_at", "approval_state"):
            self.assertEqual(after[field], before[field], field)
