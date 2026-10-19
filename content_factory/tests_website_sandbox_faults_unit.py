"""Actual isolated-hook selection, delay and lifecycle boundaries; no effects local."""
from copy import deepcopy
from datetime import timedelta
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase
from django.utils import timezone

from . import website_sandbox_faults as faults, website_goal as goals


class SandboxControllerBoundaryTests(SimpleTestCase):
    def setUp(self):
        self.directory = TemporaryDirectory(); self.addCleanup(self.directory.cleanup)
        self.now = timezone.now()
        self.context = SimpleNamespace(company=SimpleNamespace(pk="company"), organization=SimpleNamespace(pk=7))
        self.connection = SimpleNamespace(pk="connection", generation=2, repository_id=19,
            organization_id=7, organization=SimpleNamespace(domain="fixture.example"), state="connected",
            github_repo="fixture/mlai-articles-fixture-html", branch="main", app_root="", configuration_version=3, blockers=[])
        goal = goals.new_goal("goal", goals.scope_for(self.connection, "company"), now=self.now)
        self.op = SimpleNamespace(pk="goal", action="prepare", state="running", generation=2,
            connection=self.connection, payload={"company_id":"company"}, receipt={"goal":goal})
        self.case = {"case_id":"fixture-delay", "fault":"merge_delay", "company_id":"company",
            "website_connection_id":"connection", "connection_generation":2, "repository_id":19,
            "github_repo":self.connection.github_repo, "domain":"fixture.example", "sandbox":True,
            "max_injections":1, "parameters":{"delay_seconds":2}}
        self.config = {"schema_version":1, "deployment_id":"articles-acceptance-unit", "manifest_digest":"a"*64,
            "controller_revision":"b"*40, "expires_at":(self.now+timedelta(hours=1)).isoformat(), "cases":[self.case]}
        self.path = Path(self.directory.name).resolve()/"frozen.json"; self.write()
        self.enterContext(patch.dict("os.environ", {"APP_ENV":"sandbox", "ARTICLES_SANDBOX_FAULT_MODE":"isolated-acceptance",
            "CONTENT_FACTORY_SANDBOX_FAULT_CONFIG":str(self.path), "ONE_CLICK_SANDBOX_CONTROL_URL":"https://control.example.test",
            "ONE_CLICK_SANDBOX_CONTROL_TOKEN":"synthetic-control-token-value-32-chars"}))
        self.locator = self.enterContext(patch.object(faults.scope, "_run_locator", return_value={"run_id":"setup-run"}))
        self.calls = []
        self.real_post = faults._post
        self.post = self.enterContext(patch.object(faults, "_post", side_effect=self.control))

    def write(self):
        self.path.write_text(json.dumps(self.config))

    def control(self, base, token, action, case_id, payload):
        self.calls.append((action, deepcopy(payload)))
        if action == "claim":
            binding = {key:self.case[key] for key in ("company_id","website_connection_id","connection_generation","repository_id")}
            binding.update(goal_id="goal",manifest_digest=self.config["manifest_digest"])
            return {"version":1,"claim_id":"c"*32,"fault":self.case["fault"],"parameters":self.case["parameters"],
                "case_id":case_id,**payload,"binding_digest":goals.digest(binding)}
        return {"version":1}

    def stage(self, forward, boundary="merge_observation", **kwargs):
        return faults.stage(self.op,self.context,run_id="setup-run",boundary=boundary,forward=forward,**kwargs)

    def test_defaults_leave_ordinary_path_and_make_no_observation_queries(self):
        with patch.dict("os.environ", {"APP_ENV":"production"}), patch.object(faults,"_configuration") as read:
            self.assertEqual(self.stage(lambda:"ordinary"),"ordinary")
        read.assert_not_called(); self.locator.assert_not_called(); self.post.assert_not_called()

    def test_actual_measured_delay_completes_before_forwarding_normal_effect(self):
        order = []
        def forward():
            self.assertEqual(self.calls[-1][0],"complete"); order.append("ordinary")
            return "normal-merge"
        with patch.object(faults.time,"sleep",side_effect=lambda value:order.append(("sleep",value))), \
                patch.object(faults.time,"monotonic",side_effect=[10,12.1]):
            self.assertEqual(self.stage(forward),"normal-merge")
        self.assertEqual(order,[("sleep",2),"ordinary"])
        self.assertEqual(self.calls[1][1]["facts"],{"observed":True,"delay_seconds":2})

    def test_binding_expiry_target_and_transport_fail_closed_before_any_effect(self):
        saved=deepcopy(self.config)
        variants=[("repository_id",99),("github_repo","customer/site"),("sandbox",False),("domain","vyavos.com")]
        for key,value in variants:
            self.config=deepcopy(saved); self.config["cases"][0][key]=value; self.write()
            self.assertEqual(self.stage(lambda:"ordinary"),"ordinary")
        self.config=deepcopy(saved); self.config["expires_at"]=(self.now-timedelta(seconds=1)).isoformat();self.write()
        self.stage(lambda:None)
        self.config=deepcopy(saved);self.write()
        with patch.dict("os.environ",{"ONE_CLICK_SANDBOX_CONTROL_URL":"http://customer.test/arbitrary"}):
            self.stage(lambda:None)
        self.post.assert_not_called()

    def test_unowned_or_changed_original_run_never_claims(self):
        self.locator.return_value=None
        self.assertEqual(self.stage(lambda:"ordinary"),"ordinary")
        self.post.assert_not_called()

    def test_contradictory_claim_never_sleeps_or_disconnects(self):
        self.post.return_value={};self.post.side_effect=None
        with patch.object(faults.time,"sleep") as sleep:
            self.assertEqual(self.stage(lambda:"ordinary"),"ordinary")
        sleep.assert_not_called()

    def test_interrupt_or_unobserved_elapsed_time_aborts_without_applied_receipt(self):
        with patch.object(faults.time,"sleep"), patch.object(faults.time,"monotonic",side_effect=[10,10.1]), \
                self.assertRaises(faults.SandboxFaultUnavailable):
            self.stage(lambda:self.fail("ordinary effect before measured delay"))
        self.assertEqual([call[0] for call in self.calls],["claim","abort"])

    def test_disconnect_fault_calls_only_the_source_owned_lifecycle_closure(self):
        self.case["fault"]="disconnect_refund";self.write()
        effect=Mock(return_value={"observed":True,"before_generation":2,"after_generation":3,"settlement_id":"lifecycle"})
        forward=Mock()
        self.stage(forward,boundary="refund_settlement",disconnect=effect)
        effect.assert_called_once();forward.assert_not_called()
        self.assertEqual(self.calls[-1][1]["facts"]["after_generation"],3)

    def test_whole_goal_budget_boundary_observes_committed_settlement_without_model_denial(self):
        self.case["fault"]="budget_exhaustion";self.write()
        self.op.state="needs_attention";self.op.receipt["budget_stop_reason"]="active_compute"
        observed=Mock(return_value={"observed":True,"settlement_id":"actual-original-settlement"})
        with patch.object(faults.time,"sleep") as sleep:
            self.stage(lambda:None,boundary="goal_budget",observe_budget=observed)
        observed.assert_called_once();sleep.assert_not_called()
        self.assertEqual(self.calls[-1][1]["facts"],{"observed":True,"settlement_id":"actual-original-settlement"})

    def test_original_operator_cap_cannot_be_raised_rebound_or_selected_by_caller(self):
        from . import website_goal_budget as budget
        from django.test import override_settings
        self.case["fault"]="budget_exhaustion"
        self.case["goal_budget_profile"]={"active_seconds":0.5};self.write()
        with patch("content_factory.website_owner_transport.required",return_value=True), \
                override_settings(ARTICLES_WEBSITE_GOAL_BUDGETS_ENABLED=True,ARTICLES_GOAL_ACTIVE_SECONDS=7200):
            frozen=faults.admit_budget_profile(self.op,self.op.receipt["goal"])
            self.assertEqual(budget.active_limit(frozen),0.5)
            frozen["budget_used"]["active_seconds"]=0.6
            self.assertEqual(budget.exhaustion(frozen,now=self.now),"active_compute")
            frozen["scope"]["configuration_revision"]+=1
            self.assertEqual(budget.active_limit(frozen),0.5)
            frozen["sandbox_budget_profile"]["active_seconds"]=9000
            self.assertIsNone(budget.active_limit(frozen))
            self.assertEqual(budget.exhaustion(frozen,now=self.now),"invalid_budget_contract")

    def test_ordinary_or_missing_operator_profile_never_changes_normal_limits(self):
        from . import website_goal_budget as budget
        from django.test import override_settings
        with override_settings(ARTICLES_GOAL_ACTIVE_SECONDS=7200):
            self.assertEqual(faults.admit_budget_profile(self.op,self.op.receipt["goal"]),self.op.receipt["goal"])
            self.assertEqual(budget.active_limit(self.op.receipt["goal"]),7200)

    def test_actual_control_transport_is_https_bounded_and_has_no_ambient_authority(self):
        import requests
        from .tests_website_runtime_observation_unit import response
        token="synthetic-control-token-value-32-chars"
        with patch.object(requests.Session,"send",return_value=response({"version":1})) as send:
            self.assertEqual(self.real_post("https://control.example.test",token,"claim","fixture",{"goal_id":"goal"}),{"version":1})
        prepared=send.call_args.args[0]
        self.assertEqual(prepared.url,"https://control.example.test/v1/claim/fixture")
        self.assertEqual(prepared.headers["Authorization"],"Bearer "+token)
        self.assertTrue(send.call_args.kwargs["verify"])
        self.assertFalse(send.call_args.kwargs["allow_redirects"])
        self.assertEqual(send.call_args.kwargs["proxies"],{})
        with patch.object(requests.Session,"send",return_value=response({"oversize":"x"*70000})), \
                self.assertRaises(faults.SandboxFaultUnavailable):
            self.real_post("https://control.example.test",token,"claim","fixture",{"goal_id":"goal"})
