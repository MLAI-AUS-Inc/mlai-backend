"""First-article controller transitions; full provider-chain coverage is separate."""
from contextlib import nullcontext
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import MagicMock,Mock,patch
from uuid import uuid4

from django.test import SimpleTestCase,override_settings
from rest_framework.response import Response

from . import website_first_article as article,website_goal as goals,website_setup_consent as consent
from . import vibe_marketing_views as views
from .article_publish_approval import RECEIPT_KEY,make_article_publish_approval_receipt
from . import tests_website_first_article_billing_unit as billing_fixtures
from .website_contract import WebsiteAuthorityError


def rows(first=None):
    result=MagicMock()
    for method in ("filter","select_related","select_for_update","order_by","prefetch_related","exclude"):
        getattr(result,method).return_value=result
    result.first.return_value=first
    return result


@override_settings(ARTICLES_FIRST_ARTICLE_GOAL_ENABLED=True,ARTICLES_SETUP_CONSENT_ENABLED=True,
    ARTICLES_WEBSITE_GOAL_RUNNER_ENABLED=True,ARTICLES_WEBSITE_GOAL_ENROLLED_ORGANIZATIONS=["7"],
    ARTICLES_FAILURE_TAXONOMY_POLICY_ENABLED=True,ARTICLES_FAILURE_TAXONOMY_ENROLLED_ORGANIZATIONS=["7"])
class FirstArticleControllerTests(SimpleTestCase):
    def setUp(self):
        self.fixture=billing_fixtures.FirstArticleBillingTests("runTest");self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
        f=self.fixture;self.now=f.now;self.op=f.op;self.context=f.context;self.context.organization.id=7
        self.connection=f.connection;self.connection.github_repo="fixture/site";self.connection.installation_id="4"
        self.connection.verified_sha="a"*40
        self.op.connection_id=self.connection.pk;self.op.attempts=1
        self.op._claimed_at=self.now;self.op._claimed_goal_revision=0;self.op.state="running"
        self.op.receipt["goal"]["current_source_sha"]="a"*40
        held=f.reserve()
        self.config=SimpleNamespace(website_connection=self.connection,requires_review=False,pillar_strategy={})
        self.workflow=SimpleNamespace(pk=uuid4(),action="workflow",generation=3,state="running",payload={"attempt":1,"run_id":"article",
            "first_article_goal_operation_id":str(self.op.pk)})
        request={"client_request_id":held["client_request_id"],"first_article_goal_operation_id":str(self.op.pk),
            "first_article_goal_id":str(self.op.pk),"website_connection_id":str(self.connection.pk),"connection_generation":3,
            "repository_id":"12","deletion_epoch":0,"roo_points_ledger_id":"11","roo_points_billing_user_id":5,
            "operation_id":str(self.workflow.pk),"operation_attempt":1,"expected_source_sha":"a"*40,"delivery_mode":"publish_code"}
        self.run=SimpleNamespace(pk=1,run_id="article",organization_id=7,workflow="article_generation",domain="fixture.test",
            github_repo="fixture/site",status="queued",approval_state="pending",run_request=request,result={},updated_at=self.now,
            save=Mock(),refresh_from_db=Mock())
        self.records=[self.run];self.child=None
        from workflow_runs.models import ContentFactoryRun
        f.stack.enter_context(patch.object(ContentFactoryRun.objects,"filter",side_effect=self.find_run))
        from .dispatch_models import ContentFactoryDispatchOutbox
        self.dispatch_rows=f.stack.enter_context(patch.object(ContentFactoryDispatchOutbox.objects,"filter",return_value=rows()))
        from .website_models import WebsiteConnectionOperation
        f.stack.enter_context(patch.object(WebsiteConnectionOperation.objects,"select_related",return_value=rows(self.op)))
        self.connection.operations.filter.side_effect=self.find_operation
        f.rows.return_value.filter.return_value.first.return_value=self.op
        f.stack.enter_context(patch("content_factory.website_connections.authority_guard",side_effect=lambda *a,**kw:nullcontext(self.connection)))
        f.stack.enter_context(patch.object(article.transaction,"atomic",side_effect=lambda:nullcontext()))

    def find_run(self,*args,**kwargs):
        selected=self.records
        if kwargs.get("run_id"):selected=[item for item in selected if item.run_id==kwargs["run_id"]]
        if kwargs.get("run_request__client_request_id"):selected=[item for item in selected if item.run_request.get("client_request_id")==kwargs["run_request__client_request_id"]]
        return rows(selected[0] if selected else None)

    def find_operation(self,*args,**kwargs):
        return rows(self.workflow if kwargs.get("pk")==self.workflow.pk else self.op)

    def quality(self):
        self.run.status="completed"
        self.run.result={"generation":1,"livePreview":{"previewUrl":"https://preview.test/article","exactRender":True,
            "proof":{"commitSha":"b"*40},"resume_generation":0},
            "article_preview_quality":{"status":"passed","preview_url":"https://preview.test/article",
                "inputs_sha256":"c"*64,"resume_generation":0}}

    def approve(self):
        self.quality();self.run.approval_state="approved"
        self.run.run_request[RECEIPT_KEY]=make_article_publish_approval_receipt(self.run,actor_id="owner:4")
        self.op.receipt["first_article_run_id"]=self.run.run_id
        self.op.receipt["first_article_approval"]={"goal":article._immutable_binding(self.op.receipt["goal"]),
            "review":article.article_review_identity(self.run),"source_sha":"a"*40,
            "approval_digest":goals.digest(self.run.run_request[RECEIPT_KEY])}

    def publish_child(self,*,merged=False):
        self.child=SimpleNamespace(pk=2,run_id="publish",workflow="article_generation",organization_id=7,status="completed",
            domain="fixture.test",github_repo="fixture/site",run_request={**self.run.run_request,"source_run_id":"article"},
            result={"pr_number":42},save=Mock(),refresh_from_db=Mock())
        self.records.append(self.child);self.run.result["publish_child_run_id"]="publish"
        if merged:
            self.child.result.update(merge_status="merged",publish_merge_intent={"run_id":"publish","website_connection_id":str(self.connection.pk),
                "connection_generation":3,"repository_id":"12","base_branch":"main","source_sha":"a"*40,"head_sha":"d"*40},
                merge_response={"merged":True,"sha":"e"*40})

    def advance(self):
        return article.advance(self.op,self.context,self.config,user=self.fixture.user,now=self.now)

    def test_dispatch_journals_topic_before_calling_canonical_generation(self):
        self.records=[]
        selection={"topic_candidate_id":"best","topic":"Best topic","target_keyword":"best"}
        def generate(**kwargs):
            self.assertEqual(self.op.receipt["first_article_selection"]["data"],selection)
            self.assertEqual(kwargs["goal_operation_id"],self.op.pk)
            self.assertEqual(kwargs["website_contract"]["expected_source_sha"],"a"*40)
            self.assertEqual(kwargs["data"]["delivery_mode"],"publish_code")
            return Response({"run_id":"article"},status=202)
        with patch("content_factory.website_first_article_topics.current_selection",return_value=selection),patch.object(views,"start_article_generation",side_effect=generate) as dispatch:
            self.advance()
        dispatch.assert_called_once();self.assertEqual(self.op.receipt["first_article_run_id"],"article")
        self.assertEqual(self.op.state,"pending")

    def test_review_first_and_mandatory_review_dispatch_review_draft(self):
        self.records=[];goal=self.op.receipt["goal"]
        for mandatory in (False,True):
            self.config.requires_review=mandatory
            choices=deepcopy(goal["consent"]["answers"]);choices["publish_mode"]="auto" if mandatory else "review"
            goal["consent"]=consent.seal(goal,choices,user_id=4,now=self.now)
            self.op.receipt.pop("first_article_run_id",None)
            self.op.receipt.pop("first_article_billing",None)
            self.fixture.reserve()
            with patch("content_factory.website_first_article_topics.current_selection",return_value={"topic":"topic"}),patch.object(views,"start_article_generation",return_value=Response({"run_id":"article"},status=202)) as dispatch:
                self.advance()
            self.assertEqual(dispatch.call_args.kwargs["data"]["delivery_mode"],"review_draft")
            self.op.state="running"

    def test_lost_dispatch_reply_recovers_owned_run_without_new_generation(self):
        with patch.object(views,"start_article_generation") as dispatch:self.advance()
        dispatch.assert_not_called();self.assertEqual(self.op.receipt["first_article_run_id"],"article")

    def test_preview_message_without_matching_quality_cannot_request_approval(self):
        self.quality();self.run.result["article_preview_quality"]["inputs_sha256"]="bad"
        with patch.object(views,"control_article_run") as control:self.advance()
        control.assert_not_called();self.assertEqual(self.op.state,"pending")

    def test_review_choice_and_mandatory_review_wait_for_real_owner_receipt(self):
        self.quality();self.config.requires_review=True
        with patch.object(views,"control_article_run") as control:self.advance()
        control.assert_not_called();self.assertEqual(self.op.state,"needs_user")
        self.assertEqual(self.op.receipt["user_action"]["id"],"review_draft")

    def test_automatic_mode_uses_canonical_approval_then_binds_the_exact_receipt(self):
        self.quality()
        def control(**kwargs):
            if kwargs["action"]=="approve":
                self.run.approval_state="approved"
                self.run.run_request[RECEIPT_KEY]=make_article_publish_approval_receipt(self.run,actor_id="owner:4")
            return Response({},status=202)
        with patch.object(views,"control_article_run",side_effect=control) as canonical:
            self.advance()
        self.assertEqual([item.kwargs["action"] for item in canonical.call_args_list],["approve","publish-pr"])
        self.assertTrue(article.approval_matches(self.op,self.run))
        self.run.result["article_preview_quality"]["inputs_sha256"]="f"*64
        self.assertFalse(article.approval_matches(self.op,self.run))

    def test_live_proof_requires_bound_approval_owned_merge_and_current_body_hash(self):
        self.approve();self.publish_child(merged=True)
        self.op.receipt["goal"]["current_source_sha"]="e"*40
        self.op.receipt["first_article_live_receipt"]={"status":"passed","provider_verified":True,"source_sha":"e"*40,
            "run_id":"article","merge_run_id":"publish","checked_at":self.now.isoformat(),"public_url":"https://fixture.test/articles/first",
            "expected_body_sha256":"f"*64,"observed_body_sha256":"f"*64}
        self.op.receipt["first_article_live_receipt"].update(method="http_article_body",response_sha256="d"*64)
        proofs=article.proofs(self.op,self.context,self.config,source="e"*40,now=self.now)
        self.assertEqual(set(proofs),{"first_draft","first_article_live"})
        self.assertEqual(proofs["first_article_live"]["evidence_digest"],goals.digest(self.op.receipt["first_article_live_receipt"]))
        self.op.receipt["first_article_live_receipt"]["observed_body_sha256"]="a"*64
        self.assertNotIn("first_article_live",article.proofs(self.op,self.context,self.config,source="e"*40,now=self.now))

    def test_foreign_merge_or_changed_review_never_certifies_live_article(self):
        self.approve();self.publish_child(merged=True)
        self.child.result["publish_merge_intent"]["repository_id"]="foreign"
        self.assertIsNone(article.merged_source(self.op,self.run,self.context))
        self.assertEqual(article.proofs(self.op,self.context,self.config,source="e"*40,now=self.now),{})

    def test_current_approval_can_enable_goal_merge_without_legacy_auto_publish(self):
        self.approve();self.publish_child()
        self.assertTrue(views._publish_auto_merge_enabled(self.child,self.run))
        self.op.state="cancelled"
        self.child.run_request["auto_merge"]=True
        self.assertFalse(views._publish_auto_merge_enabled(self.child,self.run))

    def test_stale_source_or_cancelled_goal_cannot_start_generation(self):
        self.records=[];self.connection.generation=4
        with patch.object(views,"start_article_generation") as dispatch,self.assertRaises(WebsiteAuthorityError):self.advance()
        dispatch.assert_not_called()

    def test_lost_lease_never_persists_a_topic_or_dispatches(self):
        self.records=[];self.fixture.rows.return_value.filter.return_value.first.return_value=None
        with patch("content_factory.website_first_article_topics.current_selection",return_value={"topic":"topic"}), \
            patch.object(views,"start_article_generation") as dispatch,self.assertRaises(WebsiteAuthorityError):self.advance()
        dispatch.assert_not_called()

    def test_parent_cancellation_fences_child_before_its_queue_reply_is_attached(self):
        self.op.state="cancelled"
        with self.assertRaises(WebsiteAuthorityError):article.article_run(self.op,self.config)

    def test_live_wait_uses_existing_deployment_policy_instead_of_completing_at_merge(self):
        self.approve();self.publish_child(merged=True)
        self.op.receipt["goal"]["current_source_sha"]="e"*40
        with patch.object(views,"_maybe_auto_merge_publish_child"),patch.object(views,"_apply_publish_child_evidence_to_article"), \
            patch("content_factory.article_publish_status.refresh_publish_statuses",return_value=[]) as refresh, \
            self.assertRaises(WebsiteAuthorityError) as error:self.advance()
        self.assertEqual(error.exception.causal_code,"deployment_marker_unverified")
        self.assertEqual(refresh.call_args.kwargs["source_run_ids"],("article","publish"))
        self.assertNotIn("first_article_live_receipt",self.op.receipt)

    def test_durable_dispatch_resolution_replaces_a_provisional_run_identity(self):
        self.op.receipt["first_article_run_id"]=self.op.receipt["first_article_billing"]["client_request_id"]
        self.dispatch_rows.return_value.first.return_value=SimpleNamespace(run_id=self.run.run_id)
        with patch.object(views,"start_article_generation") as dispatch:self.advance()
        dispatch.assert_not_called();self.assertEqual(self.op.receipt["first_article_run_id"],self.run.run_id)

    def test_missing_saved_dispatch_record_waits_without_replaying_generation(self):
        self.records=[];self.op.receipt["first_article_run_id"]="missing"
        with patch.object(views,"start_article_generation") as dispatch,self.assertRaises(WebsiteAuthorityError):self.advance()
        dispatch.assert_not_called()

    def test_live_url_must_be_a_public_article_on_the_selected_site(self):
        self.assertTrue(article._public_url("https://fixture.test/articles/first","fixture.test"))
        for value in ("https://foreign.test/articles/first","https://fixture.test/","https://user@fixture.test/articles/first",
            "https://fixture.test/articles/first?secret=1","https://fixture.test/articles/first\n","https://fixture.test/articles/a)bad"):
            self.assertFalse(article._public_url(value,"fixture.test"))

    def test_unknown_control_failure_is_resolved_by_the_enrolled_registry_boundary(self):
        from .website_failure_scope import failure_scope
        with failure_scope(7),self.assertRaises(WebsiteAuthorityError) as error:
            article._check_response(Response({"code":"unregistered_customer_claim","detail":"private worker detail"},status=409))
        self.assertEqual(error.exception.failure["failure_class"],"unsupported")
        self.assertEqual(error.exception.failure["code"],"UNREGISTERED_FAILURE_CODE")
        self.assertNotIn("private worker detail",str(error.exception))

@override_settings(ARTICLES_FIRST_ARTICLE_GOAL_ENABLED=True, ARTICLES_SETUP_CONSENT_ENABLED=True,
    ARTICLES_WEBSITE_GOAL_RUNNER_ENABLED=True, ARTICLES_WEBSITE_GOAL_ENROLLED_ORGANIZATIONS=["7"],
    ARTICLES_FAILURE_TAXONOMY_POLICY_ENABLED=True, ARTICLES_FAILURE_TAXONOMY_ENROLLED_ORGANIZATIONS=["7"],
    ARTICLES_WEBSITE_GOAL_BUDGETS_ENABLED=True, ARTICLES_ARTICLE_RECOVERY_ENABLED=True,
    ARTICLES_ARTICLE_SOURCE_REFRESH_ENABLED=True)
class ArticleSourceRefreshIntentTests(SimpleTestCase):
    def setUp(self):
        from .website_goal_scope import rebind_source_scope
        self.chain = FirstArticleControllerTests("runTest")
        self.chain.setUp()
        self.addCleanup(self.chain.doCleanups)
        self.op, self.run, self.now = self.chain.op, self.chain.run, self.chain.now
        self.run.status = "completed"
        self.run.result = {"generation": 1, "state_version": 5}
        self.run.run_request.update(configuration_revision=1, branch="main", app_root="")
        self.op.receipt.update(first_article_run_id=self.run.run_id, current_step="first-article")
        self.chain.connection.configuration_version = 2
        self.chain.connection.verified_sha = "c" * 40
        self.edges = [{"from_revision": 1, "to_revision": 2, "source_sha": "c" * 40}]
        self.chain.connection.operations.filter.side_effect = self.operation_rows
        self.op.receipt["goal"] = rebind_source_scope(self.op.receipt["goal"], self.chain.connection, company_id=9, now=self.now)
        self.assertIsNotNone(self.op.receipt["goal"])
        self.op._claimed_goal_revision = self.op.receipt["goal"]["revision"]

    def operation_rows(self, *args, **kwargs):
        if kwargs.get("action") == "goal-source-scope":
            query = rows()
            query.__getitem__.side_effect = lambda key: [SimpleNamespace(payload=edge) for edge in self.edges][key]
            return query
        return self.chain.find_operation(*args, **kwargs)

    def reserve(self):
        from .website_article_refresh_intent import reserve
        return reserve(self.op, self.chain.context, self.chain.config, user=self.chain.fixture.user, now=self.now)

    def test_journal_replay_reuses_proposal_identity_and_original_spend_without_dispatch(self):
        from .website_article_refresh_intent import valid_intent
        held = deepcopy(self.op.receipt["first_article_billing"])
        self.op.receipt.update(first_article_approval={"old": "approval"}, first_article_live_receipt={"old": "live"},
            article_recovery={"old": "resume"})
        with patch.object(views, "start_article_generation") as generate, patch.object(views, "control_article_run") as control, \
            patch("content_factory.website_operations.reserve_workflow_operation") as operation:
            first = self.reserve()
            self.assertEqual(first, self.reserve())
        self.assertTrue(valid_intent(first))
        self.assertEqual(first["state"], "pending_reconciliation")
        self.assertEqual(first["binding"]["paid_root"]["run_id"], self.run.run_id)
        self.assertEqual(first["binding"]["billing"]["spend_ledger_id"], "11")
        self.assertEqual(self.op.receipt["first_article_billing"], held)
        self.assertEqual(self.chain.fixture.spend.call_count, 1)
        for key in ("first_article_approval", "first_article_live_receipt", "article_recovery"):
            self.assertNotIn(key, self.op.receipt)
        generate.assert_not_called(); control.assert_not_called(); operation.assert_not_called()
        first["binding"]["source_sha"] = "mutated returned copy"
        self.assertNotEqual(first, self.reserve())

    def test_each_new_trusted_source_gets_one_identity_with_a_fixed_goal_limit(self):
        from .website_goal_scope import rebind_source_scope
        previous = self.reserve()
        for revision in (3, 4, 5):
            self.chain.connection.configuration_version = revision
            self.chain.connection.verified_sha = str(revision) * 40
            self.edges.append({"from_revision": revision - 1, "to_revision": revision, "source_sha": str(revision) * 40})
            self.op.receipt["goal"] = rebind_source_scope(self.op.receipt["goal"], self.chain.connection, company_id=9, now=self.now)
            current = self.reserve()
            self.assertNotEqual(current["proposed_run_id"], previous["proposed_run_id"])
            self.assertEqual(current, self.reserve())
            previous = current
        self.chain.connection.configuration_version = 6
        self.chain.connection.verified_sha = "6" * 40
        self.edges.append({"from_revision": 5, "to_revision": 6, "source_sha": "6" * 40})
        self.op.receipt["goal"] = rebind_source_scope(self.op.receipt["goal"], self.chain.connection, company_id=9, now=self.now)
        with self.assertRaises(WebsiteAuthorityError): self.reserve()
        self.assertEqual(self.chain.fixture.spend.call_count, 1)

    def test_missing_trusted_edges_or_same_source_cannot_create_an_intent(self):
        self.edges = []
        with self.assertRaises(WebsiteAuthorityError): self.reserve()
        self.assertNotIn("article_source_refresh", self.op.receipt)
        self.op.receipt["goal"]["current_source_sha"] = "a" * 40
        with self.assertRaises(WebsiteAuthorityError): self.reserve()

    def test_disabled_rollout_has_no_journal_or_payment_effect(self):
        with override_settings(ARTICLES_ARTICLE_SOURCE_REFRESH_ENABLED=False):
            self.assertIsNone(self.reserve())
        self.assertNotIn("article_source_refresh", self.op.receipt)
        self.assertEqual(self.chain.fixture.spend.call_count, 1)

    def test_cancelled_root_never_becomes_refresh_authority(self):
        self.run.status = "cancelled"
        with self.assertRaises(WebsiteAuthorityError): self.reserve()
        self.assertNotIn("article_source_refresh", self.op.receipt)

    def test_cancelled_goal_and_changed_owner_do_not_acquire_intents(self):
        self.op.state = "cancelled"
        self.assertIsNone(self.reserve())
        self.op.state = "running"
        self.chain.fixture.user.pk = 999
        with self.assertRaises(WebsiteAuthorityError): self.reserve()

    def test_refunded_or_changed_original_payment_cannot_fund_a_refresh(self):
        self.chain.fixture.refunds.return_value.exists.return_value = True
        with self.assertRaises(WebsiteAuthorityError): self.reserve()
        self.chain.fixture.refunds.return_value.exists.return_value = False
        self.run.run_request["roo_points_ledger_id"] = "999"
        with self.assertRaises(WebsiteAuthorityError): self.reserve()

    def test_foreign_company_and_changed_selected_branch_are_refused(self):
        self.run.organization_id = 99
        with self.assertRaises(WebsiteAuthorityError): self.reserve()
        self.run.organization_id = 7
        self.run.run_request["branch"] = "other"
        with self.assertRaises(WebsiteAuthorityError): self.reserve()

    def test_unknown_usage_stops_before_a_new_intent(self):
        self.op.receipt["goal"]["missing_usage_run_ids"] = [self.run.run_id]
        self.assertIsNone(self.reserve())
        self.assertEqual(self.op.receipt["budget_stop_reason"], "usage_unknown")
        self.assertNotIn("article_source_refresh", self.op.receipt)

    def test_stale_prepare_lease_cannot_save_a_proposal_identity(self):
        self.chain.fixture.rows.return_value.filter.return_value.first.return_value = None
        with self.assertRaises(WebsiteAuthorityError): self.reserve()
        self.assertNotIn("article_source_refresh", self.op.receipt)

    def test_malformed_or_changed_original_intents_cannot_mint_a_replacement(self):
        original = self.reserve()
        for key, value in (("version", 2), ("state", "submitted"), ("binding", {}), ("proposed_run_id", "bad")):
            self.op.receipt["article_source_refresh"] = {**original, key: value}
            with self.subTest(key=key), self.assertRaises(WebsiteAuthorityError): self.reserve()
        self.op.receipt["article_source_refresh"] = original
        self.run.result["state_version"] += 1
        self.assertEqual(self.reserve()["proposed_run_id"], original["proposed_run_id"])
        self.run.result["generation"] += 1
        with self.assertRaises(WebsiteAuthorityError): self.reserve()

    def test_missing_or_boolean_original_revisions_are_not_refresh_evidence(self):
        request, result = deepcopy(self.run.run_request), deepcopy(self.run.result)
        for field, value in (("configuration_revision", None), ("configuration_revision", True),
                ("configuration_revision", 2), ("app_root", "other")):
            self.run.run_request = {**request, field: value}
            with self.subTest(field=field, value=value), self.assertRaises(WebsiteAuthorityError): self.reserve()
        self.run.run_request = request
        for field, value in (("generation", -1), ("generation", True), ("state_version", None)):
            self.run.result = {**result, field: value}
            with self.subTest(field=field, value=value), self.assertRaises(WebsiteAuthorityError): self.reserve()

    def test_duplicate_or_foreign_historical_intents_are_refused(self):
        from .website_article_refresh_intent import valid_intent
        current = self.reserve()
        self.op.receipt["article_source_refresh_history"] = [deepcopy(current)]
        with self.assertRaises(WebsiteAuthorityError): self.reserve()
        foreign = deepcopy(current)
        foreign["binding"]["goal"]["goal_id"] = "foreign"
        foreign["binding"]["billing"]["client_request_id"] = "articles-first:" + goals.digest({
            "goal_id": "foreign", "scope": {key: value for key, value in foreign["binding"]["current_scope"].items()
                if key != "configuration_revision"}})
        foreign["id"] = goals.digest(foreign["binding"])
        self.assertTrue(valid_intent(foreign), "A syntactically valid foreign journal is still unauthorized")
        self.op.receipt["article_source_refresh_history"] = [foreign]
        with self.assertRaises(WebsiteAuthorityError): self.reserve()
