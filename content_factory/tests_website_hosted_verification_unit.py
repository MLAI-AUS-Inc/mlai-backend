"""Authenticated merged-source promotion and pending/cancel fences without a database."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock,patch
from contextlib import nullcontext
from django.test import SimpleTestCase,override_settings
from . import website_hosted_verification as hosted,website_verification as verification,website_goal as goals
from .website_contract import WebsiteAuthorityError,evidence_digest
from . import tests_prepare_permissions_unit as permission_fixtures


@override_settings(ARTICLES_HOSTED_ATTESTATION_ENABLED=True,ARTICLES_HOSTED_VERIFICATION_ENABLED=True,ARTICLES_WEBSITE_GOAL_RUNNER_ENABLED=True,ARTICLES_WEBSITE_GOAL_ENROLLED_ORGANIZATIONS=["7"],ARTICLES_FAILURE_TAXONOMY_POLICY_ENABLED=True,ARTICLES_FAILURE_TAXONOMY_ENROLLED_ORGANIZATIONS=["7"])
class HostedPromotionTests(SimpleTestCase):
    def fixture(self):
        value=permission_fixtures.PrepareVerificationRuntimeTests();value.setUp()
        for key in ("domain","app_root","branch"):value.sealed.pop(key,None)
        value.sealed.update(schema_version=2,adapter_version=1,environment_fingerprint="f"*64)
        value.sealed["evidence_digest"]=evidence_digest({k:v for k,v in value.sealed.items() if k!="evidence_digest"})
        value.origin.payload.update(workflow="hosted_native_verification",source_run_id="setup-parent")
        value.origin.state="completed"
        value.origin.payload["binding"]={**value.sealed,"run_id":"native-run","source_run_id":"setup-parent","expected_source_sha":value.website.verified_sha}
        value.origin.refresh_from_db=Mock()
        return value

    def promote(self,value,proof=None):
        from .website_models import WebsiteConnection
        with value.boundaries(),patch.object(WebsiteConnection.objects,"get",return_value=value.website),patch.object(hosted,"remote_request",return_value={"status":"completed","run_id":"native-run","verification":proof or value.sealed}),patch("content_factory.website_operations.validate_operation",return_value=value.origin):
            return verification.record_ci_attestation({**value.sealed,"run_id":"native-run","source_run_id":"setup-parent"},origin="authenticated_hosted")

    def test_real_promotion_and_saved_receipt_work_without_customer_ci_grants(self):
        value=self.fixture();op=self.promote(value)
        self.assertEqual(op.receipt["attestation_origin"],"authenticated_hosted")
        self.assertEqual(value.target.contract["verification"]["source_sha"],value.website.verified_sha)
        self.assertTrue(value.target.capabilities["publishingReady"])
        self.assertTrue(value.target.capabilities["previewSupported"])
        self.assertFalse(any(call.kwargs.get("permission_profile")=="ci_evidence" for call in value.tokens.call_args_list))
        value.target.contract["verification"]=dict(op.receipt,status="verified")
        with value.boundaries(),patch("content_factory.website_operations.validate_operation",return_value=value.origin):
            self.assertEqual(verification.current_ci_attestation(value.website,value.target)["attestation_origin"],"authenticated_hosted")

    def test_failed_render_digest_wrong_scope_or_unknown_environment_cannot_promote(self):
        for changes in ({"browser":False},{"source_sha":"b"*40},{"environment_fingerprint":"unknown"},{"schema_version":True},{"connection_generation":True},{"evidence_digest":"e"*64}):
            value=self.fixture();bad={**value.sealed,**changes}
            if "evidence_digest" not in changes:bad["evidence_digest"]=evidence_digest({k:v for k,v in bad.items() if k!="evidence_digest"})
            with self.subTest(changes=changes),self.assertRaises(WebsiteAuthorityError):self.promote(value,bad)
            value.target.save.assert_not_called()

    def test_foreign_parent_or_cancelled_original_operation_never_fetches_service_proof(self):
        value=self.fixture()
        with patch("content_factory.website_operations.validate_operation",return_value=value.origin),patch.object(hosted,"remote_request") as fetch:
            with self.assertRaises(WebsiteAuthorityError):hosted.read_completed_proof(value.website,{**value.sealed,"run_id":"native-run","source_run_id":"foreign-parent"})
            value.origin.state="cancelled"
            with self.assertRaises(WebsiteAuthorityError):hosted.read_completed_proof(value.website,{**value.sealed,"run_id":"native-run","source_run_id":"setup-parent"})
        fetch.assert_not_called()

    def test_public_attestation_input_cannot_choose_hosted_origin(self):
        data={"attestation_origin":"authenticated_hosted","source_sha":"a"*40}
        with patch.object(verification,"record_ci_attestation",return_value=SimpleNamespace(receipt={})) as record:
            verification.WebsiteCiAttestationView().post(SimpleNamespace(data=data))
        self.assertEqual(record.call_args.kwargs,{})

    def test_hosted_permission_plan_needs_no_customer_workflow_or_ci_reads(self):
        from .tests_prepare_permissions_unit import projected_permissions
        value=permission_fixtures.PrepareVerificationRuntimeTests();value.setUp()
        from .website_journey import project_journey
        baseline=projected_permissions(integration=False,build=False,deployed=False,workflows=False,checks=False,statuses=False)
        with patch("content_factory.website_journey.mapping",wraps=goals.mapping):
            result=project_journey(company_id="9",domain="fixture.test",website={"connectionId":"site","connectionGeneration":3,"repositoryId":123,"status":"connected","repositoryAccessVerified":True},
                capabilities={"githubConnected":True,"repositoryAccessVerified":True,"repositoryWritable":True},proof={"hostedVerificationSelected":True},permissions={"workflows":False,"checks":False,"statuses":False})
        self.assertEqual(result["prerequisites"]["permissions"]["status"],"complete")
        self.assertEqual(baseline["prerequisites"]["permissions"]["status"],"needs_action")


class HostedReconciliationTests(SimpleTestCase):
    def boundaries(self,outcome,*,cancelled=False):
        from workflow_runs.models import ContentFactoryRun
        child=SimpleNamespace(state="cancelled" if cancelled else "pending",receipt={},refresh_from_db=Mock(),save=Mock())
        payload={"run_id":"native-job","source_run_id":"setup-parent"}
        changes=Mock()
        return child,changes,(
            patch.object(hosted,"request_for",return_value=(child,payload)),
            patch.object(hosted,"remote_request",return_value=outcome),
            patch.object(hosted,"authority_guard",return_value=nullcontext()),
            patch.object(ContentFactoryRun.objects,"filter",return_value=changes),
        )

    def call(self,outcome,*,cancelled=False):
        from contextlib import ExitStack
        child,changes,boundaries=self.boundaries(outcome,cancelled=cancelled)
        with ExitStack() as stack:
            for boundary in boundaries:stack.enter_context(boundary)
            record=stack.enter_context(patch.object(verification,"record_ci_attestation",return_value=SimpleNamespace(pk="proof-operation")))
            result=hosted.advance(SimpleNamespace(),SimpleNamespace(organization="organization"),None,None,"a"*40)
        return result,child,changes,record

    def test_pending_never_promotes_and_completed_binds_parent_and_origin(self):
        result,child,_,record=self.call({"status":"running","run_id":"native-job"})
        self.assertEqual(result,{"status":"pending"});self.assertEqual(child.state,"running");record.assert_not_called()
        proof={"source_sha":"a"*40}
        result,child,changes,record=self.call({"status":"completed","run_id":"native-job","verification":proof,"usage":{"model_usd":0}})
        self.assertEqual(result,{"status":"completed","operation_id":"proof-operation"})
        self.assertEqual(child.state,"completed")
        record.assert_called_once_with({**proof,"run_id":"native-job","source_run_id":"setup-parent"},origin="authenticated_hosted")
        self.assertEqual(changes.update.call_args.kwargs["result"]["usage"],{"model_usd":0})

    def test_foreign_response_and_cancellation_cannot_promote(self):
        for outcome,cancelled in (({"status":"completed","run_id":"another-job"},False),({"status":"completed","run_id":"native-job"},True),({"status":"cancelled","run_id":"native-job"},False)):
            with self.subTest(outcome=outcome,cancelled=cancelled),self.assertRaises(WebsiteAuthorityError):
                self.call(outcome,cancelled=cancelled)
