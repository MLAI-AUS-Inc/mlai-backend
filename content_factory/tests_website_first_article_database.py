"""Synthetic first-article provider-chain records in the existing PostgreSQL CI lane.

Do not run this locally without the AGENTS.md migration approval. No migration
files are added; the existing test database schema is used unchanged.
"""
from copy import deepcopy
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
from .website_contract import WebsiteAuthorityError
from .website_first_article_refunds import advance_refund
from .website_models import WebsiteConnectionOperation


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
            target="first_article_live",consent=choices or answers())

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
        self.assertEqual(self.balance(self.payer),14_000_000)
        run=ContentFactoryRun.objects.get(run_id="first-article")
        self.assertEqual(run.run_request["first_article_goal_operation_id"],str(op.pk))
        self.assertEqual(run.run_request["expected_source_sha"],"a"*40)
        dispatch=ContentFactoryDispatchOutbox.objects.get(client_request_id=op.receipt["first_article_billing"]["client_request_id"])
        self.assertEqual((dispatch.state,dispatch.run_id),("delivered","first-article"))
        self.assertFalse(goals.complete(op.receipt["goal"]))

    def test_replacement_credit_reholds_accepted_price_and_stale_claim_cannot_erase_it(self):
        first=self.start()
        successor=self.start(answers(publish_mode="review"))
        first.refresh_from_db();self.assertEqual(first.state,"cancelled")
        self.assertNotIn("first_article_billing",successor.receipt)
        stale=prepare._claim_prepare(successor.pk,timezone.now())
        refund=WebsiteConnectionOperation.objects.get(action="goal-refund",payload__goal_operation_id=str(first.pk))
        self.assertEqual(advance_refund(refund.pk,now=timezone.now()),"completed")
        successor.refresh_from_db()
        self.assertEqual(successor.receipt["first_article_billing"]["cost_points"],6)
        stale.state="pending";stale.next_attempt_at=timezone.now()
        self.assertIsNone(prepare._persist_prepare(stale,now=timezone.now()))
        successor.refresh_from_db()
        self.assertIn("first_article_billing",successor.receipt)
        self.assertEqual(self.balance(self.payer),14_000_000)
        self.assertEqual(Ledger.objects.filter(kind="SPEND",source="CONTENT_FACTORY").count(),2)
        self.assertEqual(Ledger.objects.filter(kind="REFUND",source="CONTENT_FACTORY").count(),1)
