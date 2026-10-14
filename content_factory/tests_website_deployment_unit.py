"""Provider hints, exact scope fences and manual deployment recovery without DB."""
import base64
from contextlib import nullcontext
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch
from django.test import SimpleTestCase, override_settings
from django.utils import timezone
from . import website_deployment as deployment, website_prepare as prepare, website_goal_budget as budget, website_goal as goals
from .tests_website_goal_unit import scope

WORKFLOW = "on:\n  push:\n    branches: [main]\njobs:\n  deploy:\n    steps:\n      - uses: actions/deploy-pages@v4\n"


class ProviderTests(SimpleTestCase):
    def test_config_and_public_headers_identify_every_provider_without_inventing_auto_deploy(self):
        for files,headers,provider in (({"vercel.json":"{}"},{},"vercel"),({"netlify.toml":""},{},"netlify"),({"wrangler.toml":""},{},"cloudflare"),({}, {"server":"Vercel"},"vercel"),({}, {"x-nf-request-id":"private-value"},"netlify"),({}, {"cf-ray":"private-value"},"cloudflare")):
            with self.subTest(provider=provider):
                result=deployment.fingerprint(files,app_root="",branch="main",default_branch="main",headers=headers)
                self.assertEqual(result["provider"],provider);self.assertEqual(result["automation"],"unknown")
                self.assertNotIn("private-value",str(result))
        self.assertEqual(deployment.fingerprint({"apps/admin/vercel.json":"{}"},app_root="apps/site",branch="main",default_branch="main")["provider"],"unknown")
        self.assertEqual(deployment.fingerprint({"vercel.json":"{}","netlify.toml":""},app_root="",branch="main",default_branch="main")["provider"],"unknown")

    def test_push_to_default_branch_is_required_for_automatic_actions(self):
        self.assertEqual(deployment.workflow_signal(WORKFLOW,"main","main"),{"provider":"github_pages","automation":"automatic"})
        for source,branch,default in ((WORKFLOW,"release","main"),(WORKFLOW.replace("[main]","[other]"),"main","main"),(WORKFLOW.replace("branches: [main]","paths: ['unrelated/**']"),"main","main"),(WORKFLOW.replace("branches: [main]","branches: ['*', '!main']"),"main","main")):
            self.assertEqual(deployment.workflow_signal(source,branch,default)["automation"],"unknown")
        self.assertEqual(deployment.workflow_signal(WORKFLOW.replace("  push:\n    branches: [main]","  workflow_dispatch:"),"main","main")["automation"],"manual")
        self.assertIsNone(deployment.workflow_signal("on: push\njobs:\n  test:\n    steps:\n      - uses: actions/checkout@v4","main","main"))
        generic=WORKFLOW.replace("      - uses: actions/deploy-pages@v4","    environment: production\n    steps: []")
        self.assertEqual(deployment.workflow_signal(generic,"main","main")["provider"],"github_actions")

    @override_settings(ARTICLES_DEPLOYMENT_DETECTION_ENABLED=True)
    def test_replayed_fingerprint_uses_saved_source_and_disconnect_wins_after_provider_read(self):
        connection=SimpleNamespace(capabilities={},generation=3,app_root="",branch="main",save=Mock())
        binding={"source_sha":"a"*40}
        with patch.object(deployment,"authority_guard",return_value=nullcontext(connection)),patch.object(deployment,"read_files",return_value=({"vercel.json":"{}"},"main")) as read:
            first=deployment.ensure_fingerprint(connection,binding)
            self.assertEqual(deployment.ensure_fingerprint(connection,binding),first);read.assert_called_once()
            deployment.observe_headers(connection,binding,{"cf-ray":"credential-canary"})
            self.assertEqual(connection.capabilities["deploymentFingerprint"]["provider"],"vercel")
            self.assertNotIn("credential-canary",str(connection.capabilities))
        connection.capabilities={};connection.save.reset_mock()
        from .website_contract import WebsiteAuthorityError
        with patch.object(deployment,"authority_guard",side_effect=[nullcontext(connection),WebsiteAuthorityError("website_operation_cancelled","Cancelled")]),patch.object(deployment,"read_files",return_value=({"netlify.toml":""},"main")):
            with self.assertRaises(WebsiteAuthorityError):deployment.ensure_fingerprint(connection,binding)
        connection.save.assert_not_called()

    def test_provider_reads_use_scoped_read_token_fixed_commit_and_revoke(self):
        from integrations import http_client
        connection=SimpleNamespace(installation_id="4",repository_id=12,github_repo="fixture/site",app_root="apps/site")
        def response(url,**kwargs):
            value=Mock(status_code=404)
            if url.endswith("/fixture/site"):
                value.status_code=200;value.json.return_value={"id":12,"full_name":"fixture/site","default_branch":"main"}
            elif "/apps/site/vercel.json?ref=" in url:
                value.status_code=200;value.json.return_value={"size":2,"encoding":"base64","content":base64.b64encode(b"{}").decode()}
            return value
        with patch("integrations.services.github_app.create_installation_access_token",return_value=SimpleNamespace(token="synthetic")) as token,patch.object(http_client,"get",side_effect=response) as get,patch.object(http_client,"delete") as revoke:
            files,branch=deployment.read_files(connection,"a"*40)
        self.assertEqual(files,{"apps/site/vercel.json":"{}"});self.assertEqual(branch,"main")
        self.assertEqual(token.call_args.kwargs["permission_mode"],"read");self.assertFalse(token.call_args.kwargs["use_cache"])
        self.assertTrue(all(call.args[0].endswith("ref="+"a"*40) for call in get.call_args_list[1:]));revoke.assert_called_once()


@override_settings(ARTICLES_DEPLOYMENT_DETECTION_ENABLED=True)
class ManualRecoveryTests(SimpleTestCase):
    def test_manual_workflow_asks_once_with_stable_action_and_retains_quiet_observer(self):
        now=timezone.now()
        connection=SimpleNamespace(organization_id=42,generation=3,app_root="",verified_sha="a"*40,capabilities={"deploymentFingerprint":{"version":1,"provider":"github_pages","automation":"manual","connectionGeneration":3,"appRoot":"","sourceSha":"a"*40}})
        op=SimpleNamespace(pk="prepare",connection=connection,payload={},receipt={},attempts=0)
        from .website_contract import WebsiteAuthorityError
        error=WebsiteAuthorityError("deployment_source_unverified","Not deployed")
        prepare.prepare_failure(op,"verify",error,now=now)
        self.assertEqual(op.state,"needs_user");self.assertEqual(op.next_attempt_at,now+timedelta(hours=2))
        action=op.receipt["user_action"].copy()
        prepare.prepare_failure(op,"verify",error,now=now+timedelta(hours=2))
        self.assertEqual(op.receipt["user_action"],action)
        self.assertIn("Actions tab",op.receipt["message"])
        # A verified marker uses the ordinary prepare success path; no extra click is required.
        self.assertEqual(action["id"],"deploy_site")

    def test_actual_prepare_chain_resumes_when_manual_deployment_marker_appears(self):
        from . import tests_prepare_permissions_unit as fixtures
        from .website_contract import WebsiteAuthorityError
        value=fixtures.PrepareVerificationRuntimeTests();value.setUp()
        value.website.capabilities["deploymentFingerprint"]={"version":1,"provider":"github_pages","automation":"manual","connectionGeneration":value.website.generation,"appRoot":value.website.app_root,"sourceSha":value.website.verified_sha}
        value.live_error=WebsiteAuthorityError("live_route_unavailable","The marker has not deployed")
        with value.boundaries():
            prepare.advance_prepare(value.prepare.pk,now=value.now)
            self.assertEqual(value.prepare.state,"needs_user",value.prepare.receipt)
            action=value.prepare.receipt["user_action"].copy()
            prepare.advance_prepare(value.prepare.pk,now=value.now+timedelta(hours=2))
            self.assertEqual(value.prepare.receipt["user_action"],action)
            value.live_error=None
            prepare.advance_prepare(value.prepare.pk,now=value.now+timedelta(hours=4))
            self.assertIsNotNone(value.live)
            prepare.advance_prepare(value.prepare.pk,now=value.now+timedelta(hours=4,minutes=2))
            self.assertEqual(value.prepare.state,"completed",value.prepare.receipt)
            self.assertEqual(value.prepare.receipt["status"],"ready")


class OwnerWaitBudgetTests(SimpleTestCase):
    @override_settings(ARTICLES_WEBSITE_GOAL_BUDGETS_ENABLED=True)
    def test_goal_finish_opens_and_closes_owner_interval_through_existing_ledger(self):
        from . import website_goal_runner as runner,tests_website_goal_unit as fixtures
        op=fixtures.GoalRunnerRegressions().operation();now=timezone.now()
        op.state="needs_user";op.receipt["user_action"]={"id":"merge_pr"}
        runner.finish_goal(op,now=now)
        opened=op.receipt["goal"]["owner_wait_started_at"]
        runner.finish_goal(op,now=now+timedelta(days=4))
        self.assertEqual(op.receipt["goal"]["owner_wait_started_at"],opened)
        op.state="pending";op.receipt.pop("user_action")
        runner.finish_goal(op,now=now+timedelta(days=4))
        self.assertNotIn("owner_wait_started_at",op.receipt["goal"])
        self.assertEqual(op.receipt["goal"]["owner_wait_completed_seconds"],4*86400)

    def test_replays_pause_only_owner_time_and_resume_at_exact_budget_edge(self):
        now=timezone.now();goal=goals.new_goal("goal",scope(),now=now)
        goal=budget.update_owner_wait(goal,waiting=True,now=now+timedelta(hours=1))
        self.assertEqual(budget.update_owner_wait(goal,waiting=True,now=now+timedelta(days=4)),goal)
        self.assertIsNone(budget.exhaustion(goal,now=now+timedelta(days=4)))
        self.assertEqual(budget.exhaustion({**goal,"budget_used":{"model_cost":25}},now=now+timedelta(days=4)),"model_cost")
        resumed=budget.update_owner_wait(goal,waiting=False,now=now+timedelta(days=4))
        self.assertEqual(budget.update_owner_wait(resumed,waiting=False,now=now+timedelta(days=4)),resumed)
        self.assertIsNone(budget.exhaustion(resumed,now=now+timedelta(days=4,hours=46)))
        self.assertEqual(budget.exhaustion(resumed,now=now+timedelta(days=4,hours=47)),"wall_time")
        self.assertEqual(budget.exhaustion(goals.new_goal("provider-wait",scope(),now=now),now=now+timedelta(hours=48)),"wall_time")

    def test_invalid_owner_intervals_never_grant_more_budget(self):
        now=timezone.now();goal=goals.new_goal("goal",scope(),now=now)
        for changes in ({"owner_wait_started_at":"invalid"},{"owner_wait_started_at":(now+timedelta(minutes=1)).isoformat()},{"owner_wait_completed_seconds":True},{"owner_wait_completed_seconds":1},{"owner_wait_completed_seconds":-1}):
            with self.subTest(changes=changes):self.assertEqual(budget.exhaustion({**goal,**changes},now=now),"invalid_budget_contract")
