"""Event hints never grant milestone authority or discard a running claim."""
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch
from django.test import SimpleTestCase, override_settings
from django.utils import timezone
from . import website_wakeups as wakes, website_goal_scope as scopes, website_goal as goals
from . import tests_website_goal_unit as fixtures
from . import tests_prepare_journey_unit as query_fixtures


class EventHintTests(SimpleTestCase):
    def test_supported_event_shapes_and_malformed_inputs(self):
        base={"repository":{"id":12},"installation":{"id":4}}
        payloads=(("installation",{**base,"action":"new_permissions_accepted"},"access"),
            ("installation_repositories",{**base,"action":"added","repositories_added":[{"id":12}]},"access"),
            ("pull_request",{**base,"action":"closed","pull_request":{"number":7,"merged":True,"head":{"sha":"a"*40},"base":{"ref":"main"}}},"merged"),
            ("check_suite",{**base,"action":"completed","check_suite":{"head_sha":"a"*40,"conclusion":"success"}},"previewed"),
            ("check_run",{**base,"action":"completed","check_run":{"head_sha":"a"*40,"conclusion":"failure"}},"previewed"),
            ("deployment_status",{**base,"deployment":{"sha":"a"*40,"ref":"main"},"deployment_status":{"state":"success","target_url":"https://private.invalid/secret"}},"live"))
        for event,payload,milestone in payloads:
            with self.subTest(event=event):
                signal=wakes.signal_for(event,payload)
                self.assertEqual(signal["milestone"],milestone)
                self.assertNotIn("target_url",str(signal))
                if event not in {"installation","installation_repositories"}:
                    self.assertIsNone(wakes.signal_for(event,{**payload,"repository":{"id":True}}))
        for event in ("pull_request","check_run","check_suite","deployment_status"):
            for payload in (None,[],{}, {"repository":{"id":12},event:[]}):
                self.assertIsNone(wakes.signal_for(event,payload))

    def test_foreign_pr_source_branch_installation_or_generation_cannot_wake(self):
        op=fixtures.GoalRunnerRegressions().operation()
        connection=op.connection
        connection.installation_id,connection.verified_sha="4","a"*40
        child=SimpleNamespace(run_request={"website_connection_id":str(connection.pk),"connection_generation":3,"repository_id":12},
            result={"pr_number":7,"branch_commit_sha":"b"*40})
        signal={"installation_id":"4","repository_ids":[12],"milestone":"merged","pr_number":7,"sha":"b"*40,"branch":"main"}
        self.assertTrue(wakes.matches(connection,op,signal,child))
        for change in ({"installation_id":"5"},{"repository_ids":[99]},{"pr_number":8},{"sha":"c"*40},{"branch":"other"}):
            self.assertFalse(wakes.matches(connection,op,{**signal,**change},child))
        child.run_request["connection_generation"]=2
        self.assertFalse(wakes.matches(connection,op,signal,child))

    def test_deterministic_terminal_and_budget_stops_remain_inert(self):
        op=fixtures.GoalRunnerRegressions().operation()
        signal={"success":True}
        for state in ("completed","cancelled","deleted","denied","stopped"):
            op.state=state
            self.assertFalse(wakes.policy_allows(op,signal))
        op.state="needs_attention"
        for classification in ("repairable","strategy","unsupported"):
            op.receipt["failure_class"]=classification
            self.assertFalse(wakes.policy_allows(op,signal))
        op.receipt["failure_class"]="transient"
        self.assertTrue(wakes.policy_allows(op,signal))
        op.receipt["budget_stop_reason"]="transient_attempts"
        self.assertFalse(wakes.policy_allows(op,signal))


class SourceScopeTests(SimpleTestCase):
    def test_only_a_complete_source_chain_rebinds_same_identity(self):
        old=fixtures.scope();current={**old,"configuration_revision":3}
        records=[{"from_revision":1,"to_revision":2,"source_sha":"b"*40},{"from_revision":2,"to_revision":3,"source_sha":"b"*40}]
        self.assertEqual(scopes.transition_chain(old,current,records,source_sha="b"*40),records)
        self.assertIsNone(scopes.transition_chain(old,current,records[1:],source_sha="b"*40))
        self.assertIsNone(scopes.transition_chain(old,current,records,source_sha="c"*40))
        self.assertIsNone(scopes.transition_chain(old,{**current,"branch":"other"},records,source_sha="b"*40))

    def test_rebind_preserves_goal_budget_identity_and_invalidates_proofs(self):
        now=timezone.now()
        goal=goals.refresh_goal(goals.new_goal("goal",fixtures.scope(),now=now),fixtures.proof_set(now),scope=fixtures.scope(),source_sha="a"*40,now=now)
        goal["budget_used"]={"active_seconds":123,"model_cost":2}
        connection=fixtures.GoalRunnerRegressions().operation().connection
        connection.configuration_version=3;connection.verified_sha="b"*40
        rows=query_fixtures.FrozenPrepareContextTests.rows()
        rows.__getitem__.return_value=[SimpleNamespace(payload={"from_revision":1,"to_revision":2,"source_sha":"b"*40}),SimpleNamespace(payload={"from_revision":2,"to_revision":3,"source_sha":"b"*40})]
        connection.operations=rows
        result=scopes.rebind_source_scope(goal,connection,company_id=9,now=now)
        self.assertEqual(result["goal_id"],goal["goal_id"])
        self.assertEqual(result["budget_used"],goal["budget_used"])
        self.assertEqual(result["started_at"],goal["started_at"])
        self.assertEqual(result["scope"]["configuration_revision"],3)
        self.assertFalse(goals.complete(result))
        self.assertTrue(result["scope_history"])


@override_settings(ARTICLES_WEBSITE_EVENT_WAKES_ENABLED=True,
    ARTICLES_WEBSITE_GOAL_RUNNER_ENABLED=True,ARTICLES_WEBSITE_GOAL_ENROLLED_ORGANIZATIONS=["7"],
    ARTICLES_FAILURE_TAXONOMY_POLICY_ENABLED=True,ARTICLES_FAILURE_TAXONOMY_ENROLLED_ORGANIZATIONS=["7"])
class WakeExecutionTests(SimpleTestCase):
    def test_running_claim_keeps_its_state_and_durable_wake_remains_due(self):
        from contextlib import nullcontext
        from .website_models import WebsiteConnectionOperation
        op=fixtures.GoalRunnerRegressions().operation()
        op.payload["company_id"]="9"
        op.state="running";op.next_attempt_at=timezone.now()+timedelta(minutes=5)
        op.connection.organization_id=7
        wake=SimpleNamespace(pk="wake",connection=op.connection,connection_id=op.connection.pk,generation=3,state="pending",next_attempt_at=None,
            payload={"prepare_operation_id":str(op.pk),"signal":{"success":True}},receipt={},save=Mock())
        rows=query_fixtures.FrozenPrepareContextTests.rows(first=wake)
        locked=query_fixtures.FrozenPrepareContextTests.rows(first=op);locked.get.return_value=wake
        with patch.object(WebsiteConnectionOperation.objects,"select_related",return_value=rows),patch.object(WebsiteConnectionOperation.objects,"select_for_update",return_value=locked), \
            patch.object(wakes.transaction,"atomic",return_value=nullcontext()),patch.object(wakes.cache,"add",return_value=True),patch.object(wakes.cache,"get"), \
            patch("content_factory.website_prepare.advance_prepare") as advance:
            self.assertEqual(wakes.advance_wake("wake"),"pending")
            advance.assert_not_called()
        self.assertEqual(op.state,"running")
        op.save.assert_not_called()
        self.assertIsNotNone(wake.next_attempt_at)

    def test_queue_is_durable_and_duplicate_event_registers_only_one_fast_pass(self):
        from contextlib import nullcontext
        from .website_models import WebsiteConnection, WebsiteConnectionOperation
        from organizations.models import Organization
        op=fixtures.GoalRunnerRegressions().operation()
        op.payload["company_id"]="9"
        connection=op.connection
        connection.operations=query_fixtures.FrozenPrepareContextTests.rows(first=op)
        wake=SimpleNamespace(pk="wake",payload={},state="pending",next_attempt_at=None,save=Mock())
        calls=0
        def reserve(**kw):
            nonlocal calls
            calls+=1
            if calls==1:
                wake.payload=kw["defaults"]["payload"]
                wake.next_attempt_at=kw["defaults"]["next_attempt_at"]
            return wake,calls==1
        with patch.object(WebsiteConnection.objects,"select_related",return_value=query_fixtures.FrozenPrepareContextTests.rows(first=connection)), \
             patch.object(WebsiteConnection.objects,"select_for_update") as connections,patch.object(Organization.objects,"select_for_update"), \
             patch.object(WebsiteConnectionOperation.objects,"get_or_create",side_effect=reserve),patch.object(wakes.transaction,"atomic",side_effect=lambda:nullcontext()), \
             patch.object(wakes.transaction,"on_commit") as committed,patch.object(wakes,"submit_immediate") as immediate:
            connections.return_value.get.return_value=connection
            signal={"kind":"trusted_callback","success":True}
            self.assertIs(wakes.queue_wake(connection.pk,signal=signal,expected_generation=3),wake)
            self.assertIs(wakes.queue_wake(connection.pk,signal=signal,expected_generation=3),wake)
            committed.assert_called_once()
            immediate.assert_not_called()
            self.assertIsNotNone(wake.next_attempt_at)
            self.assertEqual(wake.payload["prepare_operation_id"],str(op.pk))
            op.save.assert_not_called()

    def test_connection_lock_declines_a_concurrent_pass(self):
        from .website_models import WebsiteConnectionOperation
        op=fixtures.GoalRunnerRegressions().operation()
        wake=SimpleNamespace(pk="wake",connection=op.connection,connection_id=op.connection.pk,generation=3,state="pending",next_attempt_at=None)
        with patch.object(WebsiteConnectionOperation.objects,"select_related",return_value=query_fixtures.FrozenPrepareContextTests.rows(first=wake)), \
             patch.object(wakes.cache,"add",return_value=False),patch("content_factory.website_prepare.advance_prepare") as advance:
            self.assertIsNone(wakes.advance_wake("wake"))
            advance.assert_not_called()

    def test_malformed_verified_payload_is_ignored_without_legacy_handler_calls(self):
        from . import github_webhook
        with patch.object(github_webhook,"handle_pull_request") as old,patch.object(wakes,"wake_from_event") as wake:
            result=github_webhook.process_github_event("pull_request",{"repository":{"id":12},"pull_request":["invalid"]})
            self.assertEqual(result["reason"],"malformed_event")
            old.assert_not_called();wake.assert_not_called()
