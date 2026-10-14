"""Original goal consent, price/grant matrices and page fixtures without a database."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock,patch
from django.test import SimpleTestCase,override_settings
from django.utils import timezone
from . import website_setup_consent as consent,website_goal as goals,website_goal_runner as runner
from .website_article_page import choose_page
from .website_contract import WebsiteAuthorityError
from . import tests_website_goal_unit as fixtures
from . import tests_prepare_journey_unit as journey_fixtures


def answers(**updates):
    return {"version":1,"publish_mode":"auto","page_policy":"auto","page_mode":"none","page_route":"/articles","ci_mode":"hosted","billing":{"policy":"reserved","expected_points":6,"accepted":True},**updates}


@override_settings(ARTICLES_SETUP_CONSENT_ENABLED=True,ARTICLES_WEBSITE_GOAL_RUNNER_ENABLED=True,ARTICLES_WEBSITE_GOAL_ENROLLED_ORGANIZATIONS=["7"],ARTICLES_AUTO_PAGE_ENABLED=True,ARTICLES_FAILURE_TAXONOMY_POLICY_ENABLED=True,ARTICLES_FAILURE_TAXONOMY_ENROLLED_ORGANIZATIONS=["7"])
class ConsentTests(SimpleTestCase):
    def setUp(self):
        self.now=timezone.now();self.goal=goals.new_goal("prepare",fixtures.scope(),now=self.now)
        self.goal["consent"]=consent.seal(self.goal,answers(),user_id=4,now=self.now)

    def test_exact_goal_scope_digest_and_typed_answers_are_required(self):
        self.assertTrue(consent.valid(self.goal))
        for update in ({"goal_id":"foreign"},{"scope":{**fixtures.scope(),"company_id":"foreign"}},{"digest":"a"*64},{"version":True}):
            changed=deepcopy(self.goal);changed["consent"].update(update)
            self.assertFalse(consent.valid(changed))
        for value in (answers(billing={"policy":"reserved","expected_points":True,"accepted":True}),answers(billing={"policy":"reserved","expected_points":7,"accepted":True}),answers(page_route="/../secret"),answers(page_route="/articles?token=secret"),answers(publish_mode=[]),answers(billing={"policy":"reserved","expected_points":6,"accepted":"true"})):
            with self.assertRaises(WebsiteAuthorityError):consent.answers(value,expected_points=6)

    def test_all_repository_rungs_have_one_complete_grant_set_and_hosted_needs_no_github(self):
        for rung in ("adopt","native_collection","deterministic","model_adapter","agent"):
            self.assertEqual(consent.required_grants(ci_mode="hosted",strategy=rung),{"contents":"write","pull_requests":"write"},rung)
            self.assertEqual(consent.required_grants(ci_mode="repository",strategy=rung),{"contents":"write","pull_requests":"write","workflows":"write","checks":"read","statuses":"read"},rung)
        self.assertEqual(consent.required_grants(ci_mode="repository",strategy="hosted"),{})
        for channel in ("hosted","wordpress","ghost"):
            self.assertEqual(consent.required_grants(channel=channel),{})

    def operation(self):
        op=fixtures.GoalRunnerRegressions().operation();op.receipt["goal"]=deepcopy(self.goal)
        op.connection.organization=SimpleNamespace(domain="fixture.test")
        rows=journey_fixtures.FrozenPrepareContextTests.rows(first=op);op.connection.operations=rows
        return op

    def test_repeat_start_and_permission_resume_reuse_the_saved_answers(self):
        from .website_models import WebsiteConnectionOperation
        op=self.operation();op.state="needs_user"
        with patch.object(WebsiteConnectionOperation.objects,"get_or_create") as create:
            result=runner.start_goal(op.connection,company_id=9,user=SimpleNamespace(pk=4),retry=True,target="ready",now=self.now)
        self.assertIs(result,op);create.assert_not_called()
        self.assertEqual(op.receipt["goal"]["consent"],self.goal["consent"])
        self.assertEqual(op.state,"pending")

    def test_changed_answers_create_successor_and_price_change_never_cancels_old_work(self):
        from .website_models import WebsiteConnectionOperation
        from .models import OrganizationContentConfig
        op=self.operation();created=fixtures.GoalRunnerRegressions().operation();created.pk="successor"
        with patch("content_factory.billing.get_content_factory_article_cost_points",return_value=6),patch("content_factory.website_prepare._cancel_prepare_child") as cancel,patch.object(OrganizationContentConfig.objects,"get"),patch.object(WebsiteConnectionOperation.objects,"get_or_create",return_value=(created,True)):
            result=runner.start_goal(op.connection,company_id=9,user=SimpleNamespace(pk=4),retry=True,target="ready",now=self.now,consent=answers(publish_mode="review"))
        self.assertEqual(result.receipt["goal"]["consent"]["goal_id"],"successor")
        self.assertEqual(result.receipt["goal"]["consent"]["answers"]["publish_mode"],"review")
        self.assertEqual(op.state,"cancelled");cancel.assert_called_once()
        op=self.operation()
        with patch("content_factory.billing.get_content_factory_article_cost_points",return_value=7),patch("content_factory.website_prepare._cancel_prepare_child") as cancel:
            with self.assertRaises(WebsiteAuthorityError):runner.start_goal(op.connection,company_id=9,user=SimpleNamespace(pk=4),retry=True,target="ready",now=self.now,consent=answers())
        cancel.assert_not_called();self.assertEqual(op.state,"pending")

    def test_source_rebinding_keeps_answers_but_is_not_valid_without_trusted_scope_path(self):
        changed=deepcopy(self.goal);new_scope={**fixtures.scope(),"configuration_revision":2}
        changed["scope"]=new_scope;self.assertFalse(consent.valid(changed))
        changed["consent"]=consent.rebind_trusted_source(self.goal,new_scope)
        self.assertTrue(consent.valid(changed));self.assertEqual(changed["consent"]["answers"],answers())

    def test_plan_is_scoped_and_price_and_optional_permissions_are_shown_once(self):
        op=self.operation();context=SimpleNamespace(company=SimpleNamespace(pk=9),organization=op.connection.organization)
        with patch("content_factory.billing.get_content_factory_article_cost_points",return_value=6):
            plan=consent.setup_plan(context,SimpleNamespace(website_connection=op.connection),{"githubPermissions":{"contents":True,"pull_requests":False,"workflows":True,"checks":True,"statuses":True,"settingsUrl":"https://github.com/settings/installations/4"}})
        self.assertEqual((plan["companyId"],plan["connectionId"],plan["connectionGeneration"]),("9","website",3))
        self.assertEqual(plan["github"]["missing"],["pull_requests"])
        self.assertEqual(plan["articlePoints"],6)
        self.assertEqual(plan["goalTarget"],"ready")


class PageChoiceTests(SimpleTestCase):
    def fixture(self):
        return {"resolution":{"existence_state":"ambiguous","ranked_candidates":{"listing_surface_candidates":[{"route":"/stories","confidence":.95},{"route":"/about/story","confidence":.5}],"detail_surface_candidates":[{"route_template":"/stories/[slug]"}]}}}

    def test_talathrive_listing_beats_story_detail_pages(self):
        self.assertEqual(choose_page(self.fixture(),pick_when_ambiguous=True)["route"],"/stories")
        self.assertFalse(choose_page(self.fixture(),pick_when_ambiguous=True)["requiresChoice"])

    def test_vyavos_missing_listing_uses_articles(self):
        result=choose_page({"resolution":{"existence_state":"missing"}})
        self.assertEqual((result["mode"],result["route"]),("none","/articles"))

    def test_genuine_ambiguity_asks_only_an_owner_who_chose_to_pick(self):
        value=self.fixture();value["resolution"]["ranked_candidates"]["listing_surface_candidates"][1]["confidence"]=.94
        self.assertFalse(choose_page(value)["requiresChoice"])
        self.assertTrue(choose_page(value,pick_when_ambiguous=True)["requiresChoice"])
        from .website_prepare import _setup_surface
        with override_settings(ARTICLES_AUTO_PAGE_ENABLED=True):
            self.assertEqual(_setup_surface(SimpleNamespace(article_system={}),{"discovery":value},goal={"consent":{"answers":answers()}}),("existing","/stories"))

    def test_invalid_confidence_paths_and_detail_relationships_do_not_win(self):
        for score in (True,float("nan"),2,[],None):
            value=self.fixture();value["resolution"]["ranked_candidates"]["listing_surface_candidates"][0]["confidence"]=score
            self.assertTrue(choose_page(value,pick_when_ambiguous=True)["requiresChoice"])
        value={"resolution":{"ranked_candidates":{"listing_surface_candidates":[{"route":"/stories?token=secret","confidence":1}]}}}
        self.assertEqual(choose_page(value)["route"],"/articles")

@override_settings(ARTICLES_SETUP_CONSENT_ENABLED=True,ARTICLES_WEBSITE_GOAL_RUNNER_ENABLED=True,ARTICLES_WEBSITE_GOAL_ENROLLED_ORGANIZATIONS=["7"],ARTICLES_FAILURE_TAXONOMY_POLICY_ENABLED=True,ARTICLES_FAILURE_TAXONOMY_ENROLLED_ORGANIZATIONS=["7"])
class ConsentIntegrationTests(SimpleTestCase):
    def test_planner_checks_every_required_grant_before_scan(self):
        now=timezone.now();op=fixtures.GoalRunnerRegressions().operation()
        op.receipt["goal"]["consent"]=consent.seal(op.receipt["goal"],answers(ci_mode="repository"),user_id=4,now=now)
        config=SimpleNamespace(website_connection=op.connection)
        context=SimpleNamespace(company=SimpleNamespace(pk=9))
        proofs={"access":fixtures.proof_set(now)["access"]}
        for missing in ("contents","pull_requests","workflows","checks","statuses"):
            grants={key:True for key in consent.required_grants(ci_mode="repository")};grants[missing]=False
            with patch("content_factory.website_goal_evidence.collect_proofs",return_value=(proofs,"a"*40)):
                journey=runner.goal_journey(op,context,config,{"githubPermissions":grants},now=now)
            self.assertEqual(journey["nextAction"]["id"],"permissions",missing)
        grants={key:True for key in consent.required_grants(ci_mode="repository")}
        with patch("content_factory.website_goal_evidence.collect_proofs",return_value=(proofs,"a"*40)):
            self.assertEqual(runner.goal_journey(op,context,config,{"githubPermissions":grants},now=now)["nextAction"]["id"],"scan")

    def test_only_the_trusted_source_chain_rebinds_original_owner_answers(self):
        from .website_goal_scope import rebind_source_scope
        now=timezone.now();goal=goals.new_goal("goal",fixtures.scope(),now=now)
        goal["consent"]=consent.seal(goal,answers(),user_id=4,now=now)
        connection=fixtures.GoalRunnerRegressions().operation().connection
        connection.configuration_version=2;connection.verified_sha="b"*40
        rows=journey_fixtures.FrozenPrepareContextTests.rows();connection.operations=rows
        rows.__getitem__.return_value=[]
        self.assertIsNone(rebind_source_scope(goal,connection,company_id=9,now=now))
        rows.__getitem__.return_value=[SimpleNamespace(payload={"from_revision":1,"to_revision":2,"source_sha":"b"*40})]
        rebound=rebind_source_scope(goal,connection,company_id=9,now=now)
        self.assertTrue(consent.valid(rebound))
        self.assertEqual(rebound["consent"]["answers"],goal["consent"]["answers"])
        self.assertEqual(rebound["consent"]["accepted_at"],goal["consent"]["accepted_at"])

    def test_new_first_article_goal_requires_explicit_price_consent(self):
        from .website_models import WebsiteConnectionOperation
        now=timezone.now();op=fixtures.GoalRunnerRegressions().operation()
        op.connection.organization=SimpleNamespace(domain="fixture.test")
        rows=journey_fixtures.FrozenPrepareContextTests.rows(first=None);op.connection.operations=rows
        with patch.object(WebsiteConnectionOperation.objects,"get_or_create") as create:
            with self.assertRaises(WebsiteAuthorityError):runner.start_goal(op.connection,company_id=9,user=SimpleNamespace(pk=4),retry=False,target="first_article_live",now=now)
        create.assert_not_called()
        with patch("content_factory.billing.get_content_factory_article_cost_points",return_value=6),patch.object(WebsiteConnectionOperation.objects,"get_or_create",return_value=(op,True)):
            result=runner.start_goal(op.connection,company_id=9,user=SimpleNamespace(pk=4),retry=False,target="first_article_live",now=now,consent=answers())
        self.assertEqual(result.receipt["goal"]["target"],"first_article_live")
        self.assertTrue(consent.valid(result.receipt["goal"]))


class GrantCacheTests(SimpleTestCase):
    def test_rich_grants_cannot_reuse_legacy_cache_and_webhooks_clear_both_versions(self):
        from . import website_github_permissions as permissions
        with override_settings(ARTICLES_SETUP_CONSENT_ENABLED=False):
            old=permissions._cache_key("4")
        with override_settings(ARTICLES_SETUP_CONSENT_ENABLED=True):
            self.assertNotEqual(permissions._cache_key("4"),old)
        with patch.object(permissions.cache,"delete") as clear:
            permissions.invalidate_installation_grants("4")
        self.assertEqual({item.args[0] for item in clear.call_args_list},{"website-installation-grants-v1:4","website-installation-grants-v2:4"})
