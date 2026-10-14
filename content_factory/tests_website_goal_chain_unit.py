"""Run the actual enrolled context, journey, goal, dispatcher and queue chain."""
from contextlib import nullcontext, ExitStack
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4
from django.test import SimpleTestCase, override_settings
from django.utils import timezone
from . import website_prepare as prepare, website_goal as goals
from . import tests_one_click_operations_unit as fixtures
from . import tests_prepare_journey_unit as journey_fixtures

@override_settings(ARTICLES_WEBSITE_GOAL_RUNNER_ENABLED=True,
                   ARTICLES_WEBSITE_GOAL_ENROLLED_ORGANIZATIONS=["7"],
                   ARTICLES_FAILURE_TAXONOMY_POLICY_ENABLED=True,
                   ARTICLES_FAILURE_TAXONOMY_ENROLLED_ORGANIZATIONS=["7"],
                   CONTENT_FACTORY_URL="https://synthetic-worker.test", CONTENT_FACTORY_API_KEY="synthetic")
class RealGoalChainTests(SimpleTestCase):
    rows = staticmethod(journey_fixtures.FrozenPrepareContextTests.rows)

    def test_enrolled_goal_executes_real_context_journey_dispatch_and_remote_queue(self):
        from dataclasses import FrozenInstanceError
        from django.contrib.auth import get_user_model
        from founder_tools.services import FounderCompanyContext
        from integrations.models import GitHubInstallation
        from integrations.services import github_app
        from organizations.models import Organization
        from workflow_runs.models import ContentFactoryRun
        from .models import OrganizationContentConfig
        from .website_models import WebsiteConnection
        from . import vibe_marketing_views as views

        user = get_user_model()(pk=4, is_active=True, email="founder@example.test")
        organization = Organization(pk=7, domain="fixture.test")
        context = FounderCompanyContext(profile=SimpleNamespace(user=user, companies=self.rows()),
            company=SimpleNamespace(pk=9, id=9, domain="fixture.test"), organization=organization)
        website = WebsiteConnection(organization=organization, authorized_by=user,
            github_repo="fixture/site", repository_id=123, installation_id="4", generation=3,
            configuration_version=2, branch="main", verified_sha="a" * 40)
        config = OrganizationContentConfig(organization=organization, website_connection=website,
            github_repo=website.github_repo)
        op = fixtures.PrepareJourneyTests().operation()
        op.connection, op.connection_id = website, website.pk
        op.receipt["goal"] = goals.new_goal(str(op.pk), goals.scope_for(website, context.company.pk), now=timezone.now())
        captured = {}
        workflow = SimpleNamespace(pk=uuid4(), generation=3, connection=website, connection_id=website.pk, action="workflow", state="running", receipt={}, payload={}, attempts=0, next_attempt_at=None, created_at=timezone.now(), updated_at=timezone.now(), save=Mock(), refresh_from_db=Mock())
        def reserve(*, idempotency_key, defaults):
            workflow.payload = defaults["payload"]
            return workflow, True
        worker_run = ContentFactoryRun(run_id="scan-child", workflow="repo_scan", domain="fixture.test", github_repo="fixture/site", status="queued")
        def store(*, run_id, defaults):
            captured.update(defaults)
            worker_run.run_request = defaults["run_request"]
            return worker_run, True
        accepted = Mock(status_code=202, content=b"json")
        accepted.json.return_value = {"run_id": "scan-child", "status": "queued"}
        repo = Mock(status_code=200)
        repo.json.return_value = {"id": 123, "full_name": "fixture/site", "default_branch": "main", "permissions": {"push": True}}
        commit = Mock(status_code=200)
        commit.json.return_value = {"sha": website.verified_sha}
        with ExitStack() as stack:
            # All seams below load/store records, call providers, or contact the
            # cache. The frozen context, journey loader/projector, dispatcher,
            # request builder and guarded owner view execute their real code.
            stack.enter_context(patch.object(get_user_model().objects, "get", return_value=user))
            lookup = stack.enter_context(patch.object(views, "get_founder_company_context", return_value=context))
            stack.enter_context(patch.object(views, "_get_config", return_value=config))
            stack.enter_context(patch.object(prepare.transaction, "atomic", side_effect=lambda *a, **kw: nullcontext()))
            stack.enter_context(patch.object(prepare.WebsiteConnectionOperation.objects, "select_for_update", return_value=self.rows(first=op)))
            persistence = stack.enter_context(patch.object(prepare.WebsiteConnectionOperation.objects, "filter", return_value=self.rows()))
            for relation in ("targets", "scan_snapshots", "operations", "repository_mutations"):
                stack.enter_context(patch.object(WebsiteConnection, relation, self.rows(first=workflow) if relation == "operations" else self.rows()))
            stack.enter_context(patch.object(WebsiteConnection.objects, "select_related", return_value=self.rows(first=website)))
            stack.enter_context(patch.object(WebsiteConnection.objects, "select_for_update", return_value=self.rows(first=website)))
            stack.enter_context(patch.object(Organization.objects, "select_for_update", return_value=self.rows(first=organization)))
            stack.enter_context(patch.object(OrganizationContentConfig.objects, "filter", return_value=self.rows(exists=True)))
            stack.enter_context(patch.object(ContentFactoryRun.objects, "filter", return_value=self.rows()))
            stack.enter_context(patch.object(GitHubInstallation.objects, "filter", return_value=self.rows()))
            stack.enter_context(patch.object(github_app, "probe_installation_liveness", return_value=github_app.INSTALLATION_LIVE))
            stack.enter_context(patch.object(github_app, "create_installation_access_token", return_value=SimpleNamespace(token="synthetic")))
            stack.enter_context(patch.object(github_app, "require_installation_repository_permissions", return_value={"contents": "write", "pull_requests": "write"}))
            stack.enter_context(patch.object(github_app, "_require_installation_profile_permissions", return_value={"workflows": "write", "checks": "read", "statuses": "read"}))
            stack.enter_context(patch.object(views.http_client, "get", side_effect=[repo, commit]))
            stack.enter_context(patch.object(views.http_client, "delete"))
            for method, result in (("get", None), ("set", None), ("add", True), ("delete", None)):
                stack.enter_context(patch.object(views.cache, method, return_value=result))
            stack.enter_context(patch.object(prepare.WebsiteConnectionOperation.objects, "get_or_create", side_effect=reserve))
            stack.enter_context(patch.object(ContentFactoryRun.objects, "get_or_create", side_effect=store))
            stack.enter_context(patch.object(ContentFactoryRun, "save"))
            stack.enter_context(patch.object(OrganizationContentConfig, "refresh_from_db"))
            from .models import ContentFactoryJob
            stack.enter_context(patch.object(ContentFactoryJob.objects, "filter", return_value=self.rows()))
            queue = stack.enter_context(patch.object(views.http_client, "post", return_value=accepted))
            result = prepare.advance_prepare(op.pk)

        self.assertIs(result, op)
        self.assertEqual(op.state, "pending", op.receipt)
        self.assertEqual(op.receipt.get("child_run_id"), "scan-child")
        self.assertEqual(op.receipt.get("current_step"), "scan")
        queue.assert_called_once()
        self.assertEqual(queue.call_args.kwargs["json"]["slack_user_id"], "mlai_user:4")
        self.assertEqual(captured["run_request"]["operation_id"], str(workflow.pk))
        self.assertEqual(op.receipt["goal"]["milestones"][0]["state"], "passed")
        self.assertEqual(op.receipt["goal"]["milestones"][1]["state"], "pending")
        self.assertGreaterEqual(lookup.call_count, 3)
        self.assertTrue(all(call.args[0] is user for call in lookup.call_args_list))
        self.assertNotIn("prepare_user", context.__dict__)
        with self.assertRaises(FrozenInstanceError):
            context.company = SimpleNamespace(pk=10)
        self.assertEqual(persistence.call_args.kwargs["connection__generation"], website.generation)

