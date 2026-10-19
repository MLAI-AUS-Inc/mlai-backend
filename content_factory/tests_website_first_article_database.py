"""Synthetic first-article provider-chain records in the existing PostgreSQL CI lane.

Do not run this locally without the AGENTS.md migration approval. No migration
files are added; the existing test database schema is used unchanged.
"""
from contextlib import ExitStack
from copy import deepcopy
import socket
from types import SimpleNamespace
from unittest.mock import Mock,patch

from django.db import connection
from django.test import TransactionTestCase,override_settings
from django.utils import timezone

from roo.models import Ledger
from workflow_runs.models import ContentFactoryRun

from . import website_first_article as article,website_goal as goals,website_prepare as prepare
from . import vibe_marketing_views as views
from .activation import article_capabilities
from .dispatch_models import ContentFactoryDispatchOutbox
from .tests_one_click_database import OneClickBillingFixture
from .tests_website_setup_consent_unit import answers
from .website_contract import WebsiteAuthorityError,evidence_digest
from .website_first_article_refunds import advance_refund
from .website_models import WebsiteConnectionOperation,WebsiteConnectionTarget,WebsiteRepositoryMutation,WebsiteScanSnapshot


def provider_response(payload=None, *, status=200, body=b"{}"):
    """Only the external HTTP transport is simulated; validators remain real."""
    response=Mock(status_code=status,content=body,headers={})
    response.json.return_value=deepcopy(payload)
    response.iter_content.side_effect=lambda *_args,**_kwargs:iter([body])
    return response


class FirstArticleProviderChainTests(OneClickBillingFixture,TransactionTestCase):
    def setUp(self):
        super().setUp()
        rollout=override_settings(ARTICLES_FIRST_ARTICLE_GOAL_ENABLED=True,ARTICLES_SETUP_CONSENT_ENABLED=True,
            ARTICLES_WEBSITE_GOAL_RUNNER_ENABLED=True,ARTICLES_WEBSITE_GOAL_ENROLLED_ORGANIZATIONS=[str(self.org.pk)],
            ARTICLES_FAILURE_TAXONOMY_POLICY_ENABLED=True,ARTICLES_FAILURE_TAXONOMY_ENROLLED_ORGANIZATIONS=[str(self.org.pk)],
            ARTICLES_GOAL_NOTIFICATIONS_ENABLED=False,ARTICLES_WEBSITE_EVENT_WAKES_ENABLED=False,
            CONTENT_FACTORY_URL="https://synthetic-worker.test",CONTENT_FACTORY_API_KEY="synthetic")
        rollout.enable();self.addCleanup(rollout.disable)
        self.website.authorized_by=self.requester;self.website.verified_sha="a"*40
        self.website.save(update_fields=["authorized_by","verified_sha"])
        self.config.refresh_from_db()
        self.config.auto_publish=False;self.config.requires_review=False
        self.config.save(update_fields=["auto_publish","requires_review"])
        from founder_tools.services import get_founder_company_context
        self.context=get_founder_company_context(self.requester,company_id=self.company.pk)

    def start(self,choices=None):
        return prepare.start_prepare(self.config,company_id=self.company.pk,user=self.requester,
            target="first_article_live",consent=choices or answers(billing={"policy":"reserved","expected_points":12,"accepted":True}))

    def ready(self,op):
        """Seed the already verified Ready precondition; exercise article effects below."""
        now=timezone.now();goal=op.receipt["goal"];scope=goal["scope"]
        proofs={name:goals.make_proof(name,sorted(goals.PROOF_KINDS[name])[0],scope=scope,
            source_sha="a"*40,sha="a"*40,run_id=None,evidence={"ready_fixture":name},at=now) for name in goals.MILESTONES[:7]}
        op.receipt={**op.receipt,"goal":goals.refresh_goal(goal,proofs,scope=scope,source_sha="a"*40,now=now)}
        op.save(update_fields=["receipt","updated_at"])
        self.assertEqual(goals.next_milestone(op.receipt["goal"]),"first_draft")

    def capabilities(self,*args,**kwargs):
        # Provider observation data is synthetic; the real shared readiness gate
        # and canonical generation/admission policy calculate the capability.
        return article_capabilities(self.config,domain=self.org.domain,
            account={"owned":True,"saved":True},
            evidence={"verified":True,"publishingVerified":True,"branch":"main","sha":self.website.verified_sha},
            repository_access={"verified":True,"writable":True,"branch":"main","sha":self.website.verified_sha})

    def advance_article(self,op,*,record_failure=False):
        op.refresh_from_db()
        op.next_attempt_at=timezone.now();op.save(update_fields=["next_attempt_at"])
        claimed=prepare._claim_prepare(op.pk,timezone.now())
        self.assertIsNotNone(claimed)
        try:
            article.advance(claimed,self.context,self.config,user=self.requester,now=timezone.now())
        except WebsiteAuthorityError as exc:
            if not record_failure:raise
            from .website_goal_runner import account_child_usage
            claimed.receipt["goal"]=account_child_usage(claimed,claimed.receipt["goal"])
            prepare.prepare_failure(claimed,"first-article",exc,now=timezone.now())
        self.assertIsNotNone(prepare._finish_prepare(claimed,now=timezone.now()))
        op.refresh_from_db()

    def native_target(self):
        contract={"target_id":"native","contract_digest":"c"*64,"route_path":"/articles",
            "route_template":"/articles/{slug}","live_marker":{"kind":"artifact_digest","schema_version":1,
                "meta_name":"mlai-artifact-digest","value":"f"*64}}
        WebsiteConnectionTarget.objects.update_or_create(connection=self.website,generation=self.website.generation,
            target_key="native",defaults={"adapter":"react_article_system","source_sha":self.website.verified_sha,
                "verified_at":timezone.now(),"contract":contract,"capabilities":{"publishingReady":True}})
        self.config.default_publish_target_id="native";self.config.publish_targets=[contract]
        self.config.save(update_fields=["default_publish_target_id","publish_targets"])
        # The test begins at Ready, including the publishing guard's owning
        # deployment record. Seeding only goal milestones would leave the
        # existing integration unverified and correctly deny article approval.
        WebsiteConnectionOperation.objects.create(connection=self.website,generation=self.website.generation,
            action="deployment-verify",state="completed",idempotency_key="synthetic-ready-deployment",
            payload={"source_sha":self.website.verified_sha,"target_id":"native"},
            receipt={"status":"passed","provider_verified":True,"source_sha":self.website.verified_sha,
                "connection_generation":self.website.generation,"target_id":"native","contract_digest":"c"*64,
                "artifact_digest":"f"*64,"public_url":f"https://{self.org.domain}/articles","checked_at":timezone.now().isoformat()})

    def test_ready_goal_uses_canonical_approval_merge_reverification_and_exact_live_body(self):
        self.ready_article_chain()

    @override_settings(ARTICLES_ARTICLE_RECOVERY_ENABLED=True,ARTICLES_WEBSITE_GOAL_BUDGETS_ENABLED=True,
        CONTENT_FACTORY_ARTICLE_DELIVERY_V1_APPLY_ENABLED=True)
    def test_failed_article_recovers_original_paid_run_then_finishes_the_live_chain(self):
        self.ready_article_chain(repair=True)

    @override_settings(ARTICLES_ARTICLE_RECOVERY_ENABLED=True,ARTICLES_WEBSITE_GOAL_BUDGETS_ENABLED=True,
        CONTENT_FACTORY_ARTICLE_DELIVERY_V1_APPLY_ENABLED=True)
    def test_unresumable_worker_does_not_claim_an_article_repair_was_submitted(self):
        self.ready_article_chain(repair=True,resume_noop=True)

    def ready_article_chain(self,*,repair=False,resume_noop=False):
        """Ready is the precondition; everything through live uses owning records."""
        from django.core.cache import cache
        from .article_publish_approval import article_publish_approval_receipt_matches
        from .models import ArticlePublishStatus,WrittenArticle
        from .website_goal_evidence import collect_proofs
        from .website_verification import BOOLEAN_PROOFS,record_ci_attestation,validated_ci_identity,verify_live_deployment

        def usage(identifier,generation=0,state_version=2):
            from .website_goal_budget import COUNTERS
            return {"version":1,"run_id":identifier,"generation":generation,"state_version":state_version,
                **{key:0 for key in COUNTERS}}

        cache.delete(f"vibe-marketing:auto-merge:first-publish")
        cache.delete(f"article-publish-status:sitemap:{self.org.domain}")
        self.native_target()
        op=self.start();self.ready(op)
        public_url=f"https://{self.org.domain}/articles/first"
        expected_html=("<article><h1>Evidence of tested work</h1><p>"
            "A reliable article workflow carries the original customer choices through every stage. "
            "Its recorded evidence proves the draft was reviewed, the correct repository commit was "
            "merged, and the same complete content was served from the expected public address."
            "</p></article>")
        remote={"merged":False,"merge_calls":0,"posts":[],"content_deployed":False}
        repo={"id":self.website.repository_id,"full_name":self.website.github_repo}

        def pull():
            return {"number":42,"state":"closed" if remote["merged"] else "open","merged":remote["merged"],
                "html_url":"https://github.com/example/site/pull/42",
                "head":{"sha":"d"*40,"ref":"cf/first-article","repo":repo},
                "base":{"ref":"main","repo":repo},"merge_commit_sha":"e"*40 if remote["merged"] else None,
                "merged_at":timezone.now().isoformat() if remote["merged"] else None}

        def worker_post(url,**kwargs):
            self.assertFalse(connection.in_atomic_block)
            remote["posts"].append(url)
            if url.endswith("/api/runs/article"):
                self.assertEqual(kwargs["json"]["topic_candidate_id"],"best")
                self.assertEqual(kwargs["json"]["delivery_mode"],"publish_code")
                if repair:
                    self.assertEqual(kwargs["json"]["article_delivery_v1"],{"schema_version":1})
                    self.assertEqual(kwargs["json"]["first_article_goal_id"],str(op.pk))
                return provider_response({"run_id":"first-article","status":"queued"},status=202)
            if url.endswith("/first-article/approve"):
                return provider_response({"run_id":"first-article","status":"completed","approval_state":"approved"})
            if url.endswith("/first-article/resume"):
                self.assertEqual(kwargs["json"]["operation_attempt"],2)
                self.assertEqual(Ledger.objects.filter(kind="SPEND",source="CONTENT_FACTORY").count(),1)
                return provider_response({"run_id":"first-article","status":"noop","resume_available":False} if resume_noop
                    else {"run_id":"first-article","status":"queued"},status=200 if resume_noop else 202)
            if url.endswith("/first-article/publish-pr"):
                source=ContentFactoryRun.objects.get(run_id="first-article")
                WebsiteRepositoryMutation.objects.create(connection=self.website,generation=self.website.generation,
                    operation_id="synthetic-first-publication",run_id="first-publish",base_sha="a"*40,head_sha="d"*40,
                    branch="cf/first-article",patch_digest="d"*64,status="applied",files=[])
                return provider_response({"run_id":"first-publish","status":"completed","source_run_id":source.run_id,
                    "result":{"pr_number":42,"pr_url":"https://github.com/example/site/pull/42","usage":usage("first-publish"),
                        "branch_commit_sha":"d"*40,"branch":"cf/first-article","slug":"first"},
                    "slug":"first","route_path":"/articles/first"},status=202)
            self.fail(f"Unexpected worker action: {url}")

        def github_request(method,url,**kwargs):
            self.assertFalse(connection.in_atomic_block)
            if method=="GET" and url.endswith("/pulls/42"):return provider_response(pull())
            if method=="GET" and url.endswith("/status"):return provider_response({"state":"success","total_count":0})
            if method=="GET" and url.endswith("/check-runs"):return provider_response({"check_runs":[]})
            if method=="PUT" and url.endswith("/pulls/42/merge"):
                self.assertEqual(kwargs["json"]["sha"],"d"*40)
                remote["merge_calls"]+=1;remote["merged"]=True
                return provider_response({"merged":True,"sha":"e"*40})
            self.fail(f"Unexpected GitHub write/read: {method} {url}")

        def provider_get(url,**kwargs):
            self.assertFalse(connection.in_atomic_block)
            if "/rules/branches/" in url:return provider_response([])
            if url.endswith("/protection"):return provider_response({},status=403)
            if "/compare/" in url:
                self.assertTrue(url.endswith("d"*40+"..."+"e"*40))
                return provider_response({"status":"ahead"})
            if url.endswith("/pulls/42"):return provider_response(pull())
            if url.endswith("/sitemap.xml"):
                return provider_response(body=f"<urlset><url><loc>{public_url}</loc></url></urlset>".encode())
            if url==public_url:
                article_html=expected_html if remote["content_deployed"] else expected_html.replace("reliable","previous")
                live=f'<html><head><link rel="canonical" href="{public_url}"></head><body>{article_html}</body></html>'
                return provider_response(body=live.encode())
            self.fail(f"Unexpected public/provider read: {url}")

        with ExitStack() as stack:
            stack.enter_context(patch.object(views,"_topic_selection_candidate_pool",return_value=[{
                "id":"best","title":"Evidence of tested work","keyword":"tested work","opportunityScore":700}]))
            stack.enter_context(patch.object(views,"_article_capabilities_for_context",side_effect=self.capabilities))
            stack.enter_context(patch.object(views,"analytics_config_for_content_factory",return_value={}))
            stack.enter_context(patch.object(views,"_github_app_token_payload_for_domain",return_value={"github_token":"synthetic"}))
            stack.enter_context(patch("integrations.services.github_app.create_installation_access_token",return_value=SimpleNamespace(token="synthetic")))
            stack.enter_context(patch("content_factory.article_publish_status._github_read_token",return_value="synthetic"))
            stack.enter_context(patch.object(views.http_client,"post",side_effect=worker_post))
            stack.enter_context(patch.object(views.http_client,"request",side_effect=github_request))
            stack.enter_context(patch.object(views.http_client,"get",side_effect=provider_get))
            stack.enter_context(patch.object(views.http_client,"delete",return_value=provider_response()))
            stack.enter_context(patch("content_factory.article_live_evidence.socket.getaddrinfo",return_value=[
                (socket.AF_INET,socket.SOCK_STREAM,6,"",("93.184.216.34",443))]))
            self.advance_article(op)
            run=ContentFactoryRun.objects.get(run_id="first-article")
            if repair:
                from uuid import uuid4
                from .website_failure_contract import attach_failure_projection,classify_failure
                from .setup_delivery_enrollment import SCOPE_FIELDS
                from .setup_delivery_receiver import receive_setup_delivery
                from .tests_setup_delivery_unit import envelope,signed
                worker=envelope(kind="run_snapshot",status="failed",event_id="first-article-failure")
                worker.update(run_id=run.run_id,workflow="direct_generate",scope={
                    key:run.run_request[key] for key in SCOPE_FIELDS if run.run_request.get(key) not in (None,"")},
                    execution={"generation":0,"state_version":2})
                projection=worker["body"]["state_projection"]
                causal=attach_failure_projection({},classify_failure("DIRECTORY_STYLE_REVIEW_FAILED","article_style"),str(uuid4()))
                projection.update(generation=0,state_version=2,current_step="article_style",
                    failure=causal["failure"],failure_occurrence_id=causal["failure_occurrence_id"],
                    result={"usage":usage(run.run_id),**causal})
                signed(worker)
                accepted=receive_setup_delivery(worker,expected_kind="run_snapshot",path_run_id=run.run_id)
                self.assertEqual(accepted.status_code,200,accepted.data)
                self.assertEqual(accepted.data["disposition"],"applied")
                self.assertEqual(receive_setup_delivery(worker).data["disposition"],"duplicate")
                run.refresh_from_db();self.assertEqual(run.result["failure"],causal["failure"])
                held=deepcopy(op.receipt["first_article_billing"])
                self.advance_article(op,record_failure=True)
                self.assertEqual(op.receipt["article_recovery"]["state"],"pending")
                if resume_noop:
                    self.advance_article(op,record_failure=True)
                    self.assertNotEqual(op.receipt["article_recovery"]["state"],"submitted")
                    self.assertNotEqual(op.state,"completed")
                    self.assertFalse(goals.complete(op.receipt["goal"]))
                    self.assertEqual(op.receipt["first_article_billing"],held)
                    self.assertEqual(Ledger.objects.filter(kind="SPEND",source="CONTENT_FACTORY").count(),1)
                    self.assertNotIn("first_article_approval",op.receipt)
                    return
                self.advance_article(op)  # actual canonical resume and reserved attempt
                run.refresh_from_db()
                self.assertEqual(run.run_request["operation_attempt"],2)
                self.assertEqual(run.result.get("user_action_count",0),0)
                self.assertEqual(op.receipt["article_recovery"]["state"],"submitted")
                self.assertEqual(op.receipt["first_article_billing"],held)
            # This is the simulated Content Factory checkpoint. Approval and
            # merge receipts below must be written by their canonical handlers.
            run.status="completed"
            run.result={"generation":1 if repair else 0,"state_version":5,"usage":usage(run.run_id,1 if repair else 0,5),
                "livePreview":{"previewUrl":"https://preview.test/article","exactRender":True,
                "proof":{"commitSha":"b"*40},"resume_generation":0},
                "article_preview_quality":{"status":"passed","preview_url":"https://preview.test/article",
                    "inputs_sha256":"c"*64,"resume_generation":0},"slug":"first",
                "content_package":{"slug":"first","article_html":expected_html}}
            run.save(update_fields=["status","result","updated_at"])
            written=WrittenArticle.objects.create(organization=self.org,title="Evidence of tested work",slug="first",
                category="Testing",primary_keyword="tested work",source_run_id=run.run_id)
            self.advance_article(op)  # canonical approval, then canonical PR creation
            run.refresh_from_db()
            self.assertTrue(article_publish_approval_receipt_matches(run))
            self.assertTrue(article.approval_matches(op,run))
            self.advance_article(op)  # real merge policy and protected merge intent
            child=ContentFactoryRun.objects.get(run_id="first-publish")
            if repair:self.assertEqual(child.run_request["article_delivery_v1"],{"schema_version":1})
            self.assertEqual(child.result["merge_response"],{"merged":True,"sha":"e"*40})
            self.assertEqual(remote["merge_calls"],1)
            self.assertNotIn("first_article_live_receipt",op.receipt)
            self.assertEqual(op.receipt["goal"]["current_source_sha"],"a"*40)
            self.assertFalse(goals.complete(op.receipt["goal"]))

            body={**{key:run.run_request[key] for key in ("website_connection_id","connection_generation",
                "repository_id","operation_id","operation_attempt","deletion_epoch")},"schema_version":2,
                "github_repo":self.website.github_repo,"source_sha":"e"*40,"target_id":"native",
                "adapter_id":"react_article_system","adapter_version":1,"contract_digest":"c"*64,
                "source_tree_sha":"e"*40,"environment_fingerprint":"f"*64,"lockfile_digests":{},"artifact_digest":"f"*64,
                "baseline_build":"passed","patched_build":"passed",**{key:True for key in BOOLEAN_PROOFS},
                "verified_routes":{"listing":"/articles","detail":"/articles/first","unknown_slug":"/articles/not-a-real-article"}}
            proof={**body,"evidence_digest":evidence_digest(body)}
            def ci_provider(website,requested):
                self.assertFalse(connection.in_atomic_block)
                return validated_ci_identity(requested,proof)
            with override_settings(ARTICLES_WEBSITE_EVENT_WAKES_ENABLED=True,ARTICLES_DEPLOYMENT_DETECTION_ENABLED=False), \
                patch("content_factory.website_wakeups.submit_immediate"), \
                patch("content_factory.website_verification.read_ci_proof",side_effect=ci_provider), \
                patch("content_factory.website_live_fetch.fetch_live_route",return_value=(b"",{"x-mlai-artifact-digest":"f"*64})):
                record_ci_attestation({**proof,"run_id":child.run_id})
                self.website.refresh_from_db();self.config.refresh_from_db();op.refresh_from_db()
                self.assertEqual(op.receipt["goal"]["current_source_sha"],"e"*40)
                self.assertEqual(op.receipt["first_article_billing"]["spend_ledger_id"],str(Ledger.objects.get(kind="SPEND").pk))
                verify_live_deployment(self.config,data=proof)
            WebsiteScanSnapshot.objects.create(connection=self.website,generation=self.website.generation,
                run_id="post-merge-scan",source_sha="e"*40,fingerprint="f"*64,detector_version="synthetic_worker",
                evidence={"repository_inventory":{"discovery_complete":True}})
            journey={"connectionId":str(self.website.pk),"connectionGeneration":self.website.generation,
                "prerequisites":{"account":{"status":"complete"},"repository":{"status":"complete"}},
                "repository":{"observedSourceSha":"e"*40},"capabilities":self.capabilities(),
                "githubPermissions":{"contents":True,"pull_requests":True,"workflows":True,"checks":True,"statuses":True}}
            op.next_attempt_at=timezone.now();op.save(update_fields=["next_attempt_at"])
            with patch.object(prepare,"journey_for_context",return_value=journey):
                waiting=prepare.advance_prepare(op.pk,now=timezone.now())
                self.assertIsNotNone(waiting);self.assertEqual(waiting.state,"pending")
            op.refresh_from_db()
            self.assertNotIn("first_article_live_receipt",op.receipt)
            self.assertEqual(op.receipt["status"],"waiting_for_deployment")
            self.assertFalse(goals.complete(op.receipt["goal"]))
            remote["content_deployed"]=True
            self.advance_article(op)  # actual HTTP capture and exact saved-body comparison
            self.assertEqual(op.receipt["first_article_live_receipt"]["public_url"],public_url)
            written.refresh_from_db();self.assertEqual(written.publish_status,ArticlePublishStatus.LIVE)
            proofs,source=collect_proofs(op,self.context,self.config,journey,now=timezone.now())
            self.assertEqual(set(proofs),set(goals.MILESTONES))
            self.assertEqual(source,"e"*40)
            # Finish through the actual prepare runner and canonical proof
            # planner; the substituted journey is a provider-read projection.
            op.next_attempt_at=timezone.now();op.save(update_fields=["next_attempt_at"])
            with patch.object(prepare,"journey_for_context",return_value=journey), \
                override_settings(ARTICLES_GOAL_NOTIFICATIONS_ENABLED=True,COMMUNITY_CHAT_FRONTEND_URL="https://chat.mlai.au"):
                finished=prepare.advance_prepare(op.pk,now=timezone.now())
                self.assertIsNotNone(finished)
                self.assertEqual(finished.state,"completed")
            notice=WebsiteConnectionOperation.objects.get(action="goal-notification")
            self.assertEqual(notice.payload["notice"]["link"],public_url)
            self.assertEqual(notice.payload["notice"]["text"],"Your first article is live.")
            self.assertEqual(notice.state,"pending")  # delivery is deliberately outside this provider-chain test
        op.refresh_from_db()
        self.assertTrue(goals.complete(op.receipt["goal"]))
        self.assertEqual(op.receipt["goal"]["completed_source_sha"],"e"*40)
        self.assertEqual(Ledger.objects.filter(kind="SPEND",source="CONTENT_FACTORY").count(),1)
        self.assertFalse(Ledger.objects.filter(kind="REFUND",source="CONTENT_FACTORY").exists())
        self.assertEqual(self.balance(self.payer),8_000_000)
        self.assertEqual(remote["posts"],["https://synthetic-worker.test/api/runs/article",
            *(["https://synthetic-worker.test/api/runs/first-article/resume"] if repair else []),
            "https://synthetic-worker.test/api/runs/first-article/approve","https://synthetic-worker.test/api/runs/first-article/publish-pr"])

    def test_canonical_generation_reserves_once_and_lost_response_reuses_owned_records(self):
        op=self.start();self.ready(op)
        candidates=[{"id":"best","title":"Evidence of tested work","keyword":"tested work","opportunityScore":700},
            {"id":"covered","title":"Old work","keyword":"old work","opportunityScore":999,"alreadyWritten":True}]
        accepted=Mock(status_code=202,content=b"json");accepted.json.return_value={"run_id":"first-article","status":"queued"}
        def queue(*args,**kwargs):
            self.assertFalse(connection.in_atomic_block)
            self.assertEqual(kwargs["json"]["topic_candidate_id"],"best")
            return accepted
        with patch.object(views,"_topic_selection_candidate_pool",return_value=candidates), \
            patch.object(views,"_article_capabilities_for_context",side_effect=self.capabilities), \
            patch.object(views,"analytics_config_for_content_factory",return_value={}), \
            patch.object(views.http_client,"post",side_effect=queue) as post:
            claimed=prepare._claim_prepare(op.pk,timezone.now())
            article.advance(claimed,self.context,self.config,user=self.requester,now=timezone.now())
            self.assertIsNotNone(prepare._finish_prepare(claimed,now=timezone.now()))
            op.refresh_from_db()
            self.assertEqual(op.receipt["first_article_run_id"],"first-article")
            # Lose the caller's response/reference: only durable records remain.
            op.receipt.pop("first_article_run_id");op.receipt.pop("child_run_id",None)
            op.next_attempt_at=timezone.now();op.save(update_fields=["receipt","next_attempt_at"])
            claimed=prepare._claim_prepare(op.pk,timezone.now())
            article.advance(claimed,self.context,self.config,user=self.requester,now=timezone.now())
            prepare._finish_prepare(claimed,now=timezone.now())
        post.assert_called_once()
        self.assertEqual(Ledger.objects.filter(kind="SPEND",source="CONTENT_FACTORY").count(),1)
        self.assertEqual(self.balance(self.payer),8_000_000)
        run=ContentFactoryRun.objects.get(run_id="first-article")
        self.assertEqual(run.run_request["first_article_goal_operation_id"],str(op.pk))
        self.assertEqual(run.run_request["expected_source_sha"],"a"*40)
        dispatch=ContentFactoryDispatchOutbox.objects.get(client_request_id=op.receipt["first_article_billing"]["client_request_id"])
        self.assertEqual((dispatch.state,dispatch.run_id),("delivered","first-article"))
        self.assertFalse(goals.complete(op.receipt["goal"]))

    def test_replacement_credit_reholds_accepted_price_and_stale_claim_cannot_erase_it(self):
        first=self.start()
        successor=self.start(answers(publish_mode="review",billing={"policy":"reserved","expected_points":12,"accepted":True}))
        first.refresh_from_db();self.assertEqual(first.state,"cancelled")
        self.assertNotIn("first_article_billing",successor.receipt)
        stale=prepare._claim_prepare(successor.pk,timezone.now())
        refund=WebsiteConnectionOperation.objects.get(action="goal-refund",payload__goal_operation_id=str(first.pk))
        self.assertEqual(advance_refund(refund.pk,now=timezone.now()),"completed")
        successor.refresh_from_db()
        self.assertEqual(successor.receipt["first_article_billing"]["cost_points"],12)
        stale.state="pending";stale.next_attempt_at=timezone.now()
        self.assertIsNone(prepare._persist_prepare(stale,now=timezone.now()))
        successor.refresh_from_db()
        self.assertIn("first_article_billing",successor.receipt)
        self.assertEqual(self.balance(self.payer),8_000_000)
        self.assertEqual(Ledger.objects.filter(kind="SPEND",source="CONTENT_FACTORY").count(),2)
        self.assertEqual(Ledger.objects.filter(kind="REFUND",source="CONTENT_FACTORY").count(),1)

    def terminal_parent_child(self, *, outcome):
        """Synthetic dynamic DB IDs; raw CF producer fixtures are checked offline."""
        from .article_publication_evidence import original_scope
        from .website_first_article_refunds import reserve_refund
        op = self.start()
        billing = op.receipt["first_article_billing"]
        request = {**self.binding, "first_article_goal_id": str(op.pk), "first_article_goal_operation_id": str(op.pk),
            "client_request_id": billing["client_request_id"], "roo_points_ledger_id": billing["spend_ledger_id"],
            "roo_points_billing_user_id": billing["payer_user_id"], "github_repo": self.website.github_repo,
            "deletion_epoch": 0, "operation_id": "synthetic-parent-child", "operation_attempt": 1}
        run = ContentFactoryRun.objects.create(run_id="parent-settlement-child", organization=self.org, domain=self.org.domain,
            workflow="article_generation", github_repo=self.website.github_repo, run_request=request, status="cancelled")
        proof = {"version": 1, "purpose": "article_cancellation", "run_id": run.run_id, "goal_id": str(op.pk),
            "scope": original_scope(run), "generation": 0, "state_version": 5, "outcome": outcome, "cancelled": True,
            "delivered_value": False if outcome == "no_article_effect" else None,
            "watermark": {"operation_id": request["operation_id"], "attempt": 1, "cancelled": True}}
        proof["receipt_id"] = goals.digest(proof)
        run.result = {"article_cancellation_v1": proof, "reconciliation_refund_pending": True,
            "article_terminal_refund": {"status": "deferred_to_goal"}}
        run.save(update_fields=["result"])
        ContentFactoryDispatchOutbox.objects.create(client_request_id=billing["client_request_id"], organization=self.org,
            state="delivered", run_id=run.run_id, payload={"request": request})
        op.state = "needs_attention"
        op.receipt = {**op.receipt, "first_article_run_id": run.run_id}
        op.save(update_fields=["state", "receipt"])
        return op, run, reserve_refund(op)

    def test_unknown_child_cannot_credit_terminal_parent_or_complete_its_outbox(self):
        op, child, row = self.terminal_parent_child(outcome="unknown")
        self.assertEqual(advance_refund(row.pk), "needs_attention")
        row.refresh_from_db(); op.refresh_from_db(); child.refresh_from_db()
        self.assertEqual(row.receipt["status"], "credit_needs_reconciliation")
        self.assertEqual(op.receipt["first_article_billing"]["status"], "reserved")
        self.assertTrue(child.result["reconciliation_refund_pending"])
        self.assertFalse(Ledger.objects.filter(kind="REFUND", source="CONTENT_FACTORY").exists())
        self.assertEqual(self.balance(self.payer), 8_000_000)

    def test_duplicate_parent_settlement_credits_one_original_allocation(self):
        op, child, row = self.terminal_parent_child(outcome="no_article_effect")
        self.assertEqual(advance_refund(row.pk), "completed")
        self.assertIsNone(advance_refund(row.pk))
        op.refresh_from_db(); child.refresh_from_db()
        original = Ledger.objects.get(pk=op.receipt["first_article_billing"]["spend_ledger_id"])
        credit = Ledger.objects.get(kind="REFUND", source="CONTENT_FACTORY")
        self.assertEqual(credit.refund_of_id, original.pk)
        self.assertEqual(credit.user_id, self.payer.pk)
        self.assertEqual(self.balance(self.payer), 20_000_000)
        self.assertEqual(child.run_request["roo_points_billing_status"], "refunded")

    def test_postgres_competing_parent_workers_credit_once(self):
        if connection.vendor != "postgresql":
            self.skipTest("Concurrent row-lock regression requires PostgreSQL")
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        from django.db import close_old_connections
        op, child, row = self.terminal_parent_child(outcome="no_article_effect")
        barrier = Barrier(2)
        def settle():
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                return advance_refund(row.pk)
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(lambda _: settle(), range(2)))
        self.assertCountEqual(outcomes, ["completed", None])
        self.assertEqual(Ledger.objects.filter(kind="REFUND", source="CONTENT_FACTORY").count(), 1)
        self.assertEqual(self.balance(self.payer), 20_000_000)
