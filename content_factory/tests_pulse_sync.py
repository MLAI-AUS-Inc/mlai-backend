"""ORM/wire regressions for Pulse acknowledgement and changed-row persistence."""
import copy
import json
from pathlib import Path
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient
from workflow_runs.models import ContentFactoryRun
from organizations.models import Organization
from . import service_views as views


LEGACY_SYNC = (Path(__file__).parent / "test_fixtures/pulse_sync_main.py").read_text()


@override_settings(ROO_API_KEY="pulse-sync-synthetic-key")
class PulseSyncPersistenceTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Synthetic sync", domain="pulse-sync.invalid")
        self.client = APIClient()
        self.client.credentials(HTTP_X_API_KEY="pulse-sync-synthetic-key")
        self.sequence = json.loads((Path(__file__).parent / "test_fixtures/pulse_sync_sequence.json").read_text())

    def put(self, run_id, payload, *, ack=False):
        headers = {"HTTP_X_RUN_SYNC_RESPONSE": "ack"} if ack else {}
        return self.client.put(f"/api/content-factory/runs/{run_id}/", payload, format="json", **headers)

    def test_ack_and_legacy_responses_keep_their_wire_contracts(self):
        first = self.put("pulse-ack", self.sequence[0], ack=True)
        self.assertEqual(first.status_code, 201, first.data)
        keys = {"run_id", "workflow", "domain", "status", "current_step", "step_order", "run_request",
            "error", "resume_available", "created_at", "updated_at", "sync_status"}
        self.assertEqual(set(first.data), keys)
        full = self.put("pulse-ack", self.sequence[0])
        self.assertIn("result", full.data)
        self.assertIn("step_states", full.data)
        ContentFactoryRun.objects.filter(run_id="pulse-ack").update(status="completed")
        ignored = self.put("pulse-ack", self.sequence[0], ack=True)
        self.assertEqual(set(ignored.data), keys)
        self.assertEqual(ignored.data["sync_status"], "ignored_terminal_state")
        self.assertEqual(ignored.data["status"], "completed")
        ContentFactoryRun.objects.filter(run_id="pulse-ack").update(status="cancelled")
        cancelled = self.put("pulse-ack", self.sequence[0], ack=True)
        self.assertEqual(cancelled.status_code, 409)
        self.assertEqual(cancelled.data["error"], "run_cancelled")

    def test_full_response_includes_new_steps_and_attempts_and_omits_deleted_steps(self):
        self.assertEqual(self.put("pulse-full-relations", self.sequence[0]).status_code, 201)
        payload = copy.deepcopy(self.sequence[1])
        payload["step_order"].remove("relevance_classification")
        payload["step_states"].pop("relevance_classification")
        payload["step_order"].append("temporary_step")
        payload["step_states"]["temporary_step"] = {"status": "pending", "attempts": 0}
        response = self.put("pulse-full-relations", payload)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(set(response.data["step_states"]), {"gmail_backfill", "temporary_step"})
        self.assertEqual(
            [row["attempt"] for row in response.data["step_states"]["gmail_backfill"]["attempt_history"]],
            [1, 2],
        )
        saved = self.client.get("/api/content-factory/runs/pulse-full-relations/")
        self.assertEqual(response.data["step_states"], saved.data["step_states"])

    def test_identical_puts_with_truncated_attempt_history_write_no_step_or_attempt_rows(self):
        payload = copy.deepcopy(self.sequence[-1])
        payload["step_states"]["gmail_backfill"]["attempt_history"] = [
            {"attempt": i, "status": "completed", "message": f"Batch {i}"} for i in range(1, 21)]
        self.assertEqual(self.put("pulse-noop", payload).status_code, 201)
        payload["step_states"]["gmail_backfill"]["attempt_history"] = payload["step_states"]["gmail_backfill"]["attempt_history"][-10:]
        self.assertEqual(self.put("pulse-noop", payload).status_code, 200)
        with CaptureQueriesContext(connection) as queries:
            reply = self.put("pulse-noop", payload)
        self.assertEqual(reply.status_code, 200, reply.data)
        writes = [q["sql"] for q in queries.captured_queries if
            (q["sql"].startswith('UPDATE "content_factory_run_step') or
             q["sql"].startswith('INSERT INTO "content_factory_run_step'))]
        self.assertEqual(writes, [])
        run = ContentFactoryRun.objects.get(run_id="pulse-noop")
        self.assertEqual(run.steps.get(step_key="gmail_backfill").attempt_history.count(), 20)

    def rows(self, run_id):
        run = ContentFactoryRun.objects.get(run_id=run_id)
        core = {field.attname: getattr(run, field.attname) for field in run._meta.fields
            if field.name not in {"id", "run_id", "created_at", "updated_at"}}
        steps = []
        for step in run.steps.order_by("step_key"):
            state = {field.name: getattr(step, field.name) for field in step._meta.fields
                if field.name not in {"id", "run"}}
            state["attempt_history"] = [{field.name: getattr(attempt, field.name)
                for field in attempt._meta.fields if field.name not in {"id", "step", "created_at"}}
                for attempt in step.attempt_history.order_by("attempt")]
            steps.append(state)
        return core, steps

    def test_recorded_twenty_put_replay_matches_main_persistence_rows(self):
        namespace = vars(views).copy()
        exec(compile(LEGACY_SYNC, "main-persistence-oracle", "exec"), namespace)
        legacy = namespace["_sync_content_factory_run_snapshot"]
        for index, payload in enumerate(self.sequence):
            old = copy.deepcopy(payload)
            new = copy.deepcopy(payload)
            legacy(run_id="pulse-legacy", data=old, step_states=old["step_states"])
            response = self.put("pulse-current", new, ack=True)
            self.assertLess(response.status_code, 300, response.data)
            self.assertEqual(self.rows("pulse-current"), self.rows("pulse-legacy"), f"PUT {index+1}")
        self.assertFalse(ContentFactoryRun.objects.get(run_id="pulse-current").steps.filter(step_key="temporary_step").exists())
