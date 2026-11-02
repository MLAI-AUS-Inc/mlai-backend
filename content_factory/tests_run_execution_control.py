"""Real admission, permissions, original-request retention and ticket replay."""
from copy import deepcopy
from unittest.mock import patch

from django.db import transaction
from django.test import TestCase, override_settings
from rest_framework.test import APIRequestFactory

from roo.models import Ledger
from workflow_runs.models import ContentFactoryRun
from .self_heal_testing import ALL_FLAGS_ON, make_p2_org
from .run_ownership import (OWNER_EXECUTION_KEY, WIRE_REQUEST_KEY, WIRE_EXECUTION_KEY,
    current_execution_ticket, reserve_admitted_run, user_principal, wire_identity, owner_receipt)
from .run_execution_control import (
    BUDGET_KEY, CONTROL_KEY, ExecutionControlDenied, append_execution_ticket,
    current_execution_request, initial_control_budget, require_current_principal,
    validate_budget_observation, valid_control_budget,
)

FLAGS = {**ALL_FLAGS_ON, "ARTICLES_RUN_OWNERSHIP_ENABLED": True,
         "ARTICLES_RUN_FACT_AUTHORITY_ENABLED": True, "ARTICLES_RUN_EVENT_AUTHORITY_ENABLED": True}
BUDGET = {"schema_version": 1, "runtime": "article", "max_model_usd": "5.00", "max_model_tokens": 50000}


@override_settings(**FLAGS, ARTICLES_RUN_CONTROL_MODEL_USD="5.00", ARTICLES_RUN_CONTROL_MODEL_TOKENS=50000)
class ExecutionTicketTests(TestCase):
    def setUp(self):
        self.fixture = make_p2_org()
        self.org, self.user = self.fixture.organization, self.fixture.owner
        self.payload = {"domain": self.org.domain, "client_request_id": "control-original",
            "topic": "Keep this approved topic", "approved_body_sha256": "b" * 64}
        self.run = reserve_admitted_run(organization=self.org, workflow="article_generation", payload=self.payload,
            principal=user_principal(self.user.pk, f"mlai_user:{self.user.pk}"))
        self.original = ContentFactoryRun.objects.values().get(pk=self.run.pk)

    def append(self, key="a" * 64, **kwargs):
        with transaction.atomic():
            run = ContentFactoryRun.objects.select_for_update().get(pk=self.run.pk)
            require_current_principal(run)
            return append_execution_ticket(run, control_key=key, reason="quality_repair", **kwargs)

    def observation(self, **kwargs):
        self.run.refresh_from_db()
        return {"schema_version": 1, "run_id": self.run.run_id,
            WIRE_REQUEST_KEY: wire_identity(owner_receipt(self.run)), WIRE_EXECUTION_KEY: current_execution_ticket(self.run),
            BUDGET_KEY: deepcopy(BUDGET), "event_cursor": 0, "used_model_usd_upper_bound": "0.25",
            "used_model_tokens": 100, "uncertain_calls": 1, **kwargs}

    def test_append_and_lost_ack_retry_advance_once_with_original_request_charge_clocks(self):
        before_ledgers = list(Ledger.objects.values())
        first = self.append()
        second = self.append()
        self.assertEqual(first, second)
        self.assertEqual(first["generation"], 1)
        self.run.refresh_from_db()
        self.assertEqual(len(self.run.result[OWNER_EXECUTION_KEY]["tickets"]), 2)
        after = ContentFactoryRun.objects.values().get(pk=self.run.pk)
        for key, value in self.original.items():
            if key != "result":
                self.assertEqual(after[key], value, key)
        self.assertEqual(list(Ledger.objects.values()), before_ledgers)

    def test_internal_user_revision_is_one_retained_command_without_automatic_endpoint_admission(self):
        from .run_execution_control import AUTOMATIC_REASONS
        self.assertNotIn("user_revision", AUTOMATIC_REASONS)
        tickets = []
        for _ in range(2):
            with transaction.atomic():
                run = ContentFactoryRun.objects.select_for_update().get(pk=self.run.pk)
                require_current_principal(run)
                tickets.append(append_execution_ticket(run, control_key="d" * 64, reason="user_revision"))
        self.assertEqual(tickets[0], tickets[1])
        self.assertEqual(tickets[0]["generation"], 1)
        self.run.refresh_from_db()
        after = ContentFactoryRun.objects.values().get(pk=self.run.pk)
        for key, value in self.original.items():
            if key != "result":
                self.assertEqual(after[key], value, key)

    def test_pair_commit_before_receipt_replay_can_echo_the_issued_ticket(self):
        first = self.append()
        second = self.append(expected_execution_id=first["execution_id"])
        self.assertEqual(first, second)
        self.assertEqual(second["generation"], 1)

    def test_scope_ticket_changes_only_effective_source_and_preserves_original(self):
        scope = {**current_execution_ticket(self.run)["scope"], "expected_source_sha": "c" * 40,
                 "operation_attempt": 2}
        ticket = self.append(scope=scope, increment_generation=False)
        self.assertEqual(ticket["generation"], 0)
        self.run.refresh_from_db()
        self.assertEqual(self.run.run_request, self.original["run_request"])
        request = current_execution_request(self.run)
        self.assertEqual(request["expected_source_sha"], "c" * 40)
        self.assertEqual(request["operation_attempt"], 2)
        self.assertEqual(request["approved_body_sha256"], "b" * 64)
        self.assertNotEqual(ticket["request_digest"], owner_receipt(self.run)["identity"]["request_digest"])

    def test_changed_control_key_scope_or_parent_is_not_a_retry(self):
        initial = current_execution_ticket(self.run)
        self.append(expected_execution_id=initial["execution_id"])
        for kwargs in ({"scope": {**initial["scope"], "expected_source_sha": "d" * 40}},
                       {"expected_execution_id": "foreign"}, {"increment_generation": False}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ExecutionControlDenied):
                self.append(**kwargs)

    def test_original_user_cannot_be_replaced_by_billing_owner(self):
        self.user.is_active = False
        self.user.save(update_fields=["is_active"])
        with self.assertRaises(ExecutionControlDenied):
            self.append()
        self.assertEqual(ContentFactoryRun.objects.values().get(pk=self.run.pk), self.original)

    def test_frozen_startup_cannot_resume_even_for_its_owner(self):
        config = self.fixture.config
        config.pillar_strategy = {**config.pillar_strategy, "startup_team": {
            "version": 1, "owner_user_id": self.user.pk, "frozen": True, "members": []}}
        config.save(update_fields=["pillar_strategy"])
        with self.assertRaises(ExecutionControlDenied):
            self.append()

    def test_current_typed_transient_with_recent_budget_can_offer_explicit_resume_without_writes(self):
        from .run_execution_control import record_budget_observation
        from .run_user_resume import can_request_user_resume
        from .run_event_authority import receive_authoritative_run_events
        from .tests_run_event_authority import batch
        with transaction.atomic():
            record_budget_observation(self.run, self.observation(uncertain_calls=0))
        events = [{"status": "running"}, {"type": "failure", "failure": {"code": "worker_unavailable", "stage": "draft"}},
            {"status": "failed"}]
        self.assertEqual(receive_authoritative_run_events(batch(self.run, events)).status_code, 200)
        self.run.refresh_from_db()
        before = ContentFactoryRun.objects.values().get(pk=self.run.pk)
        self.assertFalse(self.run.resume_available)
        self.assertTrue(can_request_user_resume(self.run, user=self.user))
        self.assertEqual(ContentFactoryRun.objects.values().get(pk=self.run.pk), before)
        from django.utils import timezone
        from datetime import timedelta
        state = self.run.result[CONTROL_KEY]["budget_observation"]
        original = deepcopy(state)
        state["observed_at"] = (timezone.now()-timedelta(minutes=6)).isoformat()
        self.assertFalse(can_request_user_resume(self.run, user=self.user))
        state.update(deepcopy(original))
        state["payload"]["used_model_tokens"] = BUDGET["max_model_tokens"]
        self.assertFalse(can_request_user_resume(self.run, user=self.user))
        state.update(deepcopy(original))
        state["payload"]["uncertain_calls"] = 1
        self.assertFalse(can_request_user_resume(self.run, user=self.user))
        state.update(deepcopy(original))
        projection = self.run.result["run_owner_events_v1"]["projection"]
        projection["failure"]["code"] = "EDITORIAL_REJECTED"
        self.assertFalse(can_request_user_resume(self.run, user=self.user))

    def test_unknown_and_exhausted_budget_never_advances(self):
        for changes in ({"used_model_usd_upper_bound": None}, {"used_model_usd_upper_bound": "5.00"},
                        {"used_model_usd_upper_bound": "NaN"}, {"used_model_tokens": 50000},
                        {"event_cursor": True}, {"uncertain_calls": -1}, {BUDGET_KEY: {**BUDGET, "schema_version": True}},
                        {BUDGET_KEY: {**BUDGET, "max_model_usd": "99"}}):
            with self.subTest(changes=changes), self.assertRaises(ExecutionControlDenied):
                validate_budget_observation(self.run, self.observation(**changes))
        self.assertEqual(validate_budget_observation(self.run, self.observation()), 0)
        self.assertEqual(ContentFactoryRun.objects.values().get(pk=self.run.pk), self.original)

    def test_old_ticket_budget_cannot_grant_current_generation(self):
        observation = self.observation()
        self.append()
        self.run.refresh_from_db()
        from .run_ownership import RunOwnershipConflict
        with self.assertRaises(RunOwnershipConflict):
            validate_budget_observation(self.run, observation)

    def test_budget_high_water_refuses_lost_or_regressed_durable_ledger(self):
        from .run_execution_control import record_budget_observation
        with transaction.atomic():
            record_budget_observation(self.run, self.observation())
        self.run.refresh_from_db()
        for changes in ({"used_model_usd_upper_bound": "0.24"}, {"used_model_tokens": 99}):
            with self.subTest(changes=changes), self.assertRaises(ExecutionControlDenied):
                validate_budget_observation(self.run, self.observation(**changes))
        self.assertEqual(validate_budget_observation(self.run, self.observation()), 0)

    def test_caps_are_backend_only_off_by_default_and_never_nan_or_boolean(self):
        with override_settings(ARTICLES_RUN_CONTROL_MODEL_USD="0", ARTICLES_RUN_CONTROL_MODEL_TOKENS=0):
            self.assertIsNone(initial_control_budget("article_generation"))
        with override_settings(ARTICLES_RUN_CONTROL_MODEL_USD="5", ARTICLES_RUN_CONTROL_MODEL_TOKENS=50000,
                               ARTICLES_RUN_SDK_CONTROL_ENABLED=False):
            self.assertEqual(initial_control_budget("article_generation"), {**BUDGET, "max_model_usd": "5"})
            self.assertIsNone(initial_control_budget("article_system_setup"))
        for changes in ({"runtime": []}, {"max_model_usd": "NaN"}, {"max_model_tokens": True},
                        {"max_model_tokens": 20_000_001}, {"schema_version": True}):
            self.assertFalse(valid_control_budget({**BUDGET, **changes}))

    def test_service_endpoint_is_strictly_authenticated_and_precedes_legacy_action_route(self):
        from django.urls import resolve
        from .run_execution_control_views import RunExecutionTicketView
        match = resolve(f"/api/content-factory/runs/{self.run.run_id}/execution-ticket")
        self.assertIs(match.func.cls, RunExecutionTicketView)
        request = APIRequestFactory().post("/", {}, format="json")
        with patch("content_factory.run_execution_control_views._budget") as budget:
            response = RunExecutionTicketView.as_view()(request, run_id=self.run.run_id)
        self.assertEqual(response.status_code, 403)
        budget.assert_not_called()
        self.assertEqual(ContentFactoryRun.objects.values().get(pk=self.run.pk), self.original)

    @override_settings(INTERNAL_API_KEY="synthetic-control-service-key")
    def test_unbudgeted_original_refuses_without_http_even_if_later_caps_are_enabled(self):
        with override_settings(ARTICLES_RUN_CONTROL_MODEL_USD="0", ARTICLES_RUN_CONTROL_MODEL_TOKENS=0):
            raw = {"domain": self.org.domain, "client_request_id": "unbudgeted-original"}
            run = reserve_admitted_run(organization=self.org, workflow="article_generation", payload=raw,
                principal=user_principal(self.user.pk, f"mlai_user:{self.user.pk}"))
        from .run_execution_control_views import RunExecutionTicketView
        request = APIRequestFactory().post("/", {"schema_version": 1, "control_key": "c" * 64,
            "reason": "quality_repair", WIRE_REQUEST_KEY: wire_identity(owner_receipt(run)),
            WIRE_EXECUTION_KEY: current_execution_ticket(run)}, format="json",
            HTTP_X_API_KEY="synthetic-control-service-key")
        before = ContentFactoryRun.objects.values().get(pk=run.pk)
        with patch("requests.get") as get:
            response = RunExecutionTicketView.as_view()(request, run_id=run.run_id)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data, {"reason": "execution_budget_unavailable", "retryable": False})
        get.assert_not_called()
        self.assertEqual(ContentFactoryRun.objects.values().get(pk=run.pk), before)
