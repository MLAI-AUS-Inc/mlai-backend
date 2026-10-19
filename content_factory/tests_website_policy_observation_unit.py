"""Read-only policy projection validates actual original settlement decisions."""
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings
from django.urls import reverse
from rest_framework.test import APIRequestFactory

from . import website_policy_observation as policy, website_runtime_observation as runtime
from . import website_goal as goals, website_failure_contract as failures
from . import tests_website_first_article_settlement_unit as settlement_fixtures
from . import website_first_article_billing as billing
from .tests_website_runtime_observation_unit import query
from .tests_website_zoo_scope_unit import rows


@override_settings(ARTICLES_FIRST_ARTICLE_GOAL_ENABLED=True, ARTICLES_SETUP_CONSENT_ENABLED=True,
    ARTICLES_WEBSITE_GOAL_RUNNER_ENABLED=True, ARTICLES_WEBSITE_GOAL_ENROLLED_ORGANIZATIONS=["7"],
    ARTICLES_FAILURE_TAXONOMY_POLICY_ENABLED=True, ARTICLES_FAILURE_TAXONOMY_ENROLLED_ORGANIZATIONS=["7"])
class PolicyProjectionTests(SimpleTestCase):
    def setUp(self):
        self.fixture=settlement_fixtures.ParentSettlementTests("runTest")
        self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
        self.op=self.fixture.op;self.op.connection_id=self.op.connection.pk
        self.op.receipt["goal"]["current_source_sha"]="a"*40
        self.refund=SimpleNamespace(pk=22,user_id=5,kind="REFUND",source="CONTENT_FACTORY",
            reference_type="CONTENT_FACTORY",reference_id=self.fixture.held["client_request_id"],
            idempotency_key="content_factory:refund:"+self.fixture.held["client_request_id"],
            delta_microroo=6_000_000,refund_of_id=self.fixture.ledger.pk)
        self.settlement=SimpleNamespace(action="goal-refund",connection_id=self.op.connection_id,
            generation=self.op.generation,payload={"goal_operation_id":str(self.op.pk),"goal_id":str(self.op.pk)},
            state="completed",receipt={"goal_operation_id":str(self.op.pk)})

    def project(self):
        return policy.billing_observation(self.op,spend=self.fixture.ledger,
            refund=self.refund,settlement_operation=self.settlement)

    def test_actual_release_then_exact_ledger_and_outbox_prove_one_original_credit(self):
        billing.release(self.op,reason="fixture cancelled")
        self.assertEqual(self.op.receipt["first_article_billing"]["status"],"refunded")
        value=self.project()
        self.assertTrue(value["credit_proven"],value)
        self.assertEqual(value["coverage"],"complete")
        self.assertEqual(value["dispatch"],"not_dispatched")
        self.assertEqual(value["cost_points"],6)
        text=json.dumps(value)
        self.assertNotIn(self.fixture.held["client_request_id"],text)
        self.assertNotIn('"payer_user_id"',text)

    def test_changed_payer_amount_spend_or_refund_relation_cannot_prove_credit(self):
        billing.release(self.op,reason="fixture cancelled")
        saved=deepcopy(self.refund)
        for key,value in (("user_id",99),("delta_microroo",1),("refund_of_id",99),("reference_id","other")):
            with self.subTest(key=key):
                self.refund=deepcopy(saved);setattr(self.refund,key,value)
                self.assertEqual(self.project()["coverage"],"partial")
        self.refund=saved
        self.settlement.payload["goal_id"]="another-goal"
        self.assertFalse(self.project()["credit_proven"])

    def test_refund_receipt_alone_does_not_hide_missing_ledger_or_unknown_child(self):
        billing.release(self.op,reason="fixture cancelled")
        self.refund=None
        self.assertEqual(self.project()["coverage"],"partial")

    def refusal(self):
        request={"website_connection_id":str(self.op.connection_id),"connection_generation":self.op.generation,
            "repository_id":self.op.connection.repository_id,"deletion_epoch":0,"expected_source_sha":"a"*40,
            "first_article_goal_id":str(self.op.pk),"operation_id":"child-operation","operation_attempt":1}
        failure=failures.classify_failure("MISSING_CONFIG", "setup")
        preflight={"schema_version":1,"status":"needs_input","required_names":["NPM_TOKEN"],
            "registry_auth_names":["NPM_TOKEN"],"missing_names":["NPM_TOKEN"]}
        build={"stage":"input_preflight","missing_requirements":["NPM_TOKEN"],"build_input_preflight":preflight,
            "private_output":"github_pat_private-canary"}
        child=SimpleNamespace(run_id="setup-original",workflow="article_system_setup",organization_id=7,
            github_repo=self.op.connection.github_repo,domain=self.op.connection.organization.domain,
            run_request=request,status="blocked",result={"failure":failure,"article_system_setup":{
                "diagnostics":{"directory_build_report":build}}})
        operation=SimpleNamespace(pk="child-operation",action="workflow",connection_id=self.op.connection_id,
            generation=self.op.generation,payload={"run_id":child.run_id,"attempt":1})
        return child,operation

    def test_names_only_nested_real_preflight_shape_preserves_typed_owner_stop(self):
        child,operation=self.refusal()
        value=policy.child_observation(self.op,child,child_operation=operation)
        self.assertEqual(value["coverage"],"complete",value)
        self.assertEqual(value["failure"]["code"],"MISSING_CONFIG")
        self.assertEqual(value["preflight"]["registry_auth_names"],["NPM_TOKEN"])
        self.assertNotIn("github_pat_private-canary",json.dumps(value))

    def test_actual_cf_declared_registry_preflight_producer_matches_owned_reader(self):
        source=json.loads((Path(__file__).parent/"testdata/runtime-observation/cf-required-input-v1.json").read_text())["result"]
        child,operation=self.refusal()
        child.result={"failure":source["failure"],"article_system_setup":{"directory_build_report":source}}
        value=policy.child_observation(self.op,child,child_operation=operation)
        self.assertEqual(value["coverage"],"complete",value)
        self.assertEqual(value["preflight"]["missing_names"],["NPM_TOKEN"])
        self.assertEqual(value["failure"]["failure_fingerprint"],source["failure"]["failure_fingerprint"])

    def test_old_source_conflicting_goal_or_unreserved_child_cannot_supply_policy_proof(self):
        child,operation=self.refusal()
        for key,value in (("expected_source_sha","b"*40),("first_article_goal_id","other"),("operation_attempt",2)):
            saved=deepcopy(child.run_request);child.run_request[key]=value
            self.assertEqual(policy.child_observation(self.op,child,child_operation=operation)["coverage"],"partial")
            child.run_request=saved
        self.assertEqual(policy.child_observation(self.op,child,child_operation=None)["coverage"],"partial")

    def test_actual_disconnected_original_resolver_reads_credit_without_restoring_authority(self):
        from founder_tools.models import VibeRaisingCompany
        from .models import OrganizationContentConfig
        from .website_models import WebsiteConnectionOperation
        from roo.models import Ledger
        billing.release(self.op,reason="fixture cancelled")
        self.op.attempts=2;self.op.next_attempt_at=None;self.op.state="cancelled"
        self.op.connection.generation+=1;self.op.connection.configuration_version+=1
        self.op.connection.state="disconnected"
        company=SimpleNamespace(pk=9,organization_id=7)
        row={"original_goal_id":str(self.op.pk),"started_binding":{"companyId":"9",
            "connectionId":str(self.op.connection_id),"connectionGeneration":2}}
        def operations(**kwargs):return rows(self.op if kwargs["action"]=="prepare" else self.settlement)
        def ledger(**kwargs):return rows(self.fixture.ledger if "pk" in kwargs else self.refund)
        with patch.object(VibeRaisingCompany.objects,"only",return_value=rows(company)), \
                patch.object(OrganizationContentConfig.objects,"select_related",return_value=rows(SimpleNamespace(website_connection=self.op.connection))), \
                patch.object(WebsiteConnectionOperation.objects,"filter",side_effect=operations), \
                patch.object(Ledger.objects,"filter",side_effect=ledger):
            value=policy.resolve(row,"first_article_live")
            self.assertTrue(value["policy_satisfied"],value)
            self.assertEqual(value["original_generation"],2)
            self.assertEqual(value["current_generation"],3)
            self.assertEqual(value["policy"],"disconnect_refund")
            self.op.connection.generation+=1
            with self.assertRaises(runtime.ObservationUnavailable):policy.resolve(row,"first_article_live")


@override_settings(INTERNAL_API_KEY="synthetic-internal-observer-key")
class PrivateObservationRouteTests(SimpleTestCase):
    def setUp(self):self.factory=APIRequestFactory()

    def test_profile_is_private_read_only_and_never_accepts_observed_facts_from_caller(self):
        from . import website_github_action_archive as archive
        url=reverse("articles_provider_observation_profile")
        view=runtime.WebsiteProviderObservationProfileView.as_view()
        with patch.object(archive,"read_profile",return_value={"version":1,"coverage":"complete"}) as read:
            self.assertEqual(view(self.factory.get(url)).status_code,403)
            self.assertEqual(view(self.factory.get(url,{"observed":True},HTTP_X_API_KEY="synthetic-internal-observer-key")).status_code,400)
            read.assert_not_called()
            response=view(self.factory.get(url,HTTP_X_API_KEY="synthetic-internal-observer-key"))
            self.assertEqual(response.status_code,200)
            self.assertEqual(response["Cache-Control"],"no-store")
            read.assert_called_once_with()

    def test_policy_route_requires_private_key_and_exact_original_selection_before_read(self):
        url=reverse("articles_policy_observation");view=policy.WebsitePolicyObservationView.as_view()
        with patch.object(policy,"read_policy",return_value={"version":1,"coverage":"partial"}) as read:
            self.assertEqual(view(self.factory.get(url,data=query())).status_code,403)
            self.assertEqual(view(self.factory.get(url,data={"goal_id":"latest"},HTTP_X_API_KEY="synthetic-internal-observer-key")).status_code,409)
            read.assert_not_called()
            response=view(self.factory.get(url,data=query(),HTTP_X_API_KEY="synthetic-internal-observer-key"))
            self.assertEqual(response.status_code,200)
            self.assertEqual(response["Cache-Control"],"no-store")
            self.assertEqual(read.call_count,2)

    def test_policy_observation_changed_or_private_storage_error_stays_a_static_diagnostic(self):
        url=reverse("articles_policy_observation");view=policy.WebsitePolicyObservationView.as_view()
        request=lambda:self.factory.get(url,data=query(),HTTP_X_API_KEY="synthetic-internal-observer-key")
        with patch.object(policy,"read_policy",side_effect=[{"scope_digest":"a"*64},{"scope_digest":"b"*64}]):
            response=view(request())
        self.assertEqual(response.status_code,409)
        self.assertEqual(response.data,{"code":"original_observation_changed"})
        with patch.object(policy,"read_policy",side_effect=ValueError("github_pat_private-storage-canary")):
            response=view(request())
        self.assertEqual(response.status_code,503)
        self.assertEqual(response.data,{"code":"original_policy_observation_unavailable"})
