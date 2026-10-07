"""Replay terminal setup recovery with synthetic persistence and no database."""
from contextlib import contextmanager, nullcontext
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4

from django.test import SimpleTestCase
from rest_framework.response import Response

from workflow_runs.models import ContentFactoryRun
from . import website_connections as authority
from . import website_operations as operations
from .website_contract import WebsiteAuthorityError


class WebsiteOperationRecoveryTests(SimpleTestCase):
    def setUp(self):
        self.website = SimpleNamespace(pk=uuid4(), organization_id=42, generation=3, blockers=[], operations=Mock())
        self.op = SimpleNamespace(pk=uuid4(), connection=self.website, connection_id=self.website.pk,
            generation=3, action="workflow", state="running", payload={"attempt": 2, "run_id": "saved-setup",
            "workflow": "article_system_setup", "client_request_id": "old-request"},
            idempotency_key="old-key", receipt={}, save=Mock(), refresh_from_db=Mock())
        self.binding = {"website_connection_id": str(self.website.pk), "connection_generation": 3,
            "repository_id": 42, "operation_id": str(self.op.pk), "operation_attempt": 2, "deletion_epoch": 0}
        self.run = SimpleNamespace(run_id="saved-setup", organization_id=42, status="cancelled",
            run_request=dict(self.binding), result={})
        self.website.operations.filter.return_value.first.return_value = self.op

    def test_deliberate_restart_reconciles_terminal_run_and_keeps_new_key(self):
        for status in operations.TERMINAL_WORKFLOW_STATES:
            with self.subTest(status=status), \
                    patch.object(authority, "authority_guard", side_effect=lambda *a, **k: nullcontext()), \
                    patch.object(authority, "extend_owner_operation_contract"), \
                    patch.object(ContentFactoryRun.objects, "select_for_update") as runs, \
                    patch.object(operations.WebsiteConnectionOperation.objects, "get_or_create") as create:
                self.op.state = "running"
                self.run.status = status
                self.website.operations.select_for_update.return_value.filter.return_value = [self.op]
                runs.return_value.filter.return_value.first.return_value = self.run
                fresh = SimpleNamespace(pk=uuid4(), generation=3, state="running", payload={"attempt": 1,
                    "workflow": "article_system_setup", "request_digest": operations.evidence_digest({"domain": "fixture.test"})})
                create.return_value = (fresh, True)
                payload = {"client_request_id": "new-request", "domain": "fixture.test"}
                self.assertIs(operations.reserve_workflow_operation(self.website, workflow="article_system_setup", payload=payload), fresh)
                self.assertEqual(self.op.state, status)
                self.assertEqual(self.op.receipt["status"], status)
                self.assertEqual(create.call_args.kwargs["idempotency_key"], f"{self.website.pk}:workflow:new-request")
                self.assertEqual(payload["client_request_id"], "new-request")
                self.assertEqual(payload["operation_id"], str(fresh.pk))

    def test_in_flight_duplicate_reuses_original_dispatch_identity(self):
        self.run.status = "running"
        self.op.payload["request_digest"] = operations.evidence_digest({"domain": "fixture.test"})
        with patch.object(authority, "authority_guard", side_effect=lambda *a, **k: nullcontext()), \
                patch.object(authority, "extend_owner_operation_contract"), \
                patch.object(ContentFactoryRun.objects, "select_for_update") as runs, \
                patch.object(operations.WebsiteConnectionOperation.objects, "get_or_create") as create:
            self.website.operations.select_for_update.return_value.filter.return_value = [self.op]
            runs.return_value.filter.return_value.first.return_value = self.run
            create.return_value = (self.op, False)
            payload = {"client_request_id": "replacement-key", "domain": "fixture.test"}
            operations.reserve_workflow_operation(self.website, workflow="article_system_setup", payload=payload)
            self.assertEqual(payload["client_request_id"], "old-request")
            self.assertEqual(create.call_args.kwargs["idempotency_key"], "old-key")
            self.op.save.assert_not_called()

    def test_reconciliation_does_not_trust_a_different_saved_attempt(self):
        self.run.run_request["operation_attempt"] = 1
        with patch.object(authority, "authority_guard", side_effect=lambda *a, **k: nullcontext()), \
                patch.object(ContentFactoryRun.objects, "select_for_update") as runs:
            self.website.operations.select_for_update.return_value.filter.return_value = [self.op]
            runs.return_value.filter.return_value.first.return_value = self.run
            with self.assertRaises(WebsiteAuthorityError) as error:
                operations.reserve_workflow_operation(self.website, workflow="article_system_setup",
                    payload={"client_request_id": "fresh", "domain": "fixture.test"})
            self.assertEqual(error.exception.code, "website_operation_changed")
            self.op.save.assert_not_called()

    def test_cancelled_request_identity_cannot_be_reused(self):
        self.op.state = "cancelled"
        self.op.payload["request_digest"] = operations.evidence_digest({"domain": "fixture.test"})
        with patch.object(authority, "authority_guard", side_effect=lambda *a, **k: nullcontext()), \
                patch.object(operations.WebsiteConnectionOperation.objects, "get_or_create", return_value=(self.op, False)):
            self.website.operations.select_for_update.return_value.filter.return_value = []
            with self.assertRaises(WebsiteAuthorityError) as error:
                operations.reserve_workflow_operation(self.website, workflow="article_system_setup",
                    payload={"client_request_id": "old-request", "domain": "fixture.test"})
            self.assertEqual(error.exception.code, "operation_key_conflict")

    def test_binding_terminal_dispatch_response_never_marks_it_running(self):
        with patch.object(authority, "authority_guard", side_effect=lambda *a, **k: nullcontext()), \
                patch.object(authority, "contract_for", return_value=self.binding):
            for state in operations.TERMINAL_WORKFLOW_STATES:
                self.run.status = state
                operations.bind_operation_run(self.op, self.run)
                self.assertEqual(self.op.state, state)

    def test_observation_records_cancel_but_cannot_revive_or_replace_completion(self):
        with patch.object(operations.WebsiteConnectionOperation.objects, "select_for_update") as selected:
            selected.return_value.filter.return_value.first.return_value = self.op
            operations.observe_workflow_status(self.run)
            self.assertEqual(self.op.state, "cancelled")
            self.run.status = "running"
            operations.observe_workflow_status(self.run)
            self.assertEqual(self.op.state, "cancelled")
            self.op.state = "completed"
            self.run.status = "cancelled"
            operations.observe_workflow_status(self.run)
            self.assertEqual(self.op.state, "completed")

    def test_current_setup_failure_receipt_is_allowed_after_failure_mirror(self):
        for state in ["failed", "blocked"]:
            self.op.state = state
            for incoming in ["failed", "blocked"]:
                self.assertIs(operations.validate_operation(self.website, {**self.binding,
                    "status": incoming, "event_type": "article_system_setup_preview_failed"}), self.op)
        self.op.state = "completed"
        with self.assertRaises(WebsiteAuthorityError):
            operations.validate_operation(self.website, {**self.binding, "status": "failed",
                "event_type": "article_system_setup_preview_failed"})

    def test_terminal_failure_receipt_keeps_attempt_and_generation_fences(self):
        self.op.state = "failed"
        for field, value in [("operation_attempt", 1), ("deletion_epoch", 1)]:
            with self.subTest(field=field), self.assertRaises(WebsiteAuthorityError):
                operations.validate_operation(self.website, {**self.binding, field: value,
                    "event_type": "article_system_setup_preview_failed"})
        self.op.generation = 2
        with self.assertRaises(WebsiteAuthorityError):
            operations.validate_operation(self.website, {**self.binding, "event_type": "article_system_setup_preview_failed"})

    def test_cancellation_receipt_accepts_only_same_attempt_and_run(self):
        for state in ["running", "failed", "blocked", "cancelled"]:
            self.op.state = state
            payload = {**self.binding, "run_id": self.run.run_id, "status": "cancelled"}
            self.assertIs(operations.validate_operation(self.website, payload, cancellation_receipt=True), self.op)
            for changed in [{"run_id": "other-run"}, {"status": "running"}, {"operation_attempt": 1}, {"connection_generation": 2}]:
                with self.subTest(state=state, changed=changed), self.assertRaises(WebsiteAuthorityError):
                    operations.validate_operation(self.website, {**payload, **changed}, cancellation_receipt=True)
        self.op.state = "completed"
        with self.assertRaises(WebsiteAuthorityError):
            operations.validate_operation(self.website, payload, cancellation_receipt=True)

    def test_cancellation_snapshot_cannot_write_repository_configuration(self):
        handler = Mock()
        wrapped = authority.guarded_service_write("config_write", cancellation_receipts=True)(handler)
        request = SimpleNamespace(method="PUT", data={**self.binding, "status": "cancelled", "publish_targets": []})
        with patch.object(authority, "record_denied_terminal_callback", return_value=False):
            response = wrapped(None, request, run_id=self.run.run_id)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["code"], "cancellation_scope_mismatch")
        handler.assert_not_called()

    def test_cancelled_snapshot_records_receipt_without_config_write_or_revival(self):
        self.run.status = self.op.state = "failed"
        @contextmanager
        def guard(payload, *, action):
            self.assertEqual(action, "cancel_receipt")
            operations.validate_operation(self.website, payload, cancellation_receipt=True)
            yield self.website
        def handler(self_unused, request, **kwargs):
            self.run.status = request.data["status"]
            return Response({"status": self.run.status})
        wrapped = authority.guarded_service_write("config_write", cancellation_receipts=True)(handler)
        request = SimpleNamespace(method="PUT", data={**self.binding, "status": "cancelled"})
        self.run.refresh_from_db = Mock()
        with patch.object(authority, "authority_guard", side_effect=guard), \
                patch.object(authority, "record_scan_evidence") as scan, \
                patch.object(ContentFactoryRun.objects, "filter") as runs, \
                patch.object(operations.WebsiteConnectionOperation.objects, "select_for_update") as selected:
            runs.return_value.first.return_value = self.run
            selected.return_value.filter.return_value.first.return_value = self.op
            response = wrapped(None, request, run_id=self.run.run_id)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(self.run.status, "cancelled")
            self.assertEqual(self.op.state, "cancelled")
            scan.assert_not_called()
