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
        self.company = fixture.company
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

    def test_direct_poll_repair_helpers_cannot_replace_owned_execution_state(self):
        from .tests_run_event_authority import batch
        from .run_event_authority import receive_authoritative_run_events
        self.assertEqual(receive_authoritative_run_events(batch(self.run,
            [{"status": "running"}, {"status": "blocked"}])).status_code, 200)
        self.run.refresh_from_db()
        before = self.snapshot()
        with patch.object(views, "_article_run_has_completed_local_artifacts", return_value=True) as artifacts:
            views._heal_stale_status_poll_timeout(self.run)
        artifacts.assert_not_called()
        self.assertEqual(self.snapshot(), before)
        raw = {"domain": self.org.domain, "client_request_id": "retired-autofill"}
        autofill = bind_admitted_run(reserve_admitted_run(organization=self.org,
            principal=user_principal(self.user.pk, self.actor), workflow="startup_autofill", payload=raw),
            "retired-autofill-worker")
        before = self.snapshot(autofill)
        views._block_startup_autofill_on_status_poll_unavailable(autofill, {"error": "Status timed out"})
        self.assertEqual(self.snapshot(autofill), before)

    def test_dispatch_ack_after_revoke_retains_owned_execution_and_queues_only_cleanup_intent(self):
        from .website_models import WebsiteConnection, WebsiteConnectionOperation
        from .website_connections import contract_for
        website = WebsiteConnection.objects.create(organization=self.org, authorized_by=self.user,
            github_repo="synthetic/retired-ack", repository_id=42, installation_id="1001", branch="main", verified_sha="a" * 40)
        operation = WebsiteConnectionOperation.objects.create(connection=website, generation=website.generation,
            action="workflow", idempotency_key="retired-late-operation", payload={"attempt": 1, "deletion_epoch": 0})
        raw = {**contract_for(website), "operation_id": str(operation.pk), "operation_attempt": 1, "deletion_epoch": 0, "domain": self.org.domain, "github_repo": website.github_repo,
            "client_request_id": "retired-late-ack"}
        run = bind_admitted_run(reserve_admitted_run(organization=self.org,
            principal=user_principal(self.user.pk, self.actor), workflow="article_generation", payload=raw), "retired-late-worker")
        before = self.snapshot(run)
        WebsiteConnection.objects.filter(pk=website.pk).update(state="revoked", generation=2)
        retained = views._create_local_run(workflow=run.workflow, domain=run.domain, github_repo=run.github_repo,
            actor_id=self.actor, payload=run.run_request, remote_data={"run_id": run.run_id, "status": "queued"})
        self.assertEqual(retained.pk, run.pk)
        self.assertEqual(self.snapshot(run), before)
        from .website_operations import bind_operation_run
        bind_operation_run(operation, run)
        self.assertEqual(self.snapshot(run), before)
        cleanup = WebsiteConnectionOperation.objects.get(idempotency_key=f"{website.pk}:late-dispatch:{operation.pk}")
        from .website_cancellation_retirement import bindings_for
        self.assertEqual(cleanup.payload["cancel_run_bindings"], bindings_for([run]))
        self.assertTrue(WebsiteConnectionOperation.objects.filter(connection=website, action="disconnect",
            payload__cancel_run_ids__contains=[run.run_id]).exists())

    def test_actual_scan_and_setup_post_metadata_preserve_racing_typed_journal_and_clocks(self):
        from .website_models import WebsiteConnection
        from .website_connections import contract_for
        from .run_ownership import admitted_run_for_key, EVENT_RESULT_KEY
        from .run_event_authority import receive_authoritative_run_events
        from .tests_run_event_authority import batch
        from .run_provider_facts import save_provider_result
        from roo.services import PointsService
        from rest_framework.test import APIRequestFactory, force_authenticate
        PointsService.credit_purchased_topup(user=self.user, delta=100, description="Synthetic scan gate",
            idempotency_key="retired-post-funding")
        website = WebsiteConnection.objects.create(organization=self.org, authorized_by=self.user,
            github_repo="synthetic/metadata", repository_id=42, installation_id="1001", branch="main", verified_sha="a" * 40)
        self.config.website_connection, self.config.github_repo = website, website.github_repo
        self.config.save(update_fields=["website_connection", "github_repo"])
        provider = Mock(status_code=200)
        provider.json.return_value = {"id": 42, "full_name": website.github_repo, "default_branch": "main", "sha": "a" * 40}
        for index, endpoint in enumerate((views.VibeMarketingScanView, views.VibeMarketingArticleSystemSetupView)):
            seen = []

            def post(url, **kwargs):
                run = admitted_run_for_key(kwargs["json"]["client_request_id"])
                run = bind_admitted_run(run, f"retired-post-worker-{index}")
                self.assertEqual(receive_authoritative_run_events(batch(run, [{"status": "running"}])).status_code, 200)
                seen.append(ContentFactoryRun.objects.values().get(pk=run.pk))
                response = Mock(status_code=202, content=b"{}")
                response.json.return_value = {"run_id": run.run_id, "status": "queued"}
                return response

            def merge(run, proposed, **kwargs):
                self.assertEqual(receive_authoritative_run_events(batch(run,
                    [{"status": "running"}, {"status": "awaiting_approval"}])).status_code, 200)
                return save_provider_result(run, proposed, **kwargs)

            request = APIRequestFactory().post(f"/start?company_id={self.company.pk}", {
                **contract_for(website), "company_id": str(self.company.pk), "github_repo": website.github_repo,
                "articleSurfaceMode": "none", "articleSurfaceUrl": "/blog", "scanPurpose": "setup",
                "clientRequestId": f"retired-post-original-{index}"}, format="json")
            force_authenticate(request, user=self.user)
            with patch("integrations.services.github_connections.read_bound_repository", return_value=provider), \
                    patch.object(views, "analytics_config_for_content_factory", return_value={}), \
                    patch.object(views.http_client, "post", side_effect=post), \
                    patch("content_factory.run_provider_facts.save_provider_result", side_effect=merge):
                response = endpoint.as_view()(request)
            self.assertEqual(response.status_code, 202, response.data)
            self.assertEqual(len(seen), 1)
            after = ContentFactoryRun.objects.values().get(pk=seen[0]["id"])
            self.assertEqual(after["status"], "awaiting_approval")
            for key in ("created_at", "updated_at", "last_event_emitted_at", "reconciled_at", "run_request"):
                self.assertEqual(after[key], seen[0][key], key)
            self.assertEqual(len(after["result"][EVENT_RESULT_KEY]["events"]), 2)
            self.assertEqual(after["result"][EVENT_RESULT_KEY]["projection"]["status"], "awaiting_approval")

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

    def test_owned_deferred_refund_and_confirmed_absence_helpers_preserve_pending_evidence(self):
        raw = {"domain": self.org.domain, "client_request_id": "retired-pending-refund",
            "topic": "Retained original", "pending_billing_refund": {"charged_user_id": self.user.pk,
                "actor_id": self.actor, "article_request": {"client_request_id": "retired-pending-refund"}}}
        run = bind_admitted_run(reserve_admitted_run(organization=self.org,
            principal=user_principal(self.user.pk, self.actor), workflow="article_generation", payload=raw),
            "retired-pending-worker")
        from .tests_run_event_authority import batch
        from .run_event_authority import receive_authoritative_run_events
        self.assertEqual(receive_authoritative_run_events(batch(run, [{"status": "running"}, {"status": "failed"}])).status_code, 200)
        run.refresh_from_db()
        before = self.snapshot(run)
        with patch.object(views, "_refund_roo_points_for_article_start") as refund:
            views._process_pending_dispatch_refund(run)
            views._fail_unconfirmed_dispatch_run(run, outcome="not_received")
        refund.assert_not_called()
        self.assertEqual(self.snapshot(run), before)

    def test_old_valley_telemetry_refuses_owned_before_mutating_history_or_clocks(self):
        from .service_views import ContentFactoryRunValleyJobView
        from rest_framework.test import APIRequestFactory
        before = self.snapshot()
        request = APIRequestFactory().post("/valley-jobs", {"job_id": "old-celery-job", "transition": "started"},
            format="json", HTTP_X_API_KEY="synthetic-retired-service-key")
        response = ContentFactoryRunValleyJobView.as_view()(request, run_id=self.run.run_id)
        self.assertEqual(response.status_code, 409, response.data)
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
