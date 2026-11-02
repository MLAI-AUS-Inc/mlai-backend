"""Actual cancellation consent retains event-owned execution and current tickets."""
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from django.db import connection, transaction
from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from core.models import User
from workflow_runs.models import ContentFactoryRun, ContentFactoryRunStep, ContentFactoryRunStepAttempt
from .run_event_authority import receive_authoritative_run_events
from .run_execution_control import append_execution_ticket
from .run_ownership import (WIRE_EXECUTION_KEY, WIRE_REQUEST_KEY, bind_admitted_run,
    current_execution_ticket, reserve_admitted_run, user_principal)
from .tests_run_event_authority import FLAGS, batch
from .tests_website_connections import WebsiteDatabaseFixture
from .website_connections import transition_connection
from .website_operations import bind_operation_run, cancel_operation, reserve_workflow_operation
from .website_reconciliation import process_website_connection_operations
from . import website_cancellation_retirement as cancellations


@override_settings(**FLAGS, CONTENT_FACTORY_URL="https://cf-cancel.fixture.invalid",
                   CONTENT_FACTORY_API_KEY="synthetic-cancel-key")
class OwnedCancellationTests(WebsiteDatabaseFixture, TransactionTestCase):
    def setUp(self):
        super().setUp()
        self.user = User.objects.create_user(email="cancel-owner@synthetic.example.test")
        self.now = timezone.now()

    def make_run(self, *, owned=True, workflow="article_generation", key=None):
        key = key or "cancel-" + uuid4().hex
        payload = {**self.binding, "domain": self.org.domain, "github_repo": self.website.github_repo,
                   "client_request_id": key}
        operation = reserve_workflow_operation(self.website, workflow=workflow, payload=payload)
        if owned:
            run = reserve_admitted_run(organization=self.org, principal=user_principal(self.user.pk, f"mlai_user:{self.user.pk}"),
                workflow=workflow, payload=payload)
            run = bind_admitted_run(run, "worker-" + key)
            from .run_reconciliation import authoritative_run
            if authoritative_run(run):
                self.assertEqual(receive_authoritative_run_events(batch(run, [
                    {"status": "running"}, {"type": "step_started", "step": "write", "attempt": 1}])).status_code, 200)
            else:
                ContentFactoryRun.objects.filter(pk=run.pk).update(status="running", resume_available=True)
            run.refresh_from_db()
        else:
            run = ContentFactoryRun.objects.create(run_id=key, workflow=workflow, organization=self.org,
                domain=self.org.domain, github_repo=self.website.github_repo, status="running", resume_available=True,
                run_request=payload)
            step = ContentFactoryRunStep.objects.create(run=run, step_key="write", status="running", attempts=1)
            ContentFactoryRunStepAttempt.objects.create(step=step, attempt=1, status="running")
        bind_operation_run(operation, run)
        operation.refresh_from_db()
        return run, operation

    def snapshot(self, run):
        return (ContentFactoryRun.objects.values().get(pk=run.pk),
            list(run.steps.order_by("pk").values()),
            list(ContentFactoryRunStepAttempt.objects.filter(step__run=run).order_by("pk").values()))

    def advance(self, run, *, operation=None):
        with transaction.atomic():
            run = ContentFactoryRun.objects.select_for_update().get(pk=run.pk)
            scope = current_execution_ticket(run)["scope"]
            if operation is not None:
                operation.payload = {**operation.payload, "attempt": operation.payload.get("attempt", 1) + 1}
                operation.save(update_fields=["payload"])
                scope["operation_attempt"] = operation.payload["attempt"]
            ticket = append_execution_ticket(run, control_key=uuid4().hex * 2, reason="operation_resume",
                                            scope=scope)
        return ticket

    def cancel(self, operation):
        return cancel_operation(self.config, data={**self.binding, "operation_id": str(operation.pk),
            "operation_attempt": operation.payload.get("attempt", 1), "deletion_epoch": 0}, idempotency_key=uuid4().hex)

    def process(self, op, *, after=1):
        posted = []
        def post(url, **kwargs):
            self.assertFalse(connection.in_atomic_block, "Cancellation HTTP must follow the consent commit")
            posted.append((url, deepcopy(kwargs["json"])))
            return SimpleNamespace(status_code=200, json=lambda: {"status": "cancelled", "cleanup_pending": False,
                                                                "cleanup_success": True})
        with patch("integrations.http_client.post", side_effect=post):
            process_website_connection_operations(connection_id=self.website.pk, now=self.now + timedelta(seconds=after))
        op.refresh_from_db()
        return posted

    def test_disconnect_retires_consent_but_only_legacy_rows_receive_local_cancel_writes(self):
        owned, _ = self.make_run()
        legacy, _ = self.make_run(owned=False)
        before = self.snapshot(owned)
        with override_settings(ARTICLES_RUN_OWNERSHIP_ENABLED=False, ARTICLES_RUN_FACT_AUTHORITY_ENABLED=False,
                               ARTICLES_RUN_EVENT_AUTHORITY_ENABLED=False):
            op = transition_connection(self.config, action="disconnect", expected=self.binding, idempotency_key="disconnect")
        self.assertEqual(self.snapshot(owned), before)
        legacy.refresh_from_db()
        self.assertEqual(legacy.status, "cancelled")
        self.assertFalse(legacy.resume_available)
        self.website.refresh_from_db()
        self.assertEqual((self.website.state, self.website.generation), ("disconnected", 2))
        self.assertEqual(set(op.payload["cancel_run_ids"]), {owned.run_id, legacy.run_id})
        self.assertEqual(op.payload["cancel_run_bindings"][owned.run_id], cancellations.run_binding(owned))
        posted = self.process(op)
        own = [(url, body) for url, body in posted if owned.run_id in url]
        self.assertEqual({url.rsplit("/", 1)[-1] for url, _ in own}, {"cancel", "stop"})
        self.assertTrue(all(body[WIRE_EXECUTION_KEY] == current_execution_ticket(owned) for _, body in own))
        legacy_cancel = next(body for url, body in posted if legacy.run_id in url and url.endswith("/cancel"))
        self.assertNotIn(WIRE_EXECUTION_KEY, legacy_cancel)
        self.assertNotIn("expected_cancellation_v1", legacy_cancel)
        self.assertIn(owned.run_id, op.payload["cancel_run_ids"])
        self.assertEqual(self.snapshot(owned), before)

    def test_cancel_operation_uses_current_attempt_ticket_without_rewriting_original_history(self):
        run, operation = self.make_run()
        ticket = self.advance(run, operation=operation)
        run.refresh_from_db()
        before = self.snapshot(run)
        op = self.cancel(operation)
        operation.refresh_from_db()
        self.assertEqual(operation.state, "cancelled")
        self.assertEqual(self.snapshot(run), before)
        self.assertEqual(run.run_request["operation_attempt"], 1)
        current = cancellations.run_for(op, run.run_id)
        self.assertIsNotNone(current)
        self.assertEqual(cancellations.expected_article_cancellation(current)["scope"]["operation_attempt"], 2)
        posted = self.process(op)
        cancel = next(body for url, body in posted if url.endswith("/cancel"))
        self.assertEqual(cancel[WIRE_EXECUTION_KEY], ticket)
        self.assertEqual(cancel["expected_cancellation_v1"]["scope"]["operation_attempt"], 2)
        self.assertEqual(self.snapshot(run), before)
        self.assertIn(run.run_id, op.payload["cancel_run_ids"])

    def test_new_ticket_after_capture_refuses_both_cancel_and_preview_commands(self):
        run, operation = self.make_run()
        op = self.cancel(operation)
        self.advance(run)
        before = self.snapshot(run)
        self.assertIsNone(cancellations.run_for(op, run.run_id))
        posted = self.process(op)
        self.assertFalse(any(run.run_id in url for url, _ in posted))
        self.assertIn(run.run_id, op.payload["cancel_run_ids"])
        self.assertIn(run.run_id, op.payload["stop_preview_run_ids"])
        self.assertEqual(self.snapshot(run), before)

    def test_http_ack_only_never_mutates_lifecycle_but_real_current_terminal_journal_retires_command(self):
        run, operation = self.make_run()
        op = self.cancel(operation)
        before = self.snapshot(run)
        self.process(op)
        self.assertEqual(self.snapshot(run), before)
        self.assertIn(run.run_id, op.payload["cancel_run_ids"])
        run.refresh_from_db()
        self.assertEqual(receive_authoritative_run_events(batch(run, [{"seq": 3, "state_version": 3,
            "status": "cancelled", "step": "cancelled"}])).status_code, 200)
        after_event = self.snapshot(run)
        self.process(op, after=60)
        self.assertEqual(op.payload["cancel_run_ids"], [])
        self.assertEqual(self.snapshot(run), after_event)

    def test_nonarticle_stop_uses_current_scope_and_echoes_on_existing_connection_endpoint(self):
        run, operation = self.make_run(workflow="repo_scan")
        ticket = self.advance(run, operation=operation)
        run.refresh_from_db()
        before = self.snapshot(run)
        op = self.cancel(operation)
        posted = self.process(op)
        body = next(body for url, body in posted if url.endswith("/cancel-operation"))
        self.assertEqual(body["operation_attempt"], 2)
        self.assertEqual(body[WIRE_EXECUTION_KEY], ticket)
        self.assertEqual(self.snapshot(run), before)

    def test_disconnect_nonarticle_keeps_run_cancel_endpoint_and_current_echoes_without_article_scope(self):
        run, _ = self.make_run(workflow="repo_scan")
        op = transition_connection(self.config, action="disconnect", expected=self.binding, idempotency_key="scan-disconnect")
        before = self.snapshot(run)
        posted = self.process(op)
        body = next(body for url, body in posted if url.endswith(f"/api/runs/{run.run_id}/cancel"))
        self.assertEqual(body[WIRE_EXECUTION_KEY], current_execution_ticket(run))
        self.assertNotIn("expected_cancellation_v1", body)
        self.assertEqual(self.snapshot(run), before)

    def test_reservation_only_and_unmarked_operation_cancel_keep_legacy_execution_semantics(self):
        with override_settings(ARTICLES_RUN_EVENT_AUTHORITY_ENABLED=False):
            run, operation = self.make_run()
        # No v2 journal was projected for reservation-only work. The existing
        # local cancellation authority remains exactly the legacy path.
        op = self.cancel(operation)
        run.refresh_from_db()
        self.assertEqual(run.status, "cancelled")
        self.assertIn(run.run_id, op.payload["cancel_run_ids"])

    def test_unmarked_operation_cancel_keeps_existing_local_write_and_wire_contract(self):
        run, operation = self.make_run(owned=False)
        op = self.cancel(operation)
        run.refresh_from_db()
        self.assertEqual(run.status, "cancelled")
        posted = self.process(op)
        body = next(body for url, body in posted if url.endswith("/cancel"))
        self.assertNotIn(WIRE_REQUEST_KEY, body)
        self.assertNotIn(WIRE_EXECUTION_KEY, body)
        self.assertIn("expected_cancellation_v1", body)

    def test_old_operation_cannot_cancel_work_moved_to_a_new_current_operation(self):
        run, original = self.make_run()
        payload = {**self.binding, "client_request_id": "new-owner-operation"}
        current = reserve_workflow_operation(self.website, workflow=run.workflow, payload=payload)
        with transaction.atomic():
            run = ContentFactoryRun.objects.select_for_update().get(pk=run.pk)
            scope = {**current_execution_ticket(run)["scope"], "operation_id": str(current.pk), "operation_attempt": 1}
            append_execution_ticket(run, control_key="b" * 64, reason="operation_resume", scope=scope)
        bind_operation_run(current, run)
        before = self.snapshot(run)
        old_stop = self.cancel(original)
        self.assertNotIn(run.run_id, old_stop.payload["cancel_run_ids"])
        new_stop = self.cancel(current)
        self.assertIn(run.run_id, new_stop.payload["cancel_run_ids"])
        self.assertIsNotNone(cancellations.run_for(new_stop, run.run_id))
        self.assertEqual(self.snapshot(run), before)

    def test_damaged_current_ticket_cannot_block_consent_revocation_or_grant_unscoped_stop(self):
        from .run_ownership import OWNER_EXECUTION_KEY
        run, _ = self.make_run()
        result = deepcopy(run.result)
        result[OWNER_EXECUTION_KEY]["current_execution_id"] = "missing-ticket"
        ContentFactoryRun.objects.filter(pk=run.pk).update(result=result)
        before = self.snapshot(run)
        op = transition_connection(self.config, action="disconnect", expected=self.binding, idempotency_key="damaged-owner")
        self.website.refresh_from_db()
        self.assertEqual(self.website.state, "disconnected")
        self.assertEqual(self.snapshot(run), before)
        self.assertNotIn(run.run_id, op.payload["cancel_run_bindings"])
        self.assertFalse(any(run.run_id in url for url, _ in self.process(op)))
        self.assertIn(run.run_id, op.payload["cancel_run_ids"])
