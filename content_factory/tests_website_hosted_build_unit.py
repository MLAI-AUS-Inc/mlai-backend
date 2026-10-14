"""Hosted build and consent approval require exact saved evidence, without a database."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock,patch
from django.test import SimpleTestCase,override_settings
from django.utils import timezone
from . import website_hosted_build as hosted,website_connections as authority,website_goal as goals
from . import tests_setup_merge_verification_unit as fixtures
from .website_setup_consent import seal
from .website_contract import WebsiteAuthorityError


@override_settings(ARTICLES_HOSTED_VERIFICATION_ENABLED=True,ARTICLES_WEBSITE_GOAL_RUNNER_ENABLED=True,ARTICLES_WEBSITE_GOAL_ENROLLED_ORGANIZATIONS=["7"],ARTICLES_FAILURE_TAXONOMY_POLICY_ENABLED=True,ARTICLES_FAILURE_TAXONOMY_ENROLLED_ORGANIZATIONS=["7"])
class HostedBuildTests(SimpleTestCase):
    def setUp(self):
        fixture=fixtures.SetupMergeVerificationUnitTests();fixture.setUp();fixture.pending_setup()
        self.run,self.website,self.head,self.base=fixture.run,fixture.website,fixture.head,fixture.base
        self.website.organization_id=7;self.website.branch="main";self.website.app_root="";self.website.blockers=[]
        self.website.scan_snapshots=Mock()
        rows=self.website.scan_snapshots.filter.return_value
        rows.order_by.return_value.first.return_value=SimpleNamespace(source_sha=self.base)
        rows.exclude.return_value.filter.return_value.order_by.return_value.first.return_value=SimpleNamespace(source_sha=self.base,evidence={"scan_complete":True})
        self.run.run_request["branch"]="main"
        self.run.result["directory_quality_gates"].update(local_build=False,local_build_skipped=True)
        self.run.result["live_preview"].update(previewMode="platform_deployment",platformStatus="ready",renderConfidence="hosted",branchName="main")
        now=timezone.now()
        receipt={"version":1,"runId":self.run.run_id,"commitSha":self.head,"branch":"main","builderRepo":"fixture/builder","builderRunId":"123","dispatchNonce":"a"*12,"status":"passed","exactRender":True,"completedAt":now.isoformat()}
        receipt["digest"]=goals.digest(receipt)
        self.run.result["live_preview"]["proof"]={"builderRepo":"fixture/builder","builderDispatchNonce":"a"*12,"hostedBuildReceipt":receipt}
        self.goal=goals.new_goal("prepare",goals.scope_for(self.website,"company"),now=now)
        self.goal["consent"]=seal(self.goal,{"version":1,"publish_mode":"auto","page_policy":"auto","page_mode":"existing","page_route":"/stories","ci_mode":"repository","billing":{"policy":"reserved","expected_points":6,"accepted":True}},user_id=1,now=now)

    def verified(self):
        return authority._verified_setup_run_base(self.run,self.head,self.website,automatic_approval=True)

    def test_static_hosted_build_accepts_current_scan_even_when_verified_sha_is_stale(self):
        self.website.verified_sha="c"*40
        self.assertEqual(self.verified(),self.base)
        self.assertEqual(self.run.result["directory_quality_gates"]["local_build"],False)

    def test_missing_wrong_digest_run_nonce_head_branch_fallback_or_incomplete_gates_never_pass(self):
        valid=deepcopy(self.run.result)
        for field,value in (("digest","b"*64),("runId","foreign"),("dispatchNonce","b"*12),("commitSha",self.base),("version",True),("completedAt","invalid")):
            self.run.result=deepcopy(valid);self.run.result["live_preview"]["proof"]["hostedBuildReceipt"][field]=value
            self.assertEqual(self.verified(),"",field)
        for field,value in (("exactRender",False),("branchName","other"),("renderConfidence","fallback"),("proof",{})):
            self.run.result=deepcopy(valid);self.run.result["live_preview"][field]=value
            self.assertEqual(self.verified(),"",field)
        for field in ("browser","static_policy","visual_style","publish_surface"):
            self.run.result=deepcopy(valid);self.run.result["directory_quality_gates"][field]=False
            self.assertEqual(self.verified(),"",field)
        self.run.result=deepcopy(valid);self.run.result["directory_quality_verification"]["resume_generation"]=2
        self.assertEqual(self.verified(),"")

    def approve(self):
        from .website_prepare import approve_prepared_setup
        return approve_prepared_setup(self.run,SimpleNamespace(company=SimpleNamespace(pk="company")),SimpleNamespace(requires_review=False,website_connection=self.website),user=SimpleNamespace(pk=1),goal=self.goal)

    def test_auto_consent_uses_existing_approval_control_and_review_consent_asks(self):
        from .vibe_marketing_views import VibeMarketingRunControlView
        with patch.object(authority,"verify_repository_head",return_value=self.base) as verify,patch.object(VibeMarketingRunControlView,"post",return_value=SimpleNamespace(status_code=200)) as approve:
            self.assertEqual(self.approve(),{"status":"pending"})
        approve.assert_called_once();verify.assert_called_once_with(self.website,self.base)
        choices={**self.goal["consent"]["answers"],"publish_mode":"review"};self.goal["consent"]=seal(self.goal,choices,user_id=1,now=timezone.now())
        with patch.object(VibeMarketingRunControlView,"post") as approve:
            self.assertEqual(self.approve()["user_action"]["id"],"review_setup")
        approve.assert_not_called()

    def test_auto_consent_without_build_receipt_repairs_instead_of_asking_for_review(self):
        self.run.result["live_preview"]["proof"]={}
        with self.assertRaises(WebsiteAuthorityError) as raised:self.approve()
        self.assertEqual(raised.exception.code,"setup_preview_verification_required")
        self.assertTrue(raised.exception.retryable)

    def test_source_change_and_foreign_consent_cannot_approve(self):
        with patch.object(hosted,"current_scan_source",return_value="c"*40):
            with self.assertRaises(WebsiteAuthorityError) as raised:self.approve()
        self.assertEqual(raised.exception.code,"website_source_changed")
        self.goal["consent"]["scope"]["company_id"]="foreign"
        with self.assertRaises(WebsiteAuthorityError):self.approve()
