"""Diagnostic redaction, real scoped reads and measured outcomes without a database."""
import json
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch
from django.test import SimpleTestCase, override_settings
from django.utils import timezone
from . import website_doctor as doctor, website_goal as goals
from . import tests_website_goal_unit as fixtures
from .one_click_metrics import summarize_goals
from .website_health import waiting_is_overdue


class DoctorTests(SimpleTestCase):
    def setUp(self):
        self.now = timezone.now()
        self.op = fixtures.GoalRunnerRegressions().operation()
        self.op.connection.verified_sha = "a" * 40
        self.op.receipt["goal"] = goals.refresh_goal(self.op.receipt["goal"], fixtures.proof_set(self.now), scope=fixtures.scope(), source_sha="a" * 40, now=self.now)

    def project(self, **kwargs):
        return doctor.project_doctor(self.op, scope=fixtures.scope(), source_sha="a" * 40, **kwargs)

    def test_copy_contract_keeps_proof_and_omits_every_arbitrary_body(self):
        secret = "github_pat_redactionCanary"
        self.op.receipt.update(message=secret, result={"source": secret}, user_action={"url": f"https://example.test/?token={secret}"}, retry_epoch=secret)
        self.op.payload.update(attempt=2, retry_epoch=secret)
        self.op.receipt["goal"]["budget_used"].update(model_cost=0, active_seconds=None, message=secret)
        projected = self.project(children=[{"runId":secret,"status":"running"}], receipts=[{"kind":"ci","operationId":"ci", "status":"passed","sourceSha":"a"*40,"digest":"b"*64,"body":secret}])
        self.assertNotIn(secret, json.dumps(projected))
        self.assertIsNone(projected["retryEpoch"])
        self.assertEqual(projected["attempt"], 2)
        self.assertEqual(projected["budget"]["modelUsd"], 0)
        self.assertIsNone(projected["budget"]["activeSeconds"])
        self.assertEqual(projected["checks"][1]["proof"]["kind"], "repository_scan")
        self.assertNotIn("scope", projected["checks"][1]["proof"])

    def test_cross_scope_and_source_never_present_valid_proof(self):
        self.op.receipt["goal"]["milestones"][1]["proof"]["scope"]["company_id"] = "foreign"
        self.assertIsNone(self.project()["checks"][1]["proof"])
        self.assertEqual(self.project()["checks"][1]["state"], "pending")
        self.op.receipt["goal"]["scope"]["connection_generation"] = 4
        self.assertIsNone(self.project()["goalId"])
        self.assertEqual(self.project()["checks"], [])

    def test_malformed_and_unbounded_containers_are_safe(self):
        for value in ([], {}, True, "private body"):
            with self.subTest(value=value):
                self.op.receipt["goal"]["strategy_history"] = [value]
                result=self.project(milestones=[{"name":value,"state":value}],children=[{"runId":"run","status":value}],receipts=[{"kind":value,"operationId":"receipt"}])
                self.assertEqual(result["checks"], [])
                self.assertEqual(result["children"], [])
                self.assertEqual(result["receipts"], [])
        self.assertIsNone(doctor.number(float("nan")))
        self.assertIsNone(doctor.number(10**1000))
        self.assertIsNone(doctor.number(True))
        for value in ("ghp_canary", "sk-canary", "xoxb-canary", "eyJcanary.e30.sig"):
            self.assertIsNone(doctor.safe_identifier(value))

    def test_actual_child_reader_is_org_connection_generation_repository_and_deletion_scoped(self):
        from workflow_runs.models import ContentFactoryRun
        self.op.connection.repository_id=12
        self.op.receipt["child_run_id"]="child"
        self.op.connection.operations=Mock()
        self.op.connection.operations.filter.return_value.order_by.return_value.first.return_value=None
        request={"website_connection_id":"website","connection_generation":3,"repository_id":12,"deletion_epoch":0}
        child=SimpleNamespace(run_request=request,status="running")
        with patch.object(ContentFactoryRun.objects,"filter") as rows:
            rows.return_value.first.return_value=child
            self.assertEqual(doctor.read_doctor(self.op,scope=fixtures.scope(),source_sha="a"*40)["children"],[{"runId":"child","status":"running"}])
            self.assertEqual(rows.call_args.kwargs,{"run_id":"child","organization_id":7})
            for field,value in (("website_connection_id","other"),("connection_generation",4),("repository_id",13),("deletion_epoch",1)):
                child.run_request={**request,field:value}
                self.assertEqual(doctor.read_doctor(self.op,scope=fixtures.scope(),source_sha="a"*40)["children"],[])

    def test_inspector_omits_secrets_even_in_old_legacy_fields(self):
        from .management.commands.inspect_website_prepare import inspection_report
        secret="github_pat_inspectorCanary"
        connection=SimpleNamespace(pk="website",generation=3,state="connected",repository_id=12,installation_id=4,verified_sha="a"*40,blockers=[secret],capabilities={secret:True,"read":True})
        self.op.attempts=2;self.op.created_at=self.now
        self.op.receipt.update(message=secret,current_step=secret,reason_code=secret,user_action={"secret":secret})
        config=SimpleNamespace(article_system={"pending_article_system_setup":{"route_path":"/"+secret,"setup_run_id":secret}})
        report=inspection_report(connection,config,[self.op],None,[])
        self.assertNotIn(secret,json.dumps(report))
        self.assertEqual(report["connection"]["capabilities"],["read"])

    def test_recorded_observations_and_unknown_cost_are_distinct(self):
        goal=deepcopy(self.op.receipt["goal"])
        goal["started_at"]=(self.now-timedelta(seconds=50)).isoformat()
        goal["milestones"][0].pop("observed_at")
        goal["attempts_by_class"]["repairable"]=2
        rows=[{"receipt":{"goal":goal,"current_step":"setup","reason_code":"worker_unavailable"}}]
        report=summarize_goals(rows)
        self.assertEqual(report["milestoneSeconds"]["understood"]["p50"],50)
        self.assertEqual(report["milestoneSeconds"]["access"]["sample"],0)
        self.assertEqual(report["milestoneSeconds"]["access"]["unknown"],1)
        self.assertEqual(report["classMix"]["repairable"],2)
        self.assertEqual(report["modelUsd"]["unknown"],1)
        goal["budget_used"]["model_cost"]=0
        self.assertEqual(summarize_goals(rows)["modelUsd"]["meanUpperBound"],0)
        rows[0]["receipt"]["reason_code"]="github_pat_metricsCanary"
        self.assertEqual(summarize_goals(rows)["reasonCounts"],{})

    @override_settings(ARTICLES_GOAL_WALL_SECONDS=172800)
    def test_waits_use_deadlines_and_owner_waits_never_become_overdue(self):
        row={"state":"needs_user","receipt":{"goal":self.op.receipt["goal"]},"created_at":self.now-timedelta(days=30)}
        self.assertFalse(waiting_is_overdue(row,now=self.now+timedelta(days=30)))
        row["state"]="pending"
        row["receipt"]["goal"]["started_at"]=(self.now-timedelta(hours=47)).isoformat()
        self.assertFalse(waiting_is_overdue(row,now=self.now))
        self.assertTrue(waiting_is_overdue(row,now=self.now+timedelta(hours=1)))
        row["receipt"]={};row["created_at"]=self.now-timedelta(hours=25)
        self.assertTrue(waiting_is_overdue(row,now=self.now))
        row["created_at"]=self.now
        self.assertFalse(waiting_is_overdue(row,now=self.now))
        row["created_at"]=None
        self.assertTrue(waiting_is_overdue(row,now=self.now))

    @override_settings(ARTICLES_WEBSITE_STATUS_V1_ENABLED=True, ARTICLES_WEBSITE_DOCTOR_ENABLED=False)
    def test_feature_off_does_not_add_doctor_queries_or_contract(self):
        from . import website_status as status
        rows=Mock();self.op.connection.operations=rows
        rows.filter.return_value.filter.return_value.order_by.return_value.first.return_value=self.op
        self.op.connection.github_repo="fixture/site"
        self.op.next_attempt_at=self.now+timedelta(seconds=30)
        context=SimpleNamespace(company=SimpleNamespace(pk=9))
        config=SimpleNamespace(website_connection=self.op.connection)
        with patch.object(status,"source_for",return_value="a"*40),patch.object(doctor,"read_doctor") as read:
            result=status.status_for_context(context,config,journey={},now=self.now)
        self.assertNotIn("doctor",result);read.assert_not_called()
