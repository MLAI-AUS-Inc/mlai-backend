"""Actual authenticated callback-first admission and event routing boundaries."""
from copy import deepcopy

from django.test import TestCase, override_settings
from rest_framework.test import APIRequestFactory

from organizations.models import Organization
from workflow_runs.models import ContentFactoryRun, ContentFactoryRunStep, ContentFactoryRunStepAttempt

from .run_event_authority import event_digest
from .run_ownership import (
    DISPATCH_RESULT_KEY, EVENT_RESULT_KEY, WIRE_EXECUTION_KEY, WIRE_REQUEST_KEY,
    current_execution_ticket, reserve_admitted_run, user_principal,
)
from .service_views import ContentFactoryCallbackView


@override_settings(ARTICLES_RUN_OWNERSHIP_ENABLED=True, ARTICLES_RUN_FACT_AUTHORITY_ENABLED=True,
                   ARTICLES_RUN_EVENT_AUTHORITY_ENABLED=True, ROO_API_KEY="synthetic-r3-callback-key")
class AdmittedEventCallbackTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Synthetic callback owner", domain="callback-r3.example")
        self.request = {"domain": self.org.domain, "client_request_id": "callback-original-key", "topic": "Saved body"}
        self.run = reserve_admitted_run(organization=self.org, principal=user_principal(123, "user:123"),
                                       workflow="article_generation", payload=self.request)
        self.factory = APIRequestFactory()

    def envelope(self, *, run_id="callback-worker-id", seq=1, **fields):
        admission = deepcopy(self.request[WIRE_REQUEST_KEY])
        execution = current_execution_ticket(self.run)
        event = {"seq": seq, "generation": execution["generation"], "state_version": seq,
                 "at": "2026-11-03T00:00:00+00:00", "type": "run_state", "status": "running", **fields}
        event["digest"] = event_digest(run_id, admission, execution, event)
        return {"event": "run_events_v2", "schema_version": 1, "run_id": run_id,
                WIRE_REQUEST_KEY: admission, WIRE_EXECUTION_KEY: execution, "events": [event]}

    def receive(self, payload, *, authenticated=True):
        headers = {"HTTP_X_API_KEY": "synthetic-r3-callback-key"} if authenticated else {}
        request = self.factory.post("/internal/content-factory/callback", payload, format="json", **headers)
        return ContentFactoryCallbackView.as_view()(request)

    def test_callback_first_binds_and_projects_before_lost_http_ack_without_changing_identity_or_clocks(self):
        before = ContentFactoryRun.objects.values().get(pk=self.run.pk)
        payload = self.envelope()
        response = self.receive(payload)
        self.assertEqual(response.status_code, 200, response.data)
        self.run.refresh_from_db()
        self.assertEqual((self.run.run_id, self.run.status), ("callback-worker-id", "running"))
        after = ContentFactoryRun.objects.values().get(pk=self.run.pk)
        for field in before.keys() - {"run_id", "status", "result"}:
            self.assertEqual(after[field], before[field], field)
        self.assertEqual(ContentFactoryRun.objects.count(), 1)
        replay_before = deepcopy(after)
        replay = self.receive(payload)
        self.assertEqual((replay.status_code, replay.data["duplicates"]), (200, 1))
        self.assertEqual(ContentFactoryRun.objects.values().get(pk=self.run.pk), replay_before)

    def test_authentication_failure_has_no_binding_or_event_effect(self):
        before = ContentFactoryRun.objects.values().get(pk=self.run.pk)
        self.assertIn(self.receive(self.envelope(), authenticated=False).status_code, (401, 403))
        self.assertEqual(ContentFactoryRun.objects.values().get(pk=self.run.pk), before)

    def test_invalid_batch_and_forged_admission_or_ticket_refuse_before_binding(self):
        before = ContentFactoryRun.objects.values().get(pk=self.run.pk)
        for mode in ("digest", "tenant", "source", "generation", "bool_generation"):
            payload = self.envelope()
            if mode == "digest":
                payload["events"][0]["digest"] = "0" * 64
            elif mode == "tenant":
                payload[WIRE_REQUEST_KEY]["organization_id"] = "999"
            elif mode == "source":
                payload[WIRE_EXECUTION_KEY]["scope"]["expected_source_sha"] = "a" * 40
            else:
                value = True if mode == "bool_generation" else 4
                payload[WIRE_EXECUTION_KEY]["generation"] = value
                payload["events"][0]["generation"] = value
            if mode != "digest":
                payload["events"][0]["digest"] = event_digest(payload["run_id"], payload[WIRE_REQUEST_KEY],
                    payload[WIRE_EXECUTION_KEY], payload["events"][0])
            with self.subTest(mode=mode):
                self.assertIn(self.receive(payload).status_code, (400, 409))
                self.assertEqual(ContentFactoryRun.objects.values().get(pk=self.run.pk), before)

    def test_remote_identity_collision_preserves_both_rows_and_all_step_attempt_history(self):
        occupied = ContentFactoryRun.objects.create(run_id="callback-worker-id", workflow="article_generation",
                                                   result={"history": "legacy"})
        step = ContentFactoryRunStep.objects.create(run=occupied, step_key="kept")
        attempt = ContentFactoryRunStepAttempt.objects.create(step=step, attempt=1)
        before = list(ContentFactoryRun.objects.order_by("pk").values())
        self.assertEqual(self.receive(self.envelope()).status_code, 409)
        self.assertEqual(list(ContentFactoryRun.objects.order_by("pk").values()), before)
        self.assertTrue(ContentFactoryRunStepAttempt.objects.filter(pk=attempt.pk, step__run=occupied).exists())

    def test_conflicting_events_roll_back_callback_first_binding_and_every_projection(self):
        payload = self.envelope()
        changed = {**payload["events"][0], "status": "failed"}
        changed["digest"] = event_digest(payload["run_id"], payload[WIRE_REQUEST_KEY], payload[WIRE_EXECUTION_KEY], changed)
        payload["events"].append(changed)
        before = ContentFactoryRun.objects.values().get(pk=self.run.pk)
        self.assertEqual(self.receive(payload).status_code, 409)
        self.assertEqual(ContentFactoryRun.objects.values().get(pk=self.run.pk), before)
        self.assertFalse(self.run.steps.exists())

    def test_unknown_admission_cannot_create_or_adopt_a_run(self):
        payload = self.envelope()
        from .website_contract import evidence_digest
        payload[WIRE_REQUEST_KEY]["dispatch_key"] = "unknown-original-admission"
        ticket = payload[WIRE_EXECUTION_KEY]
        ticket["admission_digest"] = evidence_digest(payload[WIRE_REQUEST_KEY])
        ticket["execution_id"] = "admit:" + ticket["admission_digest"]
        payload["events"][0]["digest"] = event_digest(payload["run_id"], payload[WIRE_REQUEST_KEY], ticket, payload["events"][0])
        before = list(ContentFactoryRun.objects.values())
        self.assertEqual(self.receive(payload).status_code, 409)
        self.assertEqual(list(ContentFactoryRun.objects.values()), before)

    def test_exact_private_transport_hold_is_released_for_callback_first_event_only(self):
        ContentFactoryRun.objects.filter(pk=self.run.pk).update(status="blocked", current_step="dispatch_pending_resolution",
            result={**self.run.result, DISPATCH_RESULT_KEY: {"state": "pending_resolution"}})
        response = self.receive(self.envelope())
        self.assertEqual(response.status_code, 200, response.data)
        self.run.refresh_from_db()
        self.assertEqual(self.run.status, "running")
        self.assertIn(EVENT_RESULT_KEY, self.run.result)
