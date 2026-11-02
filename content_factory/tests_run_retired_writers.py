"""Every retained poll/cancel/denial path leaves owned lifecycle to typed CF history."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock, patch
from django.test import TransactionTestCase, override_settings
from workflow_runs.models import ContentFactoryRun, ContentFactoryRunStep
from .run_ownership import (WIRE_REQUEST_KEY, WIRE_EXECUTION_KEY, bind_admitted_run, current_execution_ticket,
    reserve_admitted_run, user_principal, wire_identity, owner_receipt)
from .self_heal_testing import make_p2_org
from .tests_run_event_authority import FLAGS
from . import vibe_marketing_views as views


@override_settings(**FLAGS, CONTENT_FACTORY_URL="https://cf.fixture.invalid", CONTENT_FACTORY_API_KEY="synthetic-cancel-key",
                   ROO_API_KEY="synthetic-retired-service-key")
class RetiredRunWriterTests(TransactionTestCase):
    def setUp(self):
        fixture = make_p2_org()
        self.org, self.user, self.config = fixture.organization, fixture.owner, fixture.config
        self.actor = f"mlai_user:{self.user.pk}"
        raw = {"domain": self.org.domain, "client_request_id": "retired-original", "topic": "Retained original",
            "delivery_mode": "content_only", "delivery_mode_confirmed": True}
        self.run = bind_admitted_run(reserve_admitted_run(organization=self.org,
            principal=user_principal(self.user.pk, self.actor), workflow="article_generation", payload=raw), "retired-worker")
        ContentFactoryRunStep.objects.create(run=self.run, step_key="write", status="running", attempts=1)
        self.context = SimpleNamespace(organization=self.org)

    def snapshot(self, run=None):
        run = run or self.run
        return (ContentFactoryRun.objects.values().get(pk=run.pk), list(run.steps.values()))

    def test_all_actual_local_cancel_functions_leave_owned_run_steps_approval_and_clocks_unchanged(self):
        before = self.snapshot()
        for cancel in (views._cancel_local_article_run, views._cancel_local_article_system_setup_run, views._cancel_local_scan_run):
            with self.subTest(cancel=cancel.__name__):
                kwargs = {"run": self.run, "remote_data": {"status": "cancelled", "cancelled": True,
                    "approval_state": "approved", "result": {"delivery_v1": {"forged": True}}}}
                if cancel != views._cancel_local_scan_run:
                    kwargs["organization"] = self.org
                with patch("content_factory.first_article_entitlement.settle_owner_cancel") as settle:
                    cancel(**kwargs)
                settle.assert_not_called()
                self.assertEqual(self.snapshot(), before)

    def test_actual_snapshot_poll_and_direct_step_adapters_only_read_typed_history(self):
        before = self.snapshot()
        remote = {"run_id": self.run.run_id, "status": "completed", "approval_state": "approved",
            "run_request": {"topic": "replacement"}, "result": {"draft_ready_v1": {"version": 1}},
            "steps": {"write": {"status": "completed", "attempts": 9}}}
        from .run_artifact_observations import ArtifactObservationInvalid
        with patch("content_factory.run_reconciliation.reconcile_authoritative_run", return_value={"outcome": "pending"}) as history, \
                patch("content_factory.run_verified_delivery.project_verified_delivery", side_effect=ArtifactObservationInvalid("pending")) as artifacts, \
                patch.object(views.http_client, "get") as old_status:
            views._sync_local_run_from_remote(self.run, remote)
            views._sync_local_run_from_remote_locked(self.run, remote)
            views._sync_steps_from_remote(self.run, remote)
            observed = views._call_content_factory_run_status(self.run.run_id, workflow=self.run.workflow)
        self.assertEqual(history.call_count, 2)
        self.assertEqual(artifacts.call_count, 2)
        old_status.assert_not_called()
        self.assertEqual(observed["status"], before[0]["status"])
        self.assertEqual(self.snapshot(), before)

    def test_actual_cancel_deny_and_approve_transport_echo_current_ticket_without_trusting_browser_scope(self):
        response = Mock(status_code=202, content=b"{}")
        response.json.return_value = {"run_id": self.run.run_id, "status": "cancelled"}
        before = self.snapshot()
        for action in ("cancel", "deny"):
            with patch.object(views.http_client, "post", return_value=response) as post:
                views._call_content_factory_run_action(run_id=self.run.run_id, action=action,
                    payload={"source_run_id": "browser-forgery", "expected_source_sha": "f" * 40,
                        "website_connection_id": "foreign"}, workflow=self.run.workflow)
            sent = post.call_args.kwargs["json"]
            self.assertEqual(sent[WIRE_REQUEST_KEY], wire_identity(owner_receipt(self.run)))
            self.assertEqual(sent[WIRE_EXECUTION_KEY], current_execution_ticket(self.run))
            self.assertNotIn("source_run_id", sent)
            self.assertNotIn("website_connection_id", sent)
            self.assertEqual(self.snapshot(), before)

        from .website_models import WebsiteConnection
        from .website_connections import contract_for
        website = WebsiteConnection.objects.create(organization=self.org, authorized_by=self.user,
            github_repo="synthetic/setup", repository_id=42, installation_id="1001", branch="main", verified_sha="a" * 40)
        self.config.website_connection, self.config.github_repo = website, website.github_repo
        self.config.save(update_fields=["website_connection", "github_repo"])
        setup = bind_admitted_run(reserve_admitted_run(organization=self.org,
            principal=user_principal(self.user.pk, self.actor), workflow="article_system_setup",
            payload={**contract_for(website), "domain": self.org.domain, "github_repo": website.github_repo,
                "client_request_id": "approve-original", "expected_source_sha": "a" * 40}), "approve-worker")
        provider = Mock(status_code=200)
        provider.json.return_value = {"id": 42, "full_name": website.github_repo, "default_branch": "main", "sha": "a" * 40}
        with patch.object(views.http_client, "post", return_value=response) as post, \
                patch("integrations.services.github_connections.read_bound_repository", return_value=provider):
            views._call_content_factory_run_action(run_id=setup.run_id, action="approve", payload={}, workflow=setup.workflow)
        self.assertIsNotNone(post.call_args)
        sent = post.call_args.kwargs["json"]
        self.assertEqual(sent[WIRE_REQUEST_KEY], wire_identity(owner_receipt(setup)))
        self.assertEqual(sent[WIRE_EXECUTION_KEY], current_execution_ticket(setup))

    def test_owned_island_cancellation_and_status_refund_never_infer_money_or_lifecycle(self):
        from .island_research_cancel import cancel_island_research
        from .island_research import refund_empty_or_failed_research
        raw = {"domain": self.org.domain, "client_request_id": "retired-research-original",
            "island_research_brief": {"subject": "Original retained brief"}}
        research = bind_admitted_run(reserve_admitted_run(organization=self.org,
            principal=user_principal(self.user.pk, self.actor), workflow="island_refresh", payload=raw), "retired-research-worker")
        before = self.snapshot(research)
        with patch.object(views, "_call_content_factory_run_action", return_value={"run_id": research.run_id, "status": "cancelled"}) as cancel, \
                patch("content_factory.run_reconciliation.reconcile_authoritative_run", return_value={"outcome": "pending"}), \
                patch("content_factory.run_verified_delivery.project_verified_delivery", return_value={"status": "observation_only"}), \
                patch.object(views, "_serialize_run", return_value={"status": research.status}), \
                patch("roo.services.PointsService.refund") as refund:
            response = cancel_island_research(run=research, context=self.context, payload={"slack_user_id": self.actor}, user=self.user)
            ContentFactoryRun.objects.filter(pk=research.pk).update(status="failed")
            research.refresh_from_db()
            refund_empty_or_failed_research(research)
        self.assertEqual(response.status_code, 202)
        self.assertEqual(cancel.call_args.kwargs["payload"][WIRE_EXECUTION_KEY], current_execution_ticket(research))
        refund.assert_not_called()
        after = self.snapshot(research)
        self.assertEqual({key: value for key, value in after[0].items() if key != "status"},
            {key: value for key, value in before[0].items() if key != "status"})
        self.assertEqual(after[1], before[1])

    def test_denied_legacy_callback_cannot_record_an_owned_failure(self):
        from .website_models import WebsiteConnection
        from .website_connections import contract_for, record_denied_terminal_callback
        from .website_contract import WebsiteAuthorityError
        website = WebsiteConnection.objects.create(organization=self.org, authorized_by=self.user,
            github_repo="synthetic/retained", repository_id=42, installation_id="1001", branch="main", verified_sha="a" * 40)
        raw = {**contract_for(website), "domain": self.org.domain, "github_repo": website.github_repo,
               "client_request_id": "denial-original"}
        run = bind_admitted_run(reserve_admitted_run(organization=self.org,
            principal=user_principal(self.user.pk, self.actor), workflow="article_generation", payload=raw), "denial-worker")
        before = self.snapshot(run)
        denial = WebsiteAuthorityError("website_source_changed", "Refresh this source.", retryable=False)
        self.assertFalse(record_denied_terminal_callback({**contract_for(website), "run_id": run.run_id,
            "event": "generation_failed", "error_code": "WORKER_BAD", "status": "failed"}, denial))
        self.assertEqual(self.snapshot(run), before)

    def test_actual_app_non_scan_approve_forwards_current_echoes_and_does_not_apply_ack_snapshot(self):
        from integrations import api_views_content_factory_app as app_views
        from rest_framework.test import APIRequestFactory, force_authenticate
        response = Mock(status_code=202, content=b"{}")
        response.json.return_value = {"run_id": self.run.run_id, "status": "completed", "approval_state": "approved"}
        request = APIRequestFactory().post("/approve", {}, format="json")
        force_authenticate(request, user=self.user)
        before = self.snapshot()
        with patch.object(app_views, "_content_factory_request", return_value=response) as post:
            result = app_views.ContentFactoryAppRunControlView.as_view()(request, run_id=self.run.run_id, action="approve")
        self.assertEqual(result.status_code, 200, result.data)
        sent = post.call_args.kwargs["payload"]
        self.assertEqual(sent[WIRE_REQUEST_KEY], wire_identity(owner_receipt(self.run)))
        self.assertEqual(sent[WIRE_EXECUTION_KEY], current_execution_ticket(self.run))
        self.assertEqual(self.snapshot(), before)

    def test_actual_service_write_decorator_refuses_owned_before_handler_or_original_request_rewrite(self):
        from .website_connections import guarded_service_write
        calls = []

        @guarded_service_write("config_write", only_repository=True)
        def worker_write(view, request):
            calls.append("effect")
            raise AssertionError("An owned worker must not reach a legacy handler")

        before = self.snapshot()
        request = SimpleNamespace(method="POST", data={"run_id": self.run.run_id,
            "status": "completed", "run_request": {"topic": "worker replacement"}})
        response = worker_write(SimpleNamespace(), request)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(calls, [])
        self.assertEqual(self.snapshot(), before)

    def test_actual_old_service_controls_refuse_owned_before_approval_resume_or_publish_effects(self):
        from .service_views import ContentFactoryRunControlView
        from rest_framework.test import APIRequestFactory
        before = self.snapshot()
        for action in ("approve", "deny", "resume", "promote-bundle", "publish-pr"):
            request = APIRequestFactory().post("/old-service-control", {"actor": self.actor}, format="json",
                HTTP_X_API_KEY="synthetic-retired-service-key")
            with self.subTest(action=action), \
                    patch("integrations.services.article_generation.publish_article_as_pr") as publish, \
                    patch("content_factory.website_connections.authority_guard") as old_guard:
                response = ContentFactoryRunControlView.as_view()(request, run_id=self.run.run_id, action=action)
                self.assertEqual(response.status_code, 409)
                publish.assert_not_called()
                old_guard.assert_not_called()
                self.assertEqual(self.snapshot(), before)

    def test_pending_user_denial_blocks_actual_provider_and_worker_reauthorization_before_effects(self):
        from .website_models import WebsiteConnection
        from . import website_connections as connections
        from .website_views import WebsiteConnectionAuthorizeView
        from .run_user_stop import record_user_denial
        from .website_contract import WebsiteAuthorityError
        from rest_framework.test import APIRequestFactory
        website = WebsiteConnection.objects.create(organization=self.org, authorized_by=self.user,
            github_repo="synthetic/stopped", repository_id=42, installation_id="1001", branch="main", verified_sha="a" * 40)
        self.config.website_connection, self.config.github_repo = website, website.github_repo
        self.config.save(update_fields=["website_connection", "github_repo"])
        run = bind_admitted_run(reserve_admitted_run(organization=self.org,
            principal=user_principal(self.user.pk, self.actor), workflow="article_system_setup",
            payload={**connections.contract_for(website), "domain": self.org.domain, "github_repo": website.github_repo,
                "client_request_id": "pending-provider-stop", "expected_source_sha": "a" * 40}), "pending-provider-worker")
        record_user_denial(run, user=self.user, actor_id=self.actor)
        before = self.snapshot(run)
        binding = connections.scoped_run_contract(run)
        with patch("integrations.services.github_connections.read_bound_repository") as provider:
            with self.assertRaises(WebsiteAuthorityError) as denied:
                with connections.authority_guard(binding, action="setup"):
                    self.fail("Denied consent cannot enter a provider mutation scope")
            self.assertEqual(denied.exception.code, "run_stop_requested")
            marker = connections._backend_provider_scope.set((binding, "setup"))
            try:
                with self.assertRaises(WebsiteAuthorityError):
                    connections.authorize_backend_provider_mutation("PUT", "/contents/articles/example.md")
            finally:
                connections._backend_provider_scope.reset(marker)
            query = {**connections.contract_for(website), "run_id": run.run_id, "domain": run.domain,
                "github_repo": run.github_repo, "action": "read"}
            request = APIRequestFactory().get("/worker-authorize", query, HTTP_X_API_KEY="synthetic-retired-service-key")
            self.assertEqual(WebsiteConnectionAuthorizeView.as_view()(request).status_code, 409)
            provider.assert_not_called()
        self.assertEqual(self.snapshot(run), before)

    def test_denial_arriving_during_readonly_preflight_is_rechecked_before_effect_scope(self):
        from .website_models import WebsiteConnection
        from . import website_connections as connections
        from .run_user_stop import record_user_denial
        from .website_contract import WebsiteAuthorityError
        website = WebsiteConnection.objects.create(organization=self.org, authorized_by=self.user,
            github_repo="synthetic/raced", repository_id=42, installation_id="1001", branch="main", verified_sha="a" * 40)
        self.config.website_connection, self.config.github_repo = website, website.github_repo
        self.config.save(update_fields=["website_connection", "github_repo"])
        run = bind_admitted_run(reserve_admitted_run(organization=self.org,
            principal=user_principal(self.user.pk, self.actor), workflow="article_system_setup",
            payload={**connections.contract_for(website), "domain": self.org.domain, "github_repo": website.github_repo,
                "client_request_id": "raced-provider-stop", "expected_source_sha": "a" * 40}), "raced-provider-worker")
        response = Mock(status_code=200)
        response.json.return_value = {"id": 42, "full_name": website.github_repo, "default_branch": "main"}

        def readonly(*args, **kwargs):
            record_user_denial(run, user=self.user, actor_id=self.actor)
            return response

        with patch("integrations.services.github_connections.read_bound_repository", side_effect=readonly), \
                self.assertRaises(WebsiteAuthorityError) as denied:
            with connections.authority_guard(connections.scoped_run_contract(run), action="setup"):
                self.fail("An acknowledgement cannot outlive a denial arriving during preflight")
        self.assertEqual(denied.exception.code, "run_stop_requested")

    def test_old_v1_reconciler_cannot_claim_owned_transport_or_reconciliation_clock(self):
        from .setup_delivery_reconciliation import enrolled_run, _claim_pull
        from django.utils import timezone
        self.run.run_request["setup_delivery_v1"] = {"schema_version": 1}
        # A legacy marker is not an authority transfer, even when a test row
        # carries both protocols. No claim or HTTP is allowed for this owner.
        before = self.snapshot()
        self.assertFalse(enrolled_run(self.run))
        with patch("content_factory.setup_delivery_reconciliation._locked_context") as legacy_scope:
            self.assertIsNone(_claim_pull(self.run, timezone.now()))
        legacy_scope.assert_not_called()
        self.assertEqual(self.snapshot(), before)
