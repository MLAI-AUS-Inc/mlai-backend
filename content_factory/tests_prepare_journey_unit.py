"""Replay preparation against the canonical journey, without a database."""
from contextlib import nullcontext, ExitStack
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4

from django.test import SimpleTestCase

from . import website_prepare as prepare
from . import website_operations as operations
from .website_journey import project_journey
from . import tests_one_click_operations_unit as fixtures


class PrepareJourneyReplayTests(SimpleTestCase):
    def setUp(self):
        self.op = fixtures.PrepareJourneyTests().operation()
        self.op.connection.configuration_version = 2
        self.op.connection.github_repo = "fixture/site"
        self.op.connection.repository_id = 123
        self.op.connection.installation_id = "4"
        self.op.connection.app_root = ""
        self.op.connection.branch = "main"
        self.op.connection.organization = SimpleNamespace(domain="fixture.test")
        self.config = SimpleNamespace(website_connection=self.op.connection, website_connection_id="website", article_system={})
        self.context = SimpleNamespace(company=SimpleNamespace(pk=9), organization=SimpleNamespace(pk=7), prepare_user=SimpleNamespace(pk=4))
        self.facts = {"access": True, "repository": 123, "inventory": True, "write": True,
            "resolution": {"existence_state": "confirmed", "listing_surface": {"metadata": {"route_path": "/stories"}}}}

    def journey(self, *args, capabilities=None, **kwargs):
        caps = capabilities or {"repositoryAccessVerified": self.facts["access"]}
        return project_journey(company_id=9, domain="fixture.test", website={"status": "connected", "repositoryId": self.facts["repository"],
            "connectionGeneration": 3, "writePolicy": {"allowed": self.facts["write"]}, "verifiedSha": "current"},
            capabilities=caps, discovery={"complete": self.facts["inventory"], "sourceSha": "current", "resolution": self.facts["resolution"]})

    def advance(self, handlers=None, child=None):
        with ExitStack() as stack:
            stack.enter_context(patch.object(prepare, "_claim_prepare", return_value=self.op))
            stack.enter_context(patch.object(prepare, "_prepare_context", return_value=(self.context, self.config)))
            stack.enter_context(patch.object(prepare, "journey_for_context", side_effect=self.journey))
            stack.enter_context(patch.object(prepare, "_persist_prepare", side_effect=lambda op, **kw: op))
            if child is not None:
                rows = stack.enter_context(patch("workflow_runs.models.ContentFactoryRun.objects.filter"))
                rows.return_value.first.return_value = child
            return prepare.advance_prepare(self.op.pk, handlers=handlers)

    def setup_handler(self, captured):
        def handler(op, context, config):
            captured.append(prepare._owner_request(op, context, config, "setup", journey=self.journey()).data)
            return {"run_id": "setup-child"}
        return handler

    def test_talathrive_stale_scan_then_existing_stories_setup(self):
        captured = []
        self.facts["inventory"] = False
        self.advance({"scan": lambda *args: {"run_id": "scan-child"}})
        self.assertEqual(self.op.receipt["child_run_id"], "scan-child")
        self.facts["inventory"] = True
        self.advance({"setup": self.setup_handler(captured)}, SimpleNamespace(status="completed", result={}, workflow="repo_scan"))
        self.assertEqual((captured[0]["article_surface_mode"], captured[0]["article_surface_url"]), ("existing", "/stories"))

    def test_vyavos_existing_articles_setup(self):
        self.facts["resolution"]["listing_surface"] = {"route": "/articles"}
        captured = []
        self.advance({"setup": self.setup_handler(captured)})
        self.assertEqual((captured[0]["article_surface_mode"], captured[0]["article_surface_url"]), ("existing", "/articles"))

    def test_no_blog_creates_articles(self):
        self.facts["resolution"] = {"existence_state": "missing"}
        captured = []
        self.advance({"setup": self.setup_handler(captured)})
        self.assertEqual((captured[0]["article_surface_mode"], captured[0]["article_surface_url"]), ("none", "/articles"))

    def test_verified_access_with_incomplete_repository_stops_after_repeat(self):
        self.facts["repository"] = None
        handler = Mock(return_value={"status": "completed"})
        self.advance({"verify-access": handler})
        self.advance({"verify-access": handler})
        self.assertEqual(self.op.state, "needs_attention")
        self.assertEqual(self.op.receipt["reason_code"], "prepare_no_progress")
        handler.assert_called_once()

    def test_no_allowed_action_records_blocked_step_and_reason(self):
        self.facts["write"] = False
        self.advance()
        self.assertEqual(self.op.receipt["current_step"], "integration")
        self.assertEqual(self.op.receipt["blocked_step"], "integration")
        self.assertEqual(self.op.receipt["message"], "Prepare and verify your articles integration.")
        self.assertNotIn(None, self.op.receipt["step_failures"])

    def test_non_retryable_setup_child_is_not_dispatched_again(self):
        self.op.receipt = {"child_run_id": "failed-child", "current_step": "setup"}
        child = SimpleNamespace(status="blocked", result={"error_code": "INVALID_ADAPTER", "retryable": False},
            workflow="article_system_setup", error="The adapter is invalid.")
        handler = Mock()
        self.advance({"setup": handler}, child)
        self.assertEqual(self.op.state, "needs_attention")
        self.assertEqual(self.op.receipt["message"], child.error)
        self.assertEqual(self.op.receipt["user_action"]["run_id"], "failed-child")
        handler.assert_not_called()

    def test_cancelled_prepare_reopens_at_attempt_two(self):
        self.op.pk = uuid4()
        self.op.connection.organization = SimpleNamespace(domain="fixture.test")
        self.op.connection.blockers = []
        self.op.connection.operations = Mock()
        self.op.connection.repository_mutations = Mock()
        self.op.connection.repository_mutations.filter.return_value.values.return_value = []
        self.op.connection.operations.select_for_update.return_value.filter.return_value.first.return_value = self.op
        self.config.organization = self.op.connection.organization
        self.config.github_repo = self.op.connection.github_repo
        with patch("content_factory.website_connections.authority_guard", return_value=nullcontext(self.op.connection)), \
                patch("workflow_runs.models.ContentFactoryRun.objects.filter") as rows, \
                patch.object(operations.WebsiteConnectionOperation.objects, "get_or_create", return_value=(Mock(), True)):
            rows.return_value.first.return_value = None
            rows.return_value.values_list.return_value = []
            operations.cancel_operation(self.config, data={"website_connection_id": str(uuid4()), "connection_generation": 3,
                "operation_id": str(self.op.pk)}, idempotency_key="cancel")
        self.assertEqual(self.op.state, "cancelled")
        self.assertEqual(self.op.receipt["status"], "cancelled_by_user")
        self.assertIsNone(self.op.next_attempt_at)
        with patch.object(prepare, "authority_guard", return_value=nullcontext(self.op.connection)), \
                patch.object(prepare.WebsiteConnectionOperation.objects, "get_or_create", return_value=(self.op, False)):
            prepare.start_prepare(self.config, expected={}, retry=True)
        self.assertEqual(self.op.state, "pending")
        self.assertEqual(self.op.payload["attempt"], 2)

    def test_check_again_keeps_the_setup_child_awaiting_merge(self):
        self.op.state = "needs_user"
        self.op.receipt = {"current_step": "merge", "child_run_id": "existing-pr-child"}
        with patch.object(prepare, "authority_guard", return_value=nullcontext(self.op.connection)), \
                patch.object(prepare.WebsiteConnectionOperation.objects, "get_or_create", return_value=(self.op, False)):
            prepare.start_prepare(self.config, expected={}, retry=True)
        self.assertEqual(self.op.receipt["child_run_id"], "existing-pr-child")
        self.assertEqual(self.op.state, "pending")

    def test_ambiguous_routes_require_owner_choice(self):
        self.facts["resolution"] = {"existence_state": "ambiguous", "ranked_candidates": {"listing_surface_candidates": [
            {"route": "/stories"}, {"metadata": {"route_path": "/news"}}]}}
        self.advance({"setup": self.setup_handler([])})
        self.assertEqual(self.op.state, "needs_user")
        self.assertEqual(self.op.receipt["user_action"], {"id": "choose_articles_page", "candidates": ["/stories", "/news"]})

    def test_saved_setup_choice_wins(self):
        self.config.article_system = {"pending_article_system_setup": {"mode": "existing", "route_path": "/journal"}}
        self.assertEqual(prepare._setup_surface(self.config, self.journey()), ("existing", "/journal"))

    def test_forced_access_advances_scan_in_the_same_pass(self):
        self.facts["access"] = False
        self.facts["inventory"] = False
        handler = Mock(return_value={"run_id": "scan-child"})
        with patch("content_factory.vibe_marketing_views._article_capabilities_for_context", return_value={"repositoryAccessVerified": True}):
            self.advance({"scan": handler})
        handler.assert_called_once()
        self.assertEqual(self.op.receipt["current_step"], "scan")

    def test_internal_post_reaches_real_guarded_scan_and_setup_views(self):
        from . import vibe_marketing_views as views
        self.op.connection.pk = uuid4()
        self.config.website_connection_id = self.op.connection.pk
        self.config.github_repo = "fixture/site"
        self.config.organization = self.op.connection.organization
        self.config.save = Mock()
        self.config.refresh_from_db = Mock()
        self.context.profile = SimpleNamespace(user=self.context.prepare_user, companies=Mock())
        self.context.organization.domain = "fixture.test"
        self.context.company.domain = "fixture.test"
        run = SimpleNamespace(run_id="queued-child", status="queued", result={}, save=Mock())
        with ExitStack() as stack:
            for name, result in {
                "get_founder_company_context": self.context, "_get_config": self.config,
                "_github_repo_company_conflict_response": None, "_require_roo_points_for_ai_agent": (None, 0),
                "_supersede_stale_scan_runs": [], "founder_actor_id_for_user": "actor",
                "analytics_config_for_content_factory": {}, "analytics_article_manifest": [],
                "_article_system_setup_run_is_dispatch_blocked": False,
            }.items():
                stack.enter_context(patch.object(views, name, return_value=result))
            stack.enter_context(patch.object(views, "owner_write_guard", side_effect=lambda *a, **kw: nullcontext()))
            stack.enter_context(patch("content_factory.website_views.authority_guard", side_effect=lambda *a, **kw: nullcontext(self.op.connection)))
            stack.enter_context(patch("content_factory.website_views.owner_operation_scope", side_effect=lambda *a, **kw: nullcontext()))
            queue = stack.enter_context(patch.object(views, "_queue_content_factory_run", return_value=run))
            for step, view in (("scan", views.VibeMarketingScanView), ("setup", views.VibeMarketingArticleSystemSetupView)):
                request = prepare._owner_request(self.op, self.context, self.config, step, journey=self.journey())
                self.assertEqual(request.method, "POST")
                self.assertEqual(request.query_params, {"company_id": "9"})
                self.assertEqual(request.META, {})
                response = view().post(request)
                self.assertEqual(response.status_code, 202, response.data)
            payload = queue.call_args.kwargs["payload"]
        self.assertEqual(payload["article_surface_mode"], "existing")
        self.assertEqual(payload["article_surface_hint"]["route_path"], "/stories")
        self.assertEqual(self.config.article_system["pending_article_system_setup"]["route_path"], "/stories")
