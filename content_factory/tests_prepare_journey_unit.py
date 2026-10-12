"""Replay preparation against the canonical journey, without a database."""
from contextlib import nullcontext, ExitStack
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch
from uuid import uuid4

from django.test import SimpleTestCase

from . import website_prepare as prepare
from . import website_operations as operations
from .website_journey import project_journey
from .website_connections import contract_for
from . import tests_one_click_operations_unit as fixtures


class FrozenPrepareContextTests(SimpleTestCase):
    @staticmethod
    def rows(*, first=None, exists=False):
        """Replace persistence access while leaving preparation/projection real."""
        rows = MagicMock()
        for method in ("filter", "exclude", "order_by", "select_related", "select_for_update", "prefetch_related", "values_list"):
            getattr(rows, method).return_value = rows
        rows.first.return_value = first
        rows.get.return_value = first
        rows.exists.return_value = exists
        rows.__iter__.side_effect = lambda: iter(())
        rows.__getitem__.return_value = []
        rows.update.return_value = 1
        return rows

    def test_real_frozen_context_reaches_guarded_owner_dispatch(self):
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
        worker_run = SimpleNamespace(run_id="scan-child", status="queued", result={}, save=Mock())
        repo = Mock(status_code=200)
        repo.json.return_value = {"full_name": "fixture/site", "default_branch": "main", "permissions": {"push": True}}
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
                stack.enter_context(patch.object(WebsiteConnection, relation, self.rows()))
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
            queue = stack.enter_context(patch.object(views, "_queue_content_factory_run", return_value=worker_run))
            result = prepare.advance_prepare(op.pk)

        self.assertIs(result, op)
        self.assertEqual(op.state, "pending")
        self.assertEqual(op.receipt.get("child_run_id"), "scan-child")
        self.assertEqual(op.receipt.get("current_step"), "scan")
        queue.assert_called_once()
        self.assertIs(queue.call_args.kwargs["context"], context)
        self.assertEqual(queue.call_args.kwargs["payload"]["slack_user_id"], "mlai_user:4")
        self.assertGreaterEqual(lookup.call_count, 3)
        self.assertTrue(all(call.args[0] is user for call in lookup.call_args_list))
        self.assertNotIn("prepare_user", context.__dict__)
        with self.assertRaises(FrozenInstanceError):
            context.company = SimpleNamespace(pk=10)
        self.assertEqual(persistence.call_args.kwargs["connection__generation"], website.generation)


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
        self.user = SimpleNamespace(pk=4)
        self.context = SimpleNamespace(company=SimpleNamespace(pk=9), organization=SimpleNamespace(pk=7))
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
            stack.enter_context(patch.object(prepare, "_prepare_context", return_value=(self.context, self.config, self.user)))
            stack.enter_context(patch.object(prepare, "journey_for_context", side_effect=self.journey))
            stack.enter_context(patch.object(prepare, "_persist_prepare", side_effect=lambda op, **kw: op))
            if child is not None:
                rows = stack.enter_context(patch("workflow_runs.models.ContentFactoryRun.objects.filter"))
                rows.return_value.first.return_value = child
            return prepare.advance_prepare(self.op.pk, handlers=handlers)

    def setup_handler(self, captured):
        def handler(op, context, config, *, user):
            captured.append(prepare._owner_request(op, context, config, "setup", user=user, journey=self.journey()).data)
            return {"run_id": "setup-child"}
        return handler

    def test_talathrive_stale_scan_then_existing_stories_setup(self):
        captured = []
        self.facts["inventory"] = False
        self.advance({"scan": lambda *args, **kwargs: {"run_id": "scan-child"}})
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
        self.context.profile = SimpleNamespace(user=self.user, companies=Mock())
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
                request = prepare._owner_request(self.op, self.context, self.config, step, user=self.user, journey=self.journey())
                self.assertEqual(request.method, "POST")
                self.assertEqual(request.query_params, {"company_id": "9"})
                self.assertEqual(request.META, {})
                response = view().post(request)
                self.assertEqual(response.status_code, 202, response.data)
            payload = queue.call_args.kwargs["payload"]
        self.assertEqual(payload["article_surface_mode"], "existing")
        self.assertEqual(payload["article_surface_hint"]["route_path"], "/stories")
        self.assertEqual(self.config.article_system["pending_article_system_setup"]["route_path"], "/stories")

    def _queue_ambiguous_choice(self, *, blocked=False):
        from . import vibe_marketing_views as views
        self.op.connection.pk = uuid4()
        self.op.connection_id = self.config.website_connection_id = self.op.connection.pk
        self.op.connection.operations = Mock()
        self.op.connection.operations.select_for_update.return_value.filter.return_value.first.return_value = self.op
        self.config.github_repo = "fixture/site"
        self.config.organization = self.op.connection.organization
        self.config.requires_review = True
        self.config.save = self.config.refresh_from_db = Mock()
        self.context.profile = SimpleNamespace(user=self.user, companies=Mock())
        self.context.organization.domain = self.context.company.domain = "fixture.test"
        self.op.receipt = {"scan_run_id": "original-scan", "current_step": "setup"}
        self.facts["resolution"] = {"existence_state": "ambiguous", "ranked_candidates": {"listing_surface_candidates": [
            {"route": "/stories"}, {"route": "/news"}]}}
        self.advance({"setup": self.setup_handler([])})
        self.assertEqual(self.op.state, "needs_user")
        run = SimpleNamespace(run_id="direct-choice-child", status="blocked" if blocked else "queued", workflow="article_system_setup", error="Invalid selected-page adapter." if blocked else "",
            organization_id=7, domain="fixture.test", run_request={**contract_for(self.op.connection),
                "operation_id": str(uuid4()), "operation_attempt": 1, "deletion_epoch": 0}, result={"error_code": "INVALID_ADAPTER", "retryable": False} if blocked else {}, save=Mock())
        request = SimpleNamespace(method="POST", META={}, user=self.user,
            query_params={"company_id": "9"}, headers={"Idempotency-Key": "direct-choice"}, data={
                **contract_for(self.op.connection), "company_id": "9", "article_surface_mode": "existing", "article_surface_url": "/stories"})
        def queue_child(**kwargs):
            run.run_request.update(kwargs["payload"])
            # A worker progress callback can commit before queue HTTP returns.
            with patch.object(prepare.WebsiteConnectionOperation.objects, "filter") as rows:
                rows.return_value.update.side_effect = lambda **values: setattr(self.op, "state", values["state"])
                rows.return_value.count.return_value = 1
                prepare.advance_prepares_for_connection(self.op.connection_id)
            premature_dispatch = Mock(return_value={"run_id": "duplicate-child"})
            self.advance({"setup": premature_dispatch})
            premature_dispatch.assert_not_called()
            return run
        with ExitStack() as stack:
            for name, value in {"get_founder_company_context": self.context, "_get_config": self.config,
                "_github_repo_company_conflict_response": None, "_require_roo_points_for_ai_agent": (None, 0),
                "founder_actor_id_for_user": "actor", "analytics_config_for_content_factory": {},
                "analytics_article_manifest": [], "_article_system_setup_run_is_dispatch_blocked": blocked, "_run_start_payload": {"run_id": run.run_id, "status": run.status}}.items():
                stack.enter_context(patch.object(views, name, return_value=value))
            stack.enter_context(patch.object(views, "owner_write_guard", side_effect=lambda *a, **kw: nullcontext()))
            stack.enter_context(patch.object(prepare, "authority_guard", side_effect=lambda *a, **kw: nullcontext(self.op.connection)))
            stack.enter_context(patch("content_factory.website_views.authority_guard", side_effect=lambda *a, **kw: nullcontext(self.op.connection)))
            stack.enter_context(patch("content_factory.website_views.owner_operation_scope", side_effect=lambda *a, **kw: nullcontext()))
            rows = stack.enter_context(patch("workflow_runs.models.ContentFactoryRun.objects.filter"))
            rows.return_value.first.return_value = run
            queue = stack.enter_context(patch.object(views, "_queue_content_factory_run", side_effect=queue_child))
            response = views.VibeMarketingArticleSystemSetupView().post(request)
        self.assertEqual(response.status_code, 503 if blocked else 202, response.data)
        self.assertEqual(self.op.receipt.get("child_run_id"), run.run_id)
        self.assertEqual(self.op.receipt["current_step"], "setup")
        self.assertIsNone(self.op.receipt["user_action"])
        queue.assert_called_once()
        return run, queue

    def test_ambiguous_choice_keeps_one_direct_setup_child_through_review_and_merge(self):
        run, queue = self._queue_ambiguous_choice()
        self.advance(child=run)
        self.assertEqual(self.op.receipt["child_run_id"], run.run_id)
        run.status = "awaiting_approval"
        run.result = {"article_system_setup": {"status": "preview_ready", "pr_number": 42}, "pr_number": 42}
        self.advance(child=run)
        self.assertEqual(self.op.state, "needs_user")
        self.assertEqual(self.op.receipt["user_action"]["id"], "merge_pr")
        self.assertEqual(self.op.receipt["child_run_id"], run.run_id)
        run.status = "completed"
        run.result["article_system_setup"]["merge_status"] = "merged"
        self.journey = lambda *a, **kw: project_journey(company_id=9, domain="fixture.test", website={"status": "connected", "repositoryId": 123,
            "writePolicy": {"allowed": True}}, capabilities={"repositoryAccessVerified": True, "canGenerateArticle": True}, discovery={"complete": True})
        verify = Mock(return_value={"status": "completed"})
        self.advance({"verify": verify}, child=run)
        verify.assert_called_once()
        self.assertNotIn("child_run_id", self.op.receipt)
        queue.assert_called_once()

    def test_blocked_direct_choice_reports_its_child_failure_without_new_setup(self):
        run, queue = self._queue_ambiguous_choice(blocked=True)
        self.advance(child=run)
        self.assertEqual(self.op.state, "needs_attention")
        self.assertEqual(self.op.receipt["reason_code"], "INVALID_ADAPTER")
        self.assertEqual(self.op.receipt["user_action"]["run_id"], run.run_id)
        queue.assert_called_once()

    def test_woken_prepare_adopts_saved_direct_child_before_any_setup_dispatch(self):
        self.op.connection.pk = uuid4()
        self.op.connection_id = self.config.website_connection_id = self.op.connection.pk
        self.op.receipt = {"current_step": "setup", "reason_code": "articles_page_choice_required",
            "user_action": {"id": "choose_articles_page", "candidates": ["/stories", "/news"]}}
        self.config.article_system = {"pending_article_system_setup": {"mode": "existing", "route_path": "/stories",
            "setup_run_id": "saved-direct-child", "status": "queued"}}
        child = SimpleNamespace(run_id="saved-direct-child", status="queued", workflow="article_system_setup", domain="fixture.test",
            organization_id=7, run_request={**contract_for(self.op.connection), "article_surface_hint": {"route_path": "/stories"}}, result={})
        dispatch = Mock(return_value={"run_id": "duplicate-child"})
        self.advance({"setup": dispatch}, child=child)
        self.assertEqual(self.op.receipt["child_run_id"], child.run_id)
        dispatch.assert_not_called()

    def test_saved_direct_child_cannot_borrow_another_scope(self):
        self.op.connection.pk = uuid4()
        self.op.connection_id = self.config.website_connection_id = self.op.connection.pk
        self.op.receipt = {"user_action": {"id": "choose_articles_page", "candidates": ["/stories"]}}
        self.config.article_system = {"pending_article_system_setup": {"mode": "existing", "route_path": "/stories", "setup_run_id": "child"}}
        valid = dict(run_id="child", status="queued", workflow="article_system_setup", domain="fixture.test", organization_id=7,
            run_request={**contract_for(self.op.connection), "article_surface_hint": {"route_path": "/stories"}}, result={})
        for changed in ({"organization_id": 8}, {"domain": "other.test"}, {"workflow": "article_generation"},
                {"run_request": {**valid["run_request"], "connection_generation": 2}},
                {"run_request": {**valid["run_request"], "repository_id": 456}},
                {"run_request": {**valid["run_request"], "website_connection_id": str(uuid4())}},
                {"run_request": {**valid["run_request"], "domain": "other.test"}}):
            with self.subTest(changed=changed), patch("workflow_runs.models.ContentFactoryRun.objects.filter") as rows:
                rows.return_value.first.return_value = SimpleNamespace(**{**valid, **changed})
                self.assertIsNone(prepare._selected_setup_child(self.op, self.config))
