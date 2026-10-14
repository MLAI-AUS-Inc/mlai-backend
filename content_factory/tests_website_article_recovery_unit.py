"""Article controller recovery with real policy and only provider/storage seams."""
from copy import deepcopy
from unittest.mock import patch
from uuid import uuid4

from django.test import SimpleTestCase,override_settings
from rest_framework.response import Response

from . import tests_website_first_article_unit as fixtures
from . import website_prepare as prepare,website_goal_runner as runner
from . import vibe_marketing_views as views
from .website_contract import WebsiteAuthorityError
from .website_failure_contract import attach_failure_projection,classify_failure


@override_settings(ARTICLES_FIRST_ARTICLE_GOAL_ENABLED=True,ARTICLES_SETUP_CONSENT_ENABLED=True,
    ARTICLES_WEBSITE_GOAL_RUNNER_ENABLED=True,ARTICLES_WEBSITE_GOAL_ENROLLED_ORGANIZATIONS=["7"],
    ARTICLES_FAILURE_TAXONOMY_POLICY_ENABLED=True,ARTICLES_FAILURE_TAXONOMY_ENROLLED_ORGANIZATIONS=["7"],
    ARTICLES_WEBSITE_GOAL_BUDGETS_ENABLED=True,ARTICLES_ARTICLE_RECOVERY_ENABLED=True)
class ArticleRecoveryControllerTests(SimpleTestCase):
    def setUp(self):
        self.chain=fixtures.FirstArticleControllerTests("runTest")
        self.chain.setUp();self.addCleanup(self.chain.doCleanups)
        self.op,self.run=self.chain.op,self.chain.run
        self.op.receipt.update(first_article_run_id=self.run.run_id,current_step="first-article")
        self.run.status="failed"
        self.run.result={"generation":1,"state_version":2,**attach_failure_projection({},
            classify_failure("DIRECTORY_STYLE_REVIEW_FAILED","article_style"),str(uuid4()))}

    def record_failure(self):
        with self.assertRaises(WebsiteAuthorityError) as caught:self.chain.advance()
        prepare.prepare_failure(self.op,"first-article",caught.exception,now=self.chain.now)
        runner.finish_goal(self.op,now=self.chain.now)
        return caught.exception

    def test_failed_article_resumes_same_owned_run_without_recharging_or_restarting_generation(self):
        held=deepcopy(self.op.receipt["first_article_billing"])
        self.record_failure()
        def accepted(**kwargs):
            self.assertEqual(kwargs["action"],"resume")
            self.assertIs(kwargs["run"],self.run)
            self.assertEqual(kwargs["goal_operation_id"],self.op.pk)
            self.run.status="queued"
            return Response({"run_id":self.run.run_id},status=202)
        with patch.object(views,"control_article_run",side_effect=accepted) as resume, \
            patch.object(views,"start_article_generation") as generate:
            self.chain.advance()
        resume.assert_called_once();generate.assert_not_called()
        self.assertEqual(self.op.state,"pending")
        self.assertEqual(self.op.receipt["first_article_billing"],held)
        self.assertEqual(self.chain.fixture.spend.call_count,1)
        self.assertEqual(self.op.receipt["article_recovery"]["state"],"submitted")

    def test_duplicate_failure_observation_keeps_schedule_and_counts_one_occurrence(self):
        error=self.record_failure()
        first=deepcopy(self.op.receipt["article_recovery"])
        count=deepcopy(self.op.receipt["goal"]["attempts_by_class"])
        prepare.prepare_failure(self.op,"first-article",error,now=self.chain.now)
        runner.finish_goal(self.op,now=self.chain.now)
        self.assertEqual(self.op.state,"pending")
        self.assertIsNotNone(self.op.next_attempt_at)
        self.assertEqual(self.op.receipt["article_recovery"],first)
        self.assertEqual(self.op.receipt["goal"]["attempts_by_class"],count)

    def test_uncertain_resume_reuses_reserved_attempt_and_does_not_change_payment(self):
        self.record_failure()
        held=deepcopy(self.op.receipt["first_article_billing"])
        def uncertain(**kwargs):
            self.run.run_request["operation_attempt"]=2
            self.chain.workflow.payload["attempt"]=2
            return Response({"code":"worker_unavailable","detail":"Synthetic lost response"},status=502)
        with patch.object(views,"control_article_run",side_effect=uncertain),self.assertRaises(WebsiteAuthorityError):
            self.chain.advance()
        self.assertEqual(self.op.receipt["article_recovery"]["state"],"dispatching")
        def accepted(**kwargs):
            self.assertEqual(kwargs["run"].run_request["operation_attempt"],2)
            self.run.status="queued"
            return Response({"run_id":self.run.run_id},status=202)
        with patch.object(views,"control_article_run",side_effect=accepted) as resume:
            self.chain.advance()
        resume.assert_called_once()
        self.assertEqual(self.op.receipt["article_recovery"]["state"],"submitted")
        self.assertEqual(self.op.receipt["first_article_billing"],held)
        self.assertEqual(self.chain.fixture.spend.call_count,1)

    def test_submitted_resume_does_not_replay_for_the_same_old_failure(self):
        self.record_failure()
        self.op.receipt["article_recovery"]["state"]="submitted"
        with patch.object(views,"control_article_run") as resume:
            self.chain.advance()
        resume.assert_not_called()
        self.assertEqual(self.op.state,"pending")

    def test_cancelled_goal_cannot_resume_its_saved_repair_intent(self):
        self.record_failure();self.op.state="cancelled"
        with patch.object(views,"control_article_run") as resume,self.assertRaises(WebsiteAuthorityError):
            self.chain.advance()
        resume.assert_not_called()

    def test_recovery_rechecks_shared_budget_before_dispatch(self):
        self.record_failure()
        self.op.receipt["goal"]["budget_used"]["model_cost"]=25
        with patch.object(views,"control_article_run") as resume:
            self.chain.advance()
        resume.assert_not_called()
        self.assertEqual(self.op.state,"needs_attention")
        self.assertEqual(self.op.receipt["budget_stop_reason"],"model_cost")

    def test_failed_publication_child_recovers_itself_and_retains_the_original_source(self):
        failure=deepcopy(self.run.result)
        self.chain.approve();self.chain.publish_child()
        child=self.chain.child;child.status="failed";child.result={**child.result,**failure}
        self.record_failure()
        def accepted(**kwargs):
            self.assertIs(kwargs["run"],child);child.status="queued"
            return Response({"run_id":child.run_id},status=202)
        with patch.object(views,"control_article_run",side_effect=accepted) as resume, \
            patch.object(views,"start_article_generation") as generate:
            self.chain.advance()
        resume.assert_called_once();generate.assert_not_called()
        self.assertEqual(self.op.receipt["first_article_run_id"],self.run.run_id)
        self.assertEqual(self.op.receipt["first_article_publish_run_id"],child.run_id)

    def test_rollout_or_budget_disable_does_not_enable_automatic_recovery(self):
        for settings in ({"ARTICLES_ARTICLE_RECOVERY_ENABLED":False},{"ARTICLES_WEBSITE_GOAL_BUDGETS_ENABLED":False}):
            with self.subTest(settings=settings),override_settings(**settings):
                self.op.state="running"
                with patch.object(views,"control_article_run") as resume,self.assertRaises(WebsiteAuthorityError):
                    self.chain.advance()
                resume.assert_not_called()
        self.assertNotIn("article_recovery",self.op.receipt)

    def test_unknown_publication_failure_stops_at_the_enrolled_registry_boundary(self):
        from .website_failure_scope import failure_scope
        self.chain.approve();self.chain.publish_child()
        self.chain.child.status="failed"
        self.chain.child.result={"error_code":"unregistered_worker_failure"}
        with failure_scope(7),self.assertRaises(WebsiteAuthorityError) as caught:
            self.chain.advance()
        self.assertEqual(caught.exception.failure["code"],"UNREGISTERED_FAILURE_CODE")
        self.assertEqual(caught.exception.failure["failure_class"],"unsupported")

    def test_unknown_usage_stops_before_buying_more_repair_work(self):
        self.record_failure()
        self.op.receipt["goal"]["missing_usage_run_ids"]=[self.run.run_id]
        with patch.object(views,"control_article_run") as resume:
            self.chain.advance()
        resume.assert_not_called()
        self.assertEqual(self.op.receipt["budget_stop_reason"],"usage_unknown")

    def test_changed_source_cannot_reuse_an_original_source_repair(self):
        self.record_failure()
        self.op.receipt["goal"]["current_source_sha"]="b"*40
        with patch.object(views,"control_article_run") as resume,self.assertRaises(WebsiteAuthorityError) as caught:
            self.chain.advance()
        resume.assert_not_called()
        self.assertEqual(caught.exception.authority_code,"website_source_changed")

    def test_unknown_saved_intent_version_cannot_dispatch(self):
        self.record_failure();self.op.receipt["article_recovery"]["version"]=2
        with patch.object(views,"control_article_run") as resume,self.assertRaises(WebsiteAuthorityError):
            self.chain.advance()
        resume.assert_not_called()

    def test_malformed_saved_intent_fails_closed_before_dispatch(self):
        self.record_failure()
        saved=deepcopy(self.op.receipt["article_recovery"])
        for field,value in (("state",[]),("observation",[]),("goal",None)):
            with self.subTest(field=field):
                self.op.receipt["article_recovery"]={**saved,field:value}
                with patch.object(views,"control_article_run") as resume,self.assertRaises(WebsiteAuthorityError):
                    self.chain.advance()
                resume.assert_not_called()
