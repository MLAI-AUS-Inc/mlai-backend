"""Real row-lock/transaction proofs for the default-off admitted event protocol."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Barrier
from unittest import skipUnless
from unittest.mock import patch

from django.db import close_old_connections, connection
from django.test import TestCase, TransactionTestCase, override_settings

from organizations.models import Organization
from workflow_runs.models import ContentFactoryRun, ContentFactoryRunStep, ContentFactoryRunStepAttempt
from .run_event_authority import event_attempt_identity, event_digest, receive_authoritative_run_events
from .run_ownership import (
    EVENT_RESULT_KEY, OWNER_EXECUTION_KEY, WIRE_EXECUTION_KEY, WIRE_REQUEST_KEY,
    bind_admitted_run, current_execution_ticket, reserve_admitted_run, user_principal,
)

FLAGS = {"ARTICLES_RUN_OWNERSHIP_ENABLED": True, "ARTICLES_RUN_FACT_AUTHORITY_ENABLED": True,
         "ARTICLES_RUN_EVENT_AUTHORITY_ENABLED": True}


def admit(org, key="event-key"):
    raw = {"domain": org.domain, "client_request_id": key, "topic": "Original retained body"}
    run = reserve_admitted_run(organization=org, principal=user_principal(123, "user:123"),
                               workflow="article_generation", payload=raw)
    return bind_admitted_run(run, f"worker-{key}")


def batch(run, rows, ticket=None):
    admission = deepcopy(run.run_request[WIRE_REQUEST_KEY])
    execution = deepcopy(ticket or current_execution_ticket(run))
    events = []
    for index, fields in enumerate(rows, 1):
        event = {"seq": index, "generation": execution["generation"], "state_version": index,
                 "type": "run_state", "at": "2026-11-03T00:00:00+00:00", **fields}
        event["digest"] = event_digest(run.run_id, admission, execution, event)
        events.append(event)
    return {"event": "run_events_v2", "schema_version": 1, "run_id": run.run_id,
            WIRE_REQUEST_KEY: admission, WIRE_EXECUTION_KEY: execution, "events": events}


def advance_ticket(run, *, generation=0):
    # A synthetic trusted backend control grant; no worker admission is inferred.
    result = deepcopy(run.result)
    ticket = current_execution_ticket(run)
    ticket.update(execution_id="backend-resume-ticket", generation=generation, request_digest="b" * 64,
                  scope={**ticket["scope"], "expected_source_sha": "b" * 40})
    result[OWNER_EXECUTION_KEY]["tickets"][ticket["execution_id"]] = ticket
    result[OWNER_EXECUTION_KEY]["current_execution_id"] = ticket["execution_id"]
    ContentFactoryRun.objects.filter(pk=run.pk).update(result=result)
    run.refresh_from_db()
    return ticket


@override_settings(**FLAGS)
class EventAuthorityTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Synthetic event owner", domain="events.example")
        self.run = admit(self.org)

    def receive(self, rows, ticket=None):
        response = receive_authoritative_run_events(batch(self.run, rows, ticket))
        self.run.refresh_from_db()
        return response

    def test_current_events_project_once_preserving_body_authority_billing_and_all_clocks(self):
        before = ContentFactoryRun.objects.values().get(pk=self.run.pk)
        rows = [{"status": "running"}, {"type": "step_started", "step": "write", "attempt": 1},
                {"type": "step_done", "step": "write", "attempt": 1, "status": "completed"}]
        response = self.receive(rows)
        self.assertEqual((response.status_code, response.data["accepted"]), (200, 3))
        self.assertEqual((self.run.status, self.run.current_step), ("running", "write"))
        step = self.run.steps.get(step_key="write")
        self.assertEqual((step.status, step.attempts), ("completed", 1))
        after = ContentFactoryRun.objects.values().get(pk=self.run.pk)
        for field in ("run_request", "organization_id", "domain", "github_repo", "slack_user_id",
                      "billing_amount", "billing_status", "billing_ledger_id", "billing_source_run_id",
                      "approval_state", "created_at", "updated_at", "last_event_emitted_at", "reconciled_at"):
            self.assertEqual(after[field], before[field], field)
        snapshot = deepcopy(after)
        response = self.receive(rows)
        self.assertEqual((response.data["accepted"], response.data["duplicates"]), (0, 3))
        self.assertEqual(ContentFactoryRun.objects.values().get(pk=self.run.pk), snapshot)

    def test_missing_or_legacy_or_reservation_only_run_never_gains_event_authority(self):
        raw = batch(self.run, [{"status": "running"}])
        raw["run_id"] = "missing"
        # Re-sign so the missing-run check, not a digest error, is exercised.
        raw["events"][0]["digest"] = event_digest("missing", raw[WIRE_REQUEST_KEY], raw[WIRE_EXECUTION_KEY], raw["events"][0])
        response = receive_authoritative_run_events(raw)
        self.assertEqual((response.status_code, response.data["retryable"]), (409, True))
        self.assertEqual(ContentFactoryRun.objects.count(), 1)
        legacy = ContentFactoryRun.objects.create(run_id="legacy", workflow="article_generation", domain=self.org.domain)
        raw["run_id"] = legacy.run_id
        raw["events"][0]["digest"] = event_digest(legacy.run_id, raw[WIRE_REQUEST_KEY], raw[WIRE_EXECUTION_KEY], raw["events"][0])
        self.assertEqual(receive_authoritative_run_events(raw).status_code, 409)
        with override_settings(ARTICLES_RUN_EVENT_AUTHORITY_ENABLED=False):
            reserved = admit(self.org, "reservation-only")
        raw = batch(reserved, [{"status": "running"}])
        self.assertIn(receive_authoritative_run_events(raw).status_code, (400, 409))
        reserved.refresh_from_db()
        self.assertNotIn(EVENT_RESULT_KEY, reserved.result)

    def test_forged_tenant_source_principal_ticket_or_generation_has_no_effect(self):
        before = ContentFactoryRun.objects.values().get(pk=self.run.pk)
        for target, field, value in ((WIRE_REQUEST_KEY, "organization_id", "999"),
                (WIRE_REQUEST_KEY, "workflow", "auto_discovery"),
                (WIRE_EXECUTION_KEY, "execution_id", "invented"),
                (WIRE_EXECUTION_KEY, "generation", 7)):
            raw = batch(self.run, [{"status": "completed"}])
            raw[target][field] = value
            raw["events"][0]["digest"] = event_digest(self.run.run_id, raw[WIRE_REQUEST_KEY], raw[WIRE_EXECUTION_KEY], raw["events"][0])
            with self.subTest(field=field):
                self.assertIn(receive_authoritative_run_events(raw).status_code, (400, 409))
                self.assertEqual(ContentFactoryRun.objects.values().get(pk=self.run.pk), before)
        for target, path, value in ((WIRE_REQUEST_KEY, "principal", {"kind": "user", "user_id": "999", "actor_id": "other"}),
                (WIRE_EXECUTION_KEY, "scope", {**current_execution_ticket(self.run)["scope"], "expected_source_sha": "f" * 40})):
            raw = batch(self.run, [{"status": "completed"}])
            raw[target][path] = value
            raw["events"][0]["digest"] = event_digest(self.run.run_id, raw[WIRE_REQUEST_KEY], raw[WIRE_EXECUTION_KEY], raw["events"][0])
            self.assertEqual(receive_authoritative_run_events(raw).status_code, 409)
            self.assertEqual(ContentFactoryRun.objects.values().get(pk=self.run.pk), before)

    def test_disabling_new_admission_does_not_abandon_a_previously_owned_execution(self):
        with override_settings(ARTICLES_RUN_OWNERSHIP_ENABLED=False, ARTICLES_RUN_FACT_AUTHORITY_ENABLED=False,
                               ARTICLES_RUN_EVENT_AUTHORITY_ENABLED=False):
            response = self.receive([{"status": "running"}])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.run.status, "running")

    def test_events_do_not_recreate_254_historical_jobs_or_mint_delivery_or_money(self):
        from .models import ContentFactoryJob
        from roo.models import Ledger
        ContentFactoryJob.objects.bulk_create([ContentFactoryJob(job_id=f"synthetic-history-{n}",
            slack_user_id="U_HISTORY", domain="historical.example", status="queued") for n in range(254)])
        before_jobs = list(ContentFactoryJob.objects.order_by("pk").values())
        before_money = Ledger.objects.count()
        response = self.receive([{"status": "completed"}])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(ContentFactoryRun.objects.count(), 1)
        self.assertEqual(list(ContentFactoryJob.objects.order_by("pk").values()), before_jobs)
        self.assertEqual(Ledger.objects.count(), before_money)
        self.assertNotIn("deploy_v1", self.run.result)
        self.assertNotIn("article_approval_v1", self.run.result)

    def test_failure_diagnostic_drift_is_not_a_pin_or_status_transition(self):
        response = self.receive([{"type": "failure", "failure": {"code": "NEW_WORKER_CAUSE", "stage": "write",
            "registry_version": 9999, "registry_sha256": "b" * 64}}])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.run.status, "queued")
        self.assertNotIn("failure", self.run.result)
        view = self.run.result[EVENT_RESULT_KEY]["projection"]["failure"]
        self.assertNotIn("last_projected_at", self.run.result[EVENT_RESULT_KEY])
        self.assertEqual(view["code"], "UNREGISTERED_FAILURE_CODE")
        self.assertNotIn("NEW_WORKER_CAUSE", view["user_message"])

    def test_conflicting_replay_and_conflicting_duplicate_in_one_batch_are_atomic(self):
        self.receive([{"status": "running"}])
        before = ContentFactoryRun.objects.values().get(pk=self.run.pk)
        self.assertEqual(self.receive([{"status": "completed"}]).status_code, 409)
        self.assertEqual(ContentFactoryRun.objects.values().get(pk=self.run.pk), before)
        other = admit(self.org, "atomic-conflict")
        raw = batch(other, [{"seq": 1, "status": "running"}, {"seq": 1, "status": "failed"}])
        self.assertEqual(receive_authoritative_run_events(raw).status_code, 409)
        other.refresh_from_db()
        self.assertNotIn(EVENT_RESULT_KEY, other.result)
        self.assertFalse(other.steps.exists())

    def test_new_ticket_attempt_one_has_a_new_ordinal_without_rewriting_old_history(self):
        self.receive([{"type": "step_done", "step": "write", "attempt": 1, "status": "failed"}])
        original = ContentFactoryRunStepAttempt.objects.values().get()
        old_ticket = current_execution_ticket(self.run)
        new_ticket = advance_ticket(self.run)
        response = self.receive([{"type": "step_started", "step": "write", "attempt": 1}], new_ticket)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(ContentFactoryRunStepAttempt.objects.values().get(pk=original["id"]), original)
        step = self.run.steps.get(step_key="write")
        self.assertEqual(step.attempts, 1, "reported attempts never become display ordinals")
        self.assertEqual(list(step.attempt_history.values_list("attempt", flat=True)), [1, 2])
        self.assertEqual(event_attempt_identity(self.run, "write", 2), {
            "execution_id": new_ticket["execution_id"], "generation": 0, "reported_attempt": 1})
        current = deepcopy(ContentFactoryRun.objects.values().get(pk=self.run.pk))
        response = self.receive([{"seq": 99, "status": "completed"}], old_ticket)
        self.assertEqual(response.status_code, 200)
        self.assertEqual((self.run.status, self.run.current_step), (current["status"], current["current_step"]))
        self.assertEqual(ContentFactoryRunStepAttempt.objects.count(), 2)

        self.receive([{"type": "step_started", "step": "write", "attempt": 1}], new_ticket)
        self.assertEqual(ContentFactoryRunStepAttempt.objects.count(), 2)

    def test_unattempted_skip_does_not_fabricate_a_worker_attempt(self):
        response = self.receive([{"type": "step_done", "step": "optional", "attempt": 0, "status": "skipped"}])
        self.assertEqual(response.status_code, 200)
        step = self.run.steps.get(step_key="optional")
        self.assertEqual((step.status, step.attempts), ("skipped", 0))
        self.assertEqual(event_attempt_identity(self.run, "optional", 1)["reported_attempt"], 0)

    def test_local_terminal_and_backend_soft_stop_are_not_revived(self):
        self.receive([{"status": "running"}])
        for status in ("completed", "cancelled", "denied", "failed", "blocked"):
            ContentFactoryRun.objects.filter(pk=self.run.pk).update(status=status, current_step="backend-stop")
            self.run.refresh_from_db()
            response = self.receive([{"seq": 20 + len(status), "status": "running"}])
            self.assertIn(response.status_code, (200, 409))
            self.assertEqual((self.run.status, self.run.current_step), (status, "backend-stop"))

    def test_backend_failure_blocks_same_ticket_but_new_authorized_ticket_can_recover(self):
        self.receive([{"status": "running"}])
        ContentFactoryRun.objects.filter(pk=self.run.pk).update(status="failed", current_step="backend-stop")
        self.run.refresh_from_db()
        self.receive([{"seq": 100, "status": "running"}])
        self.assertEqual((self.run.status, self.run.current_step), ("failed", "backend-stop"))
        ticket = advance_ticket(self.run)
        self.assertEqual(self.receive([{"status": "running"}], ticket).status_code, 200)
        self.assertEqual(self.run.status, "running")

    def test_scope_aba_with_same_generation_requires_the_new_exact_ticket(self):
        self.receive([{"status": "running"}])
        first = current_execution_ticket(self.run)
        advance_ticket(self.run)
        result = deepcopy(self.run.result)
        third = {**deepcopy(first), "execution_id": "backend-scope-aba-ticket", "request_digest": "c" * 64}
        result[OWNER_EXECUTION_KEY]["tickets"][third["execution_id"]] = third
        result[OWNER_EXECUTION_KEY]["current_execution_id"] = third["execution_id"]
        ContentFactoryRun.objects.filter(pk=self.run.pk).update(result=result)
        self.run.refresh_from_db()
        self.assertEqual(first["scope"], third["scope"])
        self.assertEqual(first["generation"], third["generation"])
        self.receive([{"seq": 77, "status": "completed"}], first)
        self.assertEqual(self.run.status, "running")
        self.receive([{"status": "running"}], third)
        self.assertEqual(self.run.result[EVENT_RESULT_KEY]["projection"]["execution_id"], third["execution_id"])

    def test_transaction_failure_rolls_back_receipt_and_projection_and_retry_uses_same_event(self):
        raw = batch(self.run, [{"type": "step_started", "step": "write", "attempt": 1}])
        before = ContentFactoryRun.objects.values().get(pk=self.run.pk)
        with patch("content_factory.run_event_authority._project_steps", side_effect=RuntimeError("synthetic rollback")):
            with self.assertRaises(RuntimeError):
                receive_authoritative_run_events(raw)
        self.assertEqual(ContentFactoryRun.objects.values().get(pk=self.run.pk), before)
        self.assertFalse(self.run.steps.exists())
        self.assertEqual(receive_authoritative_run_events(raw).data["accepted"], 1)
        self.assertEqual(receive_authoritative_run_events(raw).data["accepted"], 0)


@override_settings(**FLAGS)
@skipUnless(connection.vendor == "postgresql", "Real PostgreSQL row locking is required")
class EventConcurrencyTests(TransactionTestCase):
    def test_two_deliveries_append_and_project_once(self):
        org = Organization.objects.create(name="Concurrent synthetic events", domain="concurrent-events.example")
        run = admit(org)
        raw = batch(run, [{"type": "step_done", "step": "write", "attempt": 1, "status": "completed"}])
        barrier = Barrier(2)
        def send():
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                response = receive_authoritative_run_events(deepcopy(raw))
                return response.status_code, response.data
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: send(), range(2)))
        self.assertEqual([row[0] for row in results], [200, 200])
        self.assertEqual(sorted(row[1]["accepted"] for row in results), [0, 1])
        run.refresh_from_db()
        self.assertEqual(len(run.result[EVENT_RESULT_KEY]["events"]), 1)
        self.assertEqual(ContentFactoryRunStepAttempt.objects.count(), 1)

    def test_concurrent_different_content_for_one_identity_has_one_winner(self):
        org = Organization.objects.create(name="Conflicting synthetic events", domain="conflicting-events.example")
        run = admit(org)
        barrier = Barrier(2)
        def send(status):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                return receive_authoritative_run_events(batch(run, [{"status": status}])).status_code
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(send, ["running", "failed"]))
        self.assertEqual(sorted(results), [200, 409])
        run.refresh_from_db()
        self.assertEqual(len(run.result[EVENT_RESULT_KEY]["events"]), 1)
