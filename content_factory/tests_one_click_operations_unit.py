"""One-click lifecycle replays without constructing a database."""
from contextlib import nullcontext
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4

from django.test import SimpleTestCase
from django.utils import timezone

from .website_contract import WebsiteAuthorityError


class PrepareJourneyTests(SimpleTestCase):
    def operation(self):
        now = timezone.now()
        connection = SimpleNamespace(pk="website", generation=3, state="connected", organization_id=7)
        return SimpleNamespace(pk="prepare", action="prepare", connection=connection, connection_id="website", generation=3,
            state="pending", payload={"company_id": "9", "requested_by_user_id": "4"}, receipt={},
            attempts=0, next_attempt_at=None, updated_at=now, save=Mock())

    def run_step(self, step, handler=None):
        from . import website_prepare as prepare
        op = self.operation()
        config = SimpleNamespace(website_connection=op.connection, website_connection_id=op.connection_id)
        context = SimpleNamespace(company=SimpleNamespace(pk=9), organization=SimpleNamespace(pk=7))
        journey = {"nextAction": {"id": step}, "capabilities": {"canPublishArticle": False}}
        handler = handler or Mock(return_value={"status": "completed"})
        with patch.object(prepare, "_claim_prepare", return_value=op), \
                patch.object(prepare, "_prepare_context", return_value=(context, config)), \
                patch.object(prepare, "journey_for_context", return_value=journey), \
                patch.object(prepare, "_persist_prepare", side_effect=lambda operation, **kwargs: operation):
            result = prepare.advance_prepare(op.pk, handlers={step: handler})
        return result, handler

    def test_every_journey_step_uses_its_handler(self):
        for step in ["verify-access", "scan", "setup", "verify", "ci-attestation"]:
            with self.subTest(step=step):
                op, handler = self.run_step(step)
                handler.assert_called_once()
                self.assertEqual(op.receipt["current_step"], step)

    def test_authorization_failure_has_one_user_prompt(self):
        handler = Mock(side_effect=WebsiteAuthorityError("github_reconnect_required", "Authorize GitHub."))
        op, _ = self.run_step("verify-access", handler)
        self.assertEqual(op.state, "needs_user")
        self.assertEqual(op.receipt["user_action"]["id"], "authorize_github")

    def test_retryable_failure_backoff_then_attention(self):
        from .website_prepare import prepare_failure
        op = self.operation()
        error = WebsiteAuthorityError("worker_unavailable", "Temporarily unavailable.", retryable=True)
        now = timezone.now()
        for attempt in range(1, 4):
            prepare_failure(op, "setup", error, now=now)
            self.assertEqual(op.receipt["step_failures"]["setup"], attempt)
            if attempt < 3:
                self.assertEqual(op.state, "pending")
                self.assertEqual(op.next_attempt_at, now + timedelta(minutes=[1, 2][attempt - 1]))
        self.assertEqual(op.state, "needs_attention")

    def test_publish_ready_completes_without_dispatch(self):
        from . import website_prepare as prepare
        op = self.operation()
        with patch.object(prepare, "_claim_prepare", return_value=op), \
                patch.object(prepare, "_prepare_context", return_value=(Mock(), Mock())), \
                patch.object(prepare, "journey_for_context", return_value={"capabilities": {"canPublishArticle": True}}), \
                patch.object(prepare, "_persist_prepare", side_effect=lambda operation, **kwargs: operation):
            prepare.advance_prepare(op.pk, handlers={})
        self.assertEqual(op.state, "completed")
        self.assertEqual(op.receipt["status"], "ready")

    def test_repeat_click_reuses_generation_operation(self):
        from . import website_prepare as prepare
        op = self.operation()
        config = SimpleNamespace(website_connection=op.connection, website_connection_id=op.connection_id,
            organization_id=7)
        binding = {"website_connection_id": "website", "connection_generation": 3}
        with patch.object(prepare, "authority_guard", side_effect=lambda *a, **k: nullcontext(op.connection)), \
                patch.object(prepare.WebsiteConnectionOperation.objects, "get_or_create", return_value=(op, False)) as create:
            self.assertIs(prepare.start_prepare(config, expected=binding, company_id="9", user=SimpleNamespace(pk=4)), op)
        self.assertEqual(create.call_args.kwargs["idempotency_key"], "website:prepare:3")

    def test_late_prepare_write_is_conditioned_on_lease_and_generation(self):
        from . import website_prepare as prepare
        op = self.operation()
        op._claimed_at = op.updated_at
        with patch.object(prepare.WebsiteConnectionOperation.objects, "filter") as rows:
            prepare._persist_prepare(op, now=timezone.now())
        self.assertEqual(rows.call_args.kwargs["connection__generation"], 3)
        self.assertEqual(rows.call_args.kwargs["connection__state"], "connected")
        self.assertEqual(rows.call_args.kwargs["updated_at"], op._claimed_at)

    def test_dead_child_is_cleared_before_retry(self):
        from . import website_prepare as prepare
        from workflow_runs.models import ContentFactoryRun
        op = self.operation()
        op.receipt = {"child_run_id": "dead-child", "current_step": "setup"}
        with patch.object(prepare, "_claim_prepare", return_value=op), \
                patch.object(prepare, "_prepare_context", return_value=(Mock(), Mock())), \
                patch.object(prepare, "journey_for_context", return_value={"nextAction": {"id": "setup"}}), \
                patch.object(prepare, "_persist_prepare", side_effect=lambda operation, **kw: operation), \
                patch.object(ContentFactoryRun.objects, "filter") as runs:
            runs.return_value.first.return_value = SimpleNamespace(status="failed", result={}, workflow="article_system_setup")
            prepare.advance_prepare(op.pk)
        self.assertNotIn("child_run_id", op.receipt)
        self.assertEqual(op.payload["attempt"], 2)
        self.assertEqual(op.state, "pending")

    def test_setup_ready_for_review_checks_merge_without_waiting_for_terminal(self):
        from . import website_prepare as prepare
        from workflow_runs.models import ContentFactoryRun
        op = self.operation()
        op.receipt = {"child_run_id": "setup-child", "current_step": "setup"}
        child = SimpleNamespace(status="needs_review", workflow="article_system_setup", result={
            "article_system_setup": {"status": "preview_ready"}})
        with patch.object(prepare, "_claim_prepare", return_value=op), \
                patch.object(prepare, "_prepare_context", return_value=(Mock(), Mock())), \
                patch.object(prepare, "journey_for_context", return_value={"nextAction": {"id": "verify"}}), \
                patch.object(prepare, "_persist_prepare", side_effect=lambda operation, **kw: operation), \
                patch.object(prepare, "maybe_merge_prepared_setup", return_value={"status": "needs_user",
                    "user_action": {"id": "merge_pr", "pr_number": 42}}) as merge, \
                patch.object(ContentFactoryRun.objects, "filter") as runs:
            runs.return_value.first.return_value = child
            prepare.advance_prepare(op.pk)
        merge.assert_called_once()
        self.assertEqual(op.state, "needs_user")
        self.assertEqual(op.receipt["user_action"]["pr_number"], 42)

    def test_prepare_callback_waits_for_commit(self):
        from . import website_prepare as prepare
        callbacks = []
        with patch.object(prepare.transaction, "on_commit", side_effect=lambda callback, **kw: callbacks.append(callback)), \
                patch.object(prepare, "advance_prepares_for_connection") as advance:
            prepare.wake_prepare_after_commit("website")
            advance.assert_not_called()
            callbacks[0]()
            advance.assert_called_once_with("website")


class BillingClarityTests(SimpleTestCase):
    def test_bootstrap_byte_header_matches_canonical_json(self):
        from .vibe_marketing_views import _timed_vibe_response
        from rest_framework.renderers import JSONRenderer
        import time
        payload = {"startup": "Melbourne café", "points": 0}
        response = _timed_vibe_response(payload, started_at=time.perf_counter(), metric_name="vibe_bootstrap")
        self.assertEqual(int(response["X-MLAI-Payload-Bytes"]), len(JSONRenderer().render(payload)))

    def test_setup_is_free_and_account_balance_is_named(self):
        from .billing import build_roo_points_payload, get_content_factory_setup_cost_points
        self.assertEqual(get_content_factory_setup_cost_points("customer.test"), 0)
        payload = build_roo_points_payload(domain="customer.test", action="article_generation", current_balance=2,
            account_email="sam@example.test", other_founder_has_points=True)
        self.assertIn("s***@example.test", payload["message"])
        self.assertIn("2 Roo points", payload["message"])
        self.assertTrue(payload["other_founder_has_points"])

    def test_company_billing_can_only_charge_a_current_opted_in_founder(self):
        from .company_billing import billing_user_for_organization
        requester, payer = SimpleNamespace(pk=1), SimpleNamespace(pk=2, is_active=True)
        organization = SimpleNamespace(billing_user=None, founder_companies=Mock())
        self.assertIs(billing_user_for_organization(organization, requester), requester)
        organization.billing_user = payer
        organization.founder_companies.filter.return_value.exists.return_value = True
        self.assertIs(billing_user_for_organization(organization, requester), payer)
        organization.founder_companies.filter.return_value.exists.return_value = False
        with self.assertRaises(WebsiteAuthorityError) as error:
            billing_user_for_organization(organization, requester)
        self.assertEqual(error.exception.code, "company_billing_founder_unavailable")


class DispatchOutboxTests(SimpleTestCase):
    def test_unconfirmed_prior_delivery_cannot_refund_on_a_new_http_refusal(self):
        from .dispatch_outbox import dispatch_outcome
        self.assertEqual(dispatch_outcome(401, {"detail": "Unavailable"}), ("pending", True))
        self.assertEqual(dispatch_outcome(401, {}, known_key_outcome="absent"), ("failed", False))

    def test_accepted_child_cancelled_by_disconnect_requires_original_payer_refund(self):
        from . import dispatch_outbox as outbox
        row = SimpleNamespace(client_request_id="saved-key", organization=SimpleNamespace(domain="fixture.test"),
            payload={"request": {}, "workflow": "article_generation"})
        with patch("content_factory.dispatch_binding.bind_dispatch_token_run"), \
                patch("content_factory.vibe_marketing_views._create_local_run", return_value=SimpleNamespace(status="cancelled")):
            self.assertEqual(outbox._accept_delivery(row, {}, {"run_id": "real-child"}),
                ("failed", "website_connection_changed", "real-child"))

    def test_existing_remote_identity_is_adopted_before_new_policy_or_post(self):
        from . import dispatch_outbox as outbox
        row = SimpleNamespace(client_request_id="saved-key", payload={"request": {}, "endpoint": "article"})
        with patch("content_factory.vibe_marketing_views._content_factory_remote_config", return_value={"enabled": True}), \
                patch("content_factory.vibe_marketing_views._lookup_content_factory_dispatch_by_key", return_value=("dispatched", {"run_id": "real-run"})), \
                patch("content_factory.vibe_marketing_views._refresh_article_editorial_payload") as policy, \
                patch.object(outbox, "_accept_delivery", return_value=("delivered", "", "real-run")) as adopt, \
                patch("integrations.http_client.post") as post:
            self.assertEqual(outbox._deliver(row), ("delivered", "", "real-run"))
        policy.assert_not_called()
        post.assert_not_called()
        self.assertTrue(adopt.call_args.kwargs["recovered"])

    def test_fresh_policy_rejection_never_posts_to_worker(self):
        from . import dispatch_outbox as outbox
        from rest_framework.response import Response
        row = SimpleNamespace(client_request_id="saved-key", organization=Mock(), payload={"request": {
            "delivery_mode": "content_only", "delivery_mode_confirmed": True}, "endpoint": "article"})
        with patch("content_factory.vibe_marketing_views._content_factory_remote_config", return_value={"enabled": True}), \
                patch("content_factory.vibe_marketing_views._lookup_content_factory_dispatch_by_key", return_value=("absent", {})), \
                patch("content_factory.vibe_marketing_views._refresh_article_editorial_payload", return_value=Response({"code": "editorial_policy_changed"}, status=409)), \
                patch("integrations.http_client.post") as post:
            self.assertEqual(outbox._deliver(row), ("pending", "editorial_policy_changed", ""))
        post.assert_not_called()

    def test_exhausted_unknown_delivery_never_authorizes_refund(self):
        from .dispatch_outbox import exhausted_dispatch_outcome
        self.assertEqual(exhausted_dispatch_outcome("unknown"), "awaiting_resolution")
        self.assertEqual(exhausted_dispatch_outcome("absent"), "failed")
        self.assertEqual(exhausted_dispatch_outcome("dispatched"), "delivered")

    def test_terminal_refusal_is_not_retried(self):
        from .dispatch_outbox import dispatch_outcome
        state, retryable = dispatch_outcome(409, {"code": "dispatch_rejected", "backend_code": "run_cancelled"})
        self.assertEqual(state, "failed")
        self.assertFalse(retryable)

    def test_unknown_transport_outcome_retries_until_budget(self):
        from .dispatch_outbox import retry_delay
        self.assertEqual([retry_delay(attempt) for attempt in range(1, 5)], [60, 120, 300, 600])


class OutcomeMetricsTests(SimpleTestCase):
    def test_restart_lineage_counts_one_paid_root_and_preserves_intervention(self):
        from .one_click_metrics import summarize_one_click
        now = timezone.now()
        data = summarize_one_click(article_rows=[
            {"run_id": "original", "organization_id": 7, "status": "failed", "created_at": now,
                "updated_at": now, "run_request": {"roo_points_ledger_id": "12"}, "result": {"user_action_count": 1}},
            {"run_id": "restart", "organization_id": 7, "status": "awaiting_approval", "created_at": now,
                "updated_at": now + timedelta(minutes=10), "run_request": {"roo_points_ledger_id": "12", "restart_source_run_id": "original"},
                "result": {"user_action_count": 0, "content_acceptance_calls": 3}},
        ], prepare_rows=[])
        self.assertEqual(data["articleRuns"], 1)
        self.assertEqual(data["articleAttempts"], 2)
        self.assertEqual(data["articlesWithZeroUserActions"], 0)

    def test_actual_approval_status_is_a_review_outcome(self):
        from .one_click_metrics import summarize_one_click
        data = summarize_one_click(article_rows=[{"status": "awaiting_approval", "result": {"user_action_count": 0}}], prepare_rows=[])
        self.assertEqual(data["articlesWithZeroUserActions"], 1)

    def test_unknown_user_actions_do_not_count_as_zero(self):
        from .one_click_metrics import summarize_one_click
        now = timezone.now()
        data = summarize_one_click(article_rows=[{"status": "completed", "result": {}, "created_at": now,
            "updated_at": now + timedelta(minutes=20)}], prepare_rows=[])
        self.assertEqual(data["articlesReachingReview"], 1)
        self.assertEqual(data["articlesWithZeroUserActions"], 0)
        self.assertEqual(data["articlesWithoutActionTelemetry"], 1)
        self.assertEqual(data["durationSeconds"]["p50"], 1200)

    def test_first_click_and_review_calls_use_persisted_telemetry(self):
        from .one_click_metrics import summarize_one_click
        data = summarize_one_click(article_rows=[{"status": "needs_review", "result": {
            "user_action_count": 0, "content_acceptance_calls": 4, "authority_refusals": [{}, {}],
            "reconciliation": {"outcome": "adopted_remote_terminal"}}}],
            prepare_rows=[{"state": "completed", "payload": {"user_clicks": 1}, "receipt": {"status": "ready"}}])
        self.assertEqual(data["setupsReadyOnFirstClick"], 1)
        self.assertEqual(data["articlesWithZeroUserActions"], 1)
        self.assertEqual(data["reviewCalls"]["mean"], 4)
        self.assertEqual(data["refusals"], 2)


class DisconnectTests(SimpleTestCase):
    def test_disconnect_cleanup_failure_stops_after_five_attempts(self):
        from . import website_disconnect as disconnect
        op = SimpleNamespace(state="pending", receipt={}, next_attempt_at=None)
        for number in range(1, 6):
            disconnect.disconnect_retry_policy(op, now=timezone.now(), progressed=False)
            self.assertEqual(op.state, "pending" if number < 5 else "needs_attention")
        self.assertEqual(op.receipt["user_action"]["id"], "retry_disconnect")

    def test_retry_with_original_generation_reuses_disconnect_receipt(self):
        from . import website_disconnect as disconnect
        identifier = uuid4()
        connection = SimpleNamespace(pk=identifier, generation=4, state="disconnected")
        operation = SimpleNamespace(action="disconnect", generation=4, state="pending", payload={"remove_setup": False})
        with patch.object(disconnect.WebsiteConnectionOperation.objects, "filter") as operations, \
                patch.object(disconnect, "transition_connection") as transition:
            operations.return_value.first.return_value = operation
            result = disconnect.start_disconnect(SimpleNamespace(website_connection=connection), user=SimpleNamespace(pk=1),
                data={"website_connection_id": str(identifier), "connection_generation": 3})
        self.assertIs(result, operation)
        transition.assert_not_called()

    def test_retry_current_generation_reuses_explicit_operation_and_cleanup_choice(self):
        from . import website_disconnect as disconnect
        identifier, operation_id = uuid4(), uuid4()
        connection = SimpleNamespace(pk=identifier, generation=4, state="disconnected")
        operation = SimpleNamespace(pk=operation_id, generation=4, action="disconnect", state="needs_attention",
            payload={"remove_setup": True}, receipt={"cleanup_failures": 5}, save=Mock())
        with patch.object(disconnect.WebsiteConnectionOperation.objects, "filter") as operations, \
                patch.object(disconnect, "transition_connection") as transition:
            operations.return_value.first.return_value = operation
            operations.return_value.update.return_value = 1
            result = disconnect.start_disconnect(SimpleNamespace(website_connection=connection), user=SimpleNamespace(pk=1),
                data={"website_connection_id": str(identifier), "connection_generation": 4, "operation_id": str(operation_id)})
        self.assertIs(result, operation)
        self.assertTrue(operation.payload["remove_setup"])
        self.assertEqual(operation.state, "pending")
        self.assertEqual(operation.receipt["cleanup_failures"], 0)
        self.assertEqual(operations.call_args_list[0].kwargs["pk"], operation_id)
        transition.assert_not_called()

    def test_disconnect_fence_and_refund_manifest_share_one_transaction(self):
        from contextlib import contextmanager
        from . import website_disconnect as disconnect
        connection = SimpleNamespace(pk=uuid4(), repository_mutations=Mock())
        connection.repository_mutations.values_list.return_value = [uuid4()]
        operation = SimpleNamespace(pk=uuid4(), payload={"cancel_run_ids": ["child"]}, receipt={}, save=Mock())
        active = False
        @contextmanager
        def atomic():
            nonlocal active
            active = True
            try:
                yield
            finally:
                active = False
        def transition(*args, **kwargs):
            self.assertTrue(active)
            return operation
        def save(**kwargs):
            self.assertTrue(active)
            self.assertEqual(operation.payload["refund_run_ids"], ["child"])
        operation.save.side_effect = save
        with patch.object(disconnect.transaction, "atomic", side_effect=atomic), \
                patch.object(disconnect.WebsiteConnectionOperation.objects, "filter") as existing, \
                patch.object(disconnect.WebsiteConnectionOperation.objects, "select_for_update") as saved, \
                patch.object(disconnect, "transition_connection", side_effect=transition):
            existing.return_value.first.return_value = None
            saved.return_value.get.return_value = operation
            disconnect.start_disconnect(SimpleNamespace(website_connection=connection), user=SimpleNamespace(pk=1),
                data={"website_connection_id": str(connection.pk), "connection_generation": 3})
        operation.save.assert_called_once()

    def test_retry_cannot_revive_disconnect_after_reconnect(self):
        from . import website_disconnect as disconnect
        identifier, operation_id = uuid4(), uuid4()
        connection = SimpleNamespace(pk=identifier, generation=5, state="connected")
        operation = SimpleNamespace(pk=operation_id, generation=4, action="disconnect", state="needs_attention",
            payload={"remove_setup": False}, save=Mock())
        with patch.object(disconnect.WebsiteConnectionOperation.objects, "filter") as operations:
            operations.return_value.first.return_value = operation
            with self.assertRaises(WebsiteAuthorityError) as error:
                disconnect.start_disconnect(SimpleNamespace(website_connection=connection), user=SimpleNamespace(pk=1),
                    data={"website_connection_id": str(identifier), "connection_generation": 5, "operation_id": str(operation_id)})
        self.assertEqual(error.exception.code, "website_connection_changed")
        operation.save.assert_not_called()

    def test_no_cleanup_files_is_explicit_in_composite_receipt(self):
        from . import website_disconnect as disconnect
        operation = SimpleNamespace(receipt={"completed_steps": ["refunds", "repository_refs"]}, payload={"remove_setup": True})
        with patch.object(disconnect, "_fence_disconnect"), patch.object(disconnect, "_open_cleanup",
                return_value=SimpleNamespace(pk=uuid4(), receipt={"status": "no_unchanged_owned_files"})):
            disconnect.advance_disconnect_steps(operation)
        self.assertEqual(operation.receipt["cleanup_status"], "no_unchanged_owned_files")
        self.assertFalse(operation.receipt["cleanup_required"])

    def test_remove_choice_cannot_change_on_idempotent_retry(self):
        from . import website_disconnect as disconnect
        identifier = uuid4()
        with patch.object(disconnect.WebsiteConnectionOperation.objects, "filter") as operations:
            operations.return_value.first.return_value = SimpleNamespace(action="disconnect", payload={"remove_setup": False})
            with self.assertRaises(WebsiteAuthorityError) as error:
                disconnect.start_disconnect(SimpleNamespace(website_connection=SimpleNamespace(pk=identifier)), user=SimpleNamespace(pk=1),
                    data={"website_connection_id": str(identifier), "connection_generation": 3, "remove_setup": True})
        self.assertEqual(error.exception.code, "operation_key_conflict")
    def test_resumed_disconnect_runs_only_missing_steps(self):
        from . import website_disconnect as disconnect
        op = SimpleNamespace(receipt={"completed_steps": ["authority_revoked", "refunds", "repository_refs"]},
            payload={"remove_setup": False})
        with patch.object(disconnect, "_fence_disconnect"), patch.object(disconnect, "refund_cancelled_runs") as refund, \
                patch.object(disconnect, "close_owned_repository_refs") as close:
            disconnect.advance_disconnect_steps(op)
        refund.assert_not_called()
        close.assert_not_called()
        self.assertIn("pages_kept", op.receipt["completed_steps"])
        self.assertFalse(op.receipt["disconnect_steps_pending"])

    def test_cleanup_diff_is_always_a_subset_of_unchanged_ledger(self):
        from .website_contract import cleanup_plan
        from itertools import product
        # Exhaust the ownership/content-change combinations, including shared
        # and edited customer files, rather than mirroring a single fixture.
        for ownership, current in product(["created", "shared", "modified"], [None, "owned", "customer"]):
            ledger = [{"path": "articles/route.ts", "ownership": ownership, "kind": "setup_route", "after_sha256": "owned"}]
            proposal = cleanup_plan(ledger, {"articles/route.ts": current})
            self.assertLessEqual(set(proposal["deletions"]), {row["path"] for row in ledger})
            if ownership != "created" or current != "owned":
                self.assertEqual(proposal["deletions"], [])

    def test_customer_changed_branch_is_retained(self):
        from . import website_disconnect as disconnect
        connection = SimpleNamespace(branch="main", github_repo="fixture/site", repository_id=123, installation_id="4",
            repository_mutations=Mock())
        mutation = SimpleNamespace(pk="mutation", branch="codex/setup", head_sha="a" * 40, pr_url="")
        query = connection.repository_mutations.filter.return_value.order_by.return_value
        query.__iter__ = Mock(return_value=iter([mutation]))
        query.count.return_value = 1
        op = SimpleNamespace(connection=connection, receipt={}, payload={"mutation_ids": ["mutation"]})
        response = Mock(status_code=200)
        response.json.return_value = {"object": {"sha": "b" * 40}}
        with patch.object(disconnect, "_fence_disconnect"), \
                patch("integrations.services.github_app.create_installation_access_token", return_value=SimpleNamespace(token="synthetic")), \
                patch("integrations.http_client.get", return_value=response), patch("integrations.http_client.delete") as delete:
            self.assertTrue(disconnect.close_owned_repository_refs(op))
        self.assertEqual(op.receipt["skipped_refs"][0]["reason"], "customer_changed_branch")
        self.assertEqual(delete.call_count, 1)  # Token revocation only.
