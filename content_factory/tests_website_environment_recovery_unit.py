"""CF-bound partial builds recover without owner approval, DB access or providers."""
from copy import deepcopy
from datetime import timedelta
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings
from django.utils import timezone

from . import website_goal as goals, website_hosted_build as hosted
from . import website_prepare as prepare, website_goal_runner as runner, website_strategy as strategy
from . import tests_website_hosted_build_unit as hosted_fixtures, tests_website_goal_unit as goal_fixtures
from .website_contract import WebsiteAuthorityError
from .website_setup_consent import seal


@override_settings(ARTICLES_HOSTED_ENVIRONMENT_RECOVERY_ENABLED=True, ARTICLES_HOSTED_VERIFICATION_ENABLED=True,
    ARTICLES_WEBSITE_GOAL_RUNNER_ENABLED=True, ARTICLES_WEBSITE_GOAL_ENROLLED_ORGANIZATIONS=["7"],
    ARTICLES_FAILURE_TAXONOMY_POLICY_ENABLED=True, ARTICLES_FAILURE_TAXONOMY_ENROLLED_ORGANIZATIONS=["7"])
class EnvironmentRecoveryTests(SimpleTestCase):
    def setUp(self):
        fixture=hosted_fixtures.HostedBuildTests();fixture.setUp()
        self.run,self.website,self.head,self.base,self.goal=fixture.run,fixture.website,fixture.head,fixture.base,fixture.goal
        self.approve=fixture.approve
        self.preview = self.run.result["live_preview"]
        self.proof = self.preview["proof"]
        self.now = timezone.now()
        self.run.result["directory_quality_verification"]["completed_at"] = self.now.isoformat()
        self.branch = "cf-review/" + self.run.run_id
        self.preview["branchName"] = self.branch
        self.preview["builderRunUrl"] = "https://github.com/fixture/builder/actions/runs/123"
        self.proof.update(branch=self.branch, sourceRepo=self.website.github_repo, environmentRequired=True,
            sourceBuildPlanDigest="c"*64, sourceBuildPublicInputsDigest="d"*64)
        self.proof.pop("hostedBuildReceipt")
        request = {"schema_version":1, "request_digest":"e"*64, "build_request_digest":"f"*64,
            "fingerprint_digest":"a"*64, "source_sha":self.base, "source_ref":"main", "app_root":""}
        self.preview["requestIdentity"] = deepcopy(request)
        self.record = {"schema_version":1, "status":"bound_to_attempt", "run_id":self.run.run_id, "commit_sha":self.head,
            "branch":self.branch, "dispatch_nonce":"a"*12, "builder_repo":"fixture/builder", "builder_run_id":"123",
            "builder_revision":"e"*40, "request_identity":request, "broker_record":{"source_sha":self.head,
                "source_plan_digest":"c"*64, "public_inputs_digest":"d"*64},
            "identity_complete":False, "reproducible":False}
        self.sign()

    def sign(self):
        body = {key:value for key,value in self.record.items() if key != "digest"}
        self.record["digest"] = hashlib.sha256(json.dumps(body,sort_keys=True,separators=(",",":"),allow_nan=False).encode()).hexdigest()
        self.proof["hostedEnvironmentReceipt"] = self.record

    def matches(self):
        return hosted.partial_environment(self.run,self.preview,self.head,self.website,now=self.now)

    def test_current_partial_attempt_selects_strategy_without_approval_or_provider_lookup(self):
        from .vibe_marketing_views import VibeMarketingRunControlView
        self.assertTrue(self.matches())
        with patch.object(VibeMarketingRunControlView,"post") as approve, patch("content_factory.website_connections.verify_repository_head") as verify:
            with self.assertRaises(WebsiteAuthorityError) as raised:
                self.approve()
        self.assertEqual(raised.exception.code,"prepare_no_progress")
        self.assertEqual(raised.exception.findings,[{"constraint":"incomplete_environment_identity"}])
        approve.assert_not_called();verify.assert_not_called()

    def test_recovery_flag_off_or_unenrolled_preserves_waiting(self):
        with override_settings(ARTICLES_HOSTED_ENVIRONMENT_RECOVERY_ENABLED=False):
            self.assertFalse(self.matches())
            with self.assertRaises(WebsiteAuthorityError) as raised:self.approve()
            self.assertEqual(raised.exception.code,"setup_preview_verification_required")
        with override_settings(ARTICLES_WEBSITE_GOAL_ENROLLED_ORGANIZATIONS=[]):
            self.assertFalse(self.matches())

    def test_owner_requested_review_stays_review_even_for_a_partial_receipt(self):
        choices={**self.goal["consent"]["answers"],"publish_mode":"review"}
        self.goal["consent"]=seal(self.goal,choices,user_id=1,now=self.now)
        self.assertEqual(self.approve()["user_action"]["id"],"review_setup")
        self.goal["consent"]=seal(self.goal,{**choices,"publish_mode":"auto"},user_id=1,now=self.now)
        context=SimpleNamespace(company=SimpleNamespace(pk="company"))
        result=prepare.approve_prepared_setup(self.run,context,
            SimpleNamespace(requires_review=True,website_connection=self.website),user=SimpleNamespace(pk=1),goal=self.goal)
        self.assertEqual(result["user_action"]["id"],"review_setup")

    def test_ascii_escaped_binding_matches_unicode_app_root_without_changing_c3_digest(self):
        self.website.app_root="apps/caf\u00e9"
        self.record["request_identity"]["app_root"]=self.website.app_root
        self.preview["requestIdentity"]=deepcopy(self.record["request_identity"])
        self.sign()
        self.assertTrue(self.matches())
        wrong=goals.digest({key:value for key,value in self.record.items() if key!="digest"})
        self.assertNotEqual(wrong,self.record["digest"])
        self.proof["hostedEnvironmentReceipt"]={**self.record,"digest":wrong}
        self.assertFalse(self.matches())

    def test_oversized_or_nonfinite_record_cannot_drive_recovery(self):
        self.record["broker_record"]["unsupported_payload"]="x"*(96*1024)
        self.sign()
        self.assertFalse(self.matches())
        self.record["broker_record"]["unsupported_payload"]=float("nan")
        self.assertFalse(self.matches())

    def test_missing_complete_or_malformed_receipt_is_not_a_partial_outcome(self):
        valid=deepcopy(self.record)
        for changed in (None,{},[],{**valid,"identity_complete":True},{**valid,"identity_complete":0},
                {**valid,"schema_version":True},{**valid,"digest":"f"*64}):
            with self.subTest(changed=type(changed)):
                self.proof["hostedEnvironmentReceipt"]=changed
                self.assertFalse(self.matches())

    def test_rehashed_foreign_bindings_cannot_drive_recovery(self):
        valid=deepcopy(self.record)
        for field,value in (("run_id","other"),("commit_sha",self.base),("branch","foreign"),
                ("dispatch_nonce","b"*12),("builder_repo","foreign/builder"),("builder_run_id","124"),
                ("builder_revision","invalid"),("status","unbound"),("reproducible",True)):
            with self.subTest(field=field):
                self.record={**deepcopy(valid),field:value};self.sign()
                self.assertFalse(self.matches())
        self.record=valid;self.sign()
        for field,value in (("source_sha","c"*40),("source_ref","foreign"),("app_root","foreign"),
                ("schema_version",True),("request_digest","invalid")):
            self.record=deepcopy(valid);self.record["request_identity"][field]=value;self.sign()
            self.preview["requestIdentity"]=deepcopy(self.record["request_identity"])
            self.assertFalse(self.matches(),field)

    def test_foreign_source_scope_plan_or_public_inputs_are_not_current(self):
        valid=deepcopy(self.run.run_request)
        for field,value in (("website_connection_id","other"),("connection_generation",True),
                ("connection_generation",self.website.generation+1),("repository_id",124),("branch","other")):
            self.run.run_request={**valid,field:value}
            self.assertFalse(self.matches(),field)
        self.run.run_request=valid
        for field in ("source_plan_digest","public_inputs_digest","source_sha"):
            original=deepcopy(self.record)
            self.record["broker_record"][field]="f"*64;self.sign()
            self.assertFalse(self.matches(),field)
            self.record=original;self.sign()

    def test_stale_future_nonhosted_or_mismatched_preview_cannot_drive_recovery(self):
        original=deepcopy(self.preview)
        for field,value in (("builderRunUrl","https://github.com/foreign/builder/actions/runs/123"),
                ("platformStatus","building"),("renderConfidence","fallback"),("exactRender",False),
                ("branchName","foreign"),("commitSha",self.base),("requestIdentity",{})):
            self.preview[field]=value;self.assertFalse(self.matches(),field)
            self.preview.clear();self.preview.update(deepcopy(original));self.proof=self.preview["proof"]
        for at in (self.now-timedelta(days=8),self.now+timedelta(hours=1)):
            self.run.result["directory_quality_verification"]["completed_at"]=at.isoformat()
            self.assertFalse(self.matches())

    def test_strategy_outcome_runs_through_actual_failure_journal_once(self):
        op=goal_fixtures.GoalRunnerRegressions().operation()
        op.connection=self.website
        op.connection_id,op.generation=self.website.pk,self.website.generation
        op.payload["company_id"]="company"
        op.receipt["goal"]=deepcopy(self.goal)
        op.receipt["goal"]["current_source_sha"]=self.base
        op.receipt["goal"]=strategy.set_pin(op.receipt["goal"],0,source=self.base,fingerprint="f"*64,now=self.now,reason="fixture_inventory")
        op.receipt.update(setup_run_id=self.run.run_id,child_run_id=self.run.run_id)
        with override_settings(ARTICLES_WEBSITE_STRATEGY_ENABLED=True),self.assertRaises(WebsiteAuthorityError) as raised:
            self.approve()
        with override_settings(ARTICLES_WEBSITE_STRATEGY_ENABLED=True):
            prepare.prepare_failure(op,"setup",raised.exception,now=self.now)
            self.assertEqual(op.receipt["failure_class"],"strategy")
            runner.finish_goal(op,now=self.now)
            self.assertEqual(op.state,"pending")
            self.assertEqual(op.receipt["goal"]["strategy_pin"]["rung"],"native_collection")
            self.assertIsNone(op.receipt["user_action"])
            epoch=op.payload["retry_epoch"]
            runner.finish_goal(op,now=self.now)
            self.assertEqual(op.payload["retry_epoch"],epoch)
