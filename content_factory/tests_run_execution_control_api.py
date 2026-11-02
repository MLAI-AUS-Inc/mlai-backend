"""Authenticated control API with real admission, history, reducer and grants.

Only external requests.get is replaced. No authorization, reconciliation,
ticket, budget-validation or event-receiver helper is stubbed.
"""
from copy import deepcopy
import json
from unittest.mock import Mock, patch

from django.db import connection, transaction
from django.test import TransactionTestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from core.models import User
from roo.models import Ledger
from workflow_runs.models import ContentFactoryRun
from .run_event_authority import receive_authoritative_run_events
from .run_execution_control import BUDGET_KEY, CONTROL_KEY
from .run_ownership import (
    EVENT_RESULT_KEY, OWNER_EXECUTION_KEY, RECONCILIATION_RESULT_KEY,
    WIRE_REQUEST_KEY, WIRE_EXECUTION_KEY, bind_admitted_run, current_execution_ticket,
    owner_receipt, reserve_admitted_run, scheduler_principal, user_principal, wire_identity,
)
from .run_reconciliation import _claim
from .self_heal_testing import make_p2_org
from .tests_run_event_authority import batch
from .tests_run_execution_control import FLAGS


def http(body, *, status=200):
    data = json.dumps(body).encode()
    return Mock(status_code=status, iter_content=Mock(return_value=[data]))


@override_settings(**FLAGS, CONTENT_FACTORY_URL="https://cf-control.fixture.invalid",
                   CONTENT_FACTORY_API_KEY="synthetic-cf-read-key", ROO_API_KEY="synthetic-control-api-key",
                   ARTICLES_RUN_CONTROL_MODEL_USD="5.00", ARTICLES_RUN_CONTROL_MODEL_TOKENS=50000)
class ExecutionControlApiTests(TransactionTestCase):
    def setUp(self):
        self.fixture = make_p2_org()
        self.org, self.user = self.fixture.organization, self.fixture.owner
        self.client = APIClient()
        self.client.credentials(HTTP_X_API_KEY="synthetic-control-api-key")
        self.sequence = 0
        self.admit()

    def admit(self, *, principal=None):
        self.sequence += 1
        raw = {"domain": self.org.domain, "client_request_id": f"api-control-{self.sequence}",
               "topic": "Keep the original approved article", "approved_body_sha256": "b" * 64}
        self.run = reserve_admitted_run(organization=self.org, workflow="article_generation", payload=raw,
            principal=principal or user_principal(self.user.pk, f"mlai_user:{self.user.pk}"))
        self.run = bind_admitted_run(self.run, f"actual-api-worker-{self.sequence}")
        self.journal = [batch(self.run, [{"status": "running", "step": "write"}])]
        self.budget_changes = {}
        self.history_changes = {}
        self.before_budget = self.before_history = None
        self.budget_status = self.history_status = 200

    def body(self, *, key="a" * 64):
        self.run.refresh_from_db()
        return {"schema_version": 1, "control_key": key, "reason": "quality_repair",
            WIRE_REQUEST_KEY: wire_identity(owner_receipt(self.run)), WIRE_EXECUTION_KEY: current_execution_ticket(self.run)}

    def budget(self):
        self.run.refresh_from_db()
        cursor = max([0, *(row["seq"] for raw in self.journal for row in raw["events"])])
        return {"schema_version": 1, "run_id": self.run.run_id,
            WIRE_REQUEST_KEY: wire_identity(owner_receipt(self.run)), WIRE_EXECUTION_KEY: current_execution_ticket(self.run),
            BUDGET_KEY: deepcopy(self.run.run_request[BUDGET_KEY]), "event_cursor": cursor,
            "used_model_usd_upper_bound": "0.25", "used_model_tokens": 100, "uncertain_calls": 1,
            **deepcopy(self.budget_changes)}

    def history(self, cursor):
        rows = []
        for raw in self.journal:
            kept = [deepcopy(row) for row in raw["events"] if row["seq"] > cursor]
            if kept:
                rows.append({**deepcopy(raw), "events": kept})
        end = max([cursor, *(row["seq"] for raw in rows for row in raw["events"])])
        return {"schema_version": 1, "run_id": self.run.run_id, "batches": rows,
                "next_cursor": end, "has_more": False, **deepcopy(self.history_changes)}

    def get(self, url, **kwargs):
        self.assertFalse(connection.in_atomic_block, "Provider HTTP must precede all database locks")
        self.assertEqual(kwargs["headers"]["X-API-KEY"], "synthetic-cf-read-key")
        self.assertFalse(kwargs["allow_redirects"])
        self.assertTrue(kwargs["stream"])
        if url.endswith("/execution-budget"):
            if self.before_budget:
                self.before_budget()
            return http(self.budget(), status=self.budget_status)
        if url.endswith("/events"):
            if self.before_history:
                self.before_history()
            return http(self.history(kwargs["params"]["after_seq"]), status=self.history_status)
        raise AssertionError("Unexpected external GET; no real provider may be called")

    def post(self, body=None):
        return self.client.post(f"/api/content-factory/runs/{self.run.run_id}/execution-ticket",
                                self.body() if body is None else body, format="json")

    def assert_no_grant(self):
        self.run.refresh_from_db()
        self.assertEqual(current_execution_ticket(self.run)["generation"], 0)
        self.assertEqual(len(self.run.result[OWNER_EXECUTION_KEY]["tickets"]), 1)
        self.assertFalse(self.run.result.get(CONTROL_KEY, {}).get("grants"))

    def test_positive_authenticated_grant_lost_ack_and_duplicate_use_one_ticket_and_original_spend(self):
        request = self.body()
        before = ContentFactoryRun.objects.values().get(pk=self.run.pk)
        ledgers = list(Ledger.objects.order_by("pk").values())
        with patch("requests.get", side_effect=self.get) as get:
            first = self.post(request)
            self.assertEqual(first.status_code, 200, getattr(first, "data", None))
            self.assertEqual(get.call_count, 2)
        self.assertEqual(set(first.data), {"schema_version", WIRE_REQUEST_KEY, WIRE_EXECUTION_KEY})
        self.assertEqual(first.data[WIRE_EXECUTION_KEY]["generation"], 1)
        with patch("requests.get", side_effect=self.get) as get:
            retry = self.post(request)  # The original caller lost the first acknowledgement.
        self.assertEqual(retry.status_code, 200, retry.data)
        self.assertEqual(retry.data, first.data)
        get.assert_not_called()
        self.run.refresh_from_db()
        self.assertEqual(len(self.run.result[OWNER_EXECUTION_KEY]["tickets"]), 2)
        self.assertEqual(self.run.result[EVENT_RESULT_KEY]["projection"]["status"], "running")
        after = ContentFactoryRun.objects.values().get(pk=self.run.pk)
        for field in before.keys() - {"result", "status", "current_step"}:
            self.assertEqual(after[field], before[field], field)
        self.assertEqual(list(Ledger.objects.order_by("pk").values()), ledgers)

    def test_absent_or_wrong_service_key_performs_zero_http_or_run_mutation(self):
        before = ContentFactoryRun.objects.values().get(pk=self.run.pk)
        for key in (None, "incorrect-synthetic-key"):
            self.client.credentials(**({"HTTP_X_API_KEY": key} if key else {}))
            with patch("requests.get", side_effect=self.get) as get:
                result = self.post()
            self.assertEqual(result.status_code, 403)
            get.assert_not_called()
        self.assertEqual(ContentFactoryRun.objects.values().get(pk=self.run.pk), before)

    def test_wrong_tenant_principal_source_ticket_or_request_shape_is_rejected_before_http(self):
        original = self.body()
        changes = []
        changes.append({})
        for field, value in (("organization_id", "999999"), ("principal", {"kind": "user", "user_id": "999999", "actor_id": "mlai_user:999999"})):
            body = deepcopy(original)
            body[WIRE_REQUEST_KEY][field] = value
            changes.append(body)
        body = deepcopy(original)
        body[WIRE_EXECUTION_KEY]["scope"]["source_run_id"] = "another-run"
        changes.append(body)
        for fields in ({"reason": "user_resume"}, {"schema_version": True}, {"control_key": "not-a-digest"}, {"approval_state": "approved"}):
            changes.append({**deepcopy(original), **fields})
        before = ContentFactoryRun.objects.values().get(pk=self.run.pk)
        for body in changes:
            with self.subTest(body=body), patch("requests.get", side_effect=self.get) as get:
                self.assertEqual(self.post(body).status_code, 409)
                get.assert_not_called()
        self.assertEqual(ContentFactoryRun.objects.values().get(pk=self.run.pk), before)

    def test_approval_required_or_denied_cannot_grant_even_when_worker_label_is_running(self):
        for approval in ("approval_required", "denied"):
            ContentFactoryRun.objects.filter(pk=self.run.pk).update(status="running", approval_state=approval)
            before = ContentFactoryRun.objects.values().get(pk=self.run.pk)
            with self.subTest(approval=approval), patch("requests.get", side_effect=self.get) as get:
                result = self.post()
                self.assertEqual(result.status_code, 409)
                get.assert_not_called()
            self.assertEqual(ContentFactoryRun.objects.values().get(pk=self.run.pk), before)

    def test_empty_reservation_without_current_lifecycle_projection_cannot_grant(self):
        self.journal = []
        with patch("requests.get", side_effect=self.get):
            result = self.post()
        self.assertEqual(result.status_code, 409)
        self.assertEqual(result.data["reason"], "execution_history_pending")
        self.assert_no_grant()

    def test_boolean_budget_echo_version_is_not_integer_one(self):
        self.budget_changes[BUDGET_KEY] = {**self.run.run_request[BUDGET_KEY], "schema_version": True}
        with patch("requests.get", side_effect=self.get):
            result = self.post()
        self.assertEqual(result.status_code, 409)
        self.assertEqual(result.data["reason"], "execution_budget_unavailable")
        self.assert_no_grant()

    def test_every_awaiting_or_terminal_status_refuses_automatic_control_before_http(self):
        from .run_status_contract import RUN_STATUSES, PLATFORM_EXTENSIONS
        for status in sorted((RUN_STATUSES | PLATFORM_EXTENSIONS) - {"queued", "running"}):
            ContentFactoryRun.objects.filter(pk=self.run.pk).update(status=status)
            with self.subTest(status=status), patch("requests.get", side_effect=self.get) as get:
                self.assertEqual(self.post().status_code, 409)
                get.assert_not_called()
        self.assert_no_grant()

    def test_inactive_or_frozen_current_owner_refuses_before_http(self):
        User.objects.filter(pk=self.user.pk).update(is_active=False)
        with patch("requests.get", side_effect=self.get) as get:
            self.assertEqual(self.post().status_code, 409)
        get.assert_not_called()
        User.objects.filter(pk=self.user.pk).update(is_active=True)
        config = self.fixture.config
        config.pillar_strategy = {**config.pillar_strategy, "startup_team": {
            "version": 1, "owner_user_id": self.user.pk, "frozen": True, "members": []}}
        config.save(update_fields=["pillar_strategy"])
        with patch("requests.get", side_effect=self.get) as get:
            self.assertEqual(self.post().status_code, 409)
        get.assert_not_called()
        self.assert_no_grant()

    def test_original_nonowner_or_removed_member_cannot_be_replaced_by_company_owner(self):
        member = User.objects.create_user(email="removed-member@synthetic.example.test")
        config = self.fixture.config
        roster = {"version": 1, "owner_user_id": self.user.pk, "frozen": False,
                  "members": [{"user_id": member.pk, "state": "active", "rights": {}}]}
        config.pillar_strategy = {**config.pillar_strategy, "startup_team": roster}
        config.save(update_fields=["pillar_strategy"])
        self.admit(principal=user_principal(member.pk, f"mlai_user:{member.pk}"))
        roster["members"] = []
        config.pillar_strategy["startup_team"] = roster
        config.save(update_fields=["pillar_strategy"])
        with patch("requests.get", side_effect=self.get) as get:
            self.assertEqual(self.post().status_code, 409)
        get.assert_not_called()
        self.assert_no_grant()

    def test_scheduler_principal_fails_closed_without_service_specific_authority(self):
        self.admit(principal=scheduler_principal("daily_discovery"))
        with patch("requests.get", side_effect=self.get) as get:
            self.assertEqual(self.post().status_code, 409)
        get.assert_not_called()
        self.assert_no_grant()

    def test_private_needs_user_unsupported_and_waiting_failures_refuse_before_http(self):
        from .website_failure_contract import load_registry
        registry = load_registry()["by_code"]
        for kind in ("needs_user", "unsupported", "waiting"):
            self.admit()
            code = next(code for code, value in registry.items() if value["failure_class"] == kind)
            raw = batch(self.run, [{"status": "running"}, {"type": "failure", "failure": {"code": code, "stage": "write"}}])
            self.assertEqual(receive_authoritative_run_events(raw).status_code, 200)
            self.run.refresh_from_db()
            self.assertNotIn("failure", self.run.result)  # Running presentation never hides the private cause from control.
            with self.subTest(kind=kind), patch("requests.get", side_effect=self.get) as get:
                result = self.post()
                self.assertEqual((result.status_code, result.data["reason"]), (409, "execution_requires_review"))
                get.assert_not_called()
            self.assert_no_grant()

    def test_approval_or_owner_freeze_during_http_is_rechecked_before_grant(self):
        def deny():
            ContentFactoryRun.objects.filter(pk=self.run.pk).update(approval_state="denied")
        self.before_history = deny
        with patch("requests.get", side_effect=self.get) as get:
            self.assertEqual(self.post().status_code, 409)
        self.assertEqual(get.call_count, 2)
        self.assert_no_grant()
        self.admit()

        def freeze():
            config = self.fixture.config
            config.pillar_strategy = {**config.pillar_strategy, "startup_team": {
                "version": 1, "owner_user_id": self.user.pk, "frozen": True, "members": []}}
            config.save(update_fields=["pillar_strategy"])
        self.before_history = freeze
        with patch("requests.get", side_effect=self.get):
            self.assertEqual(self.post().status_code, 409)
        self.assert_no_grant()

    def test_unknown_exhausted_malformed_or_wrong_ticket_budget_never_grants(self):
        changes = ({"used_model_usd_upper_bound": None}, {"used_model_usd_upper_bound": "5.00"},
                   {"used_model_usd_upper_bound": "NaN"}, {"used_model_tokens": 50000},
                   {"used_model_tokens": True}, {"uncertain_calls": -1}, {"event_cursor": True})
        for change in changes:
            self.admit()
            self.budget_changes = change
            with self.subTest(change=change), patch("requests.get", side_effect=self.get):
                self.assertEqual(self.post().status_code, 409)
            self.assert_no_grant()
        self.admit()
        self.budget_changes[WIRE_EXECUTION_KEY] = {**current_execution_ticket(self.run), "execution_id": "foreign-ticket"}
        with patch("requests.get", side_effect=self.get):
            self.assertEqual(self.post().status_code, 409)
        self.assert_no_grant()

    def test_unavailable_budget_does_not_even_fetch_history_or_fabricate_no_effect(self):
        self.budget_status = 503
        with patch("requests.get", side_effect=self.get) as get:
            result = self.post()
        self.assertEqual((result.status_code, result.data["retryable"], get.call_count), (409, True, 1))
        self.assert_no_grant()
        self.assertNotIn(RECONCILIATION_RESULT_KEY, self.run.result)

    def test_regressed_conservative_budget_after_prior_accepted_grant_does_not_issue_second_ticket(self):
        with patch("requests.get", side_effect=self.get):
            self.assertEqual(self.post().status_code, 200)
        self.run.refresh_from_db()
        first = current_execution_ticket(self.run)
        self.journal.append(batch(self.run, [{"seq": 2, "state_version": 2, "status": "running"}]))
        for change in ({"used_model_usd_upper_bound": "0.24"}, {"used_model_tokens": 99}):
            self.budget_changes = change
            with self.subTest(change=change), patch("requests.get", side_effect=self.get):
                result = self.post(self.body(key="b" * 64))
            self.assertEqual((result.status_code, result.data["reason"]), (409, "execution_budget_regressed"))
            self.run.refresh_from_db()
            self.assertEqual(current_execution_ticket(self.run), first)
            self.assertEqual(len(self.run.result[OWNER_EXECUTION_KEY]["tickets"]), 2)

    def test_cursor_mismatch_or_cf_journal_advancing_between_reads_stays_pending(self):
        for cursor in (0, 2):
            self.admit()
            self.budget_changes["event_cursor"] = cursor
            with self.subTest(cursor=cursor), patch("requests.get", side_effect=self.get):
                result = self.post()
                self.assertEqual((result.status_code, result.data["reason"]), (409, "execution_history_pending"))
                self.assert_no_grant()
        self.admit()

        def cf_advanced():
            self.journal.append(batch(self.run, [{"seq": 2, "state_version": 2, "type": "cost", "usd": "0.05"}]))
        self.before_history = cf_advanced
        with patch("requests.get", side_effect=self.get):
            result = self.post()
        self.assertEqual((result.status_code, result.data["retryable"]), (409, True))
        self.assert_no_grant()

    def test_announced_more_history_or_active_lease_never_grants(self):
        self.history_changes["has_more"] = True
        with patch("requests.get", side_effect=self.get):
            result = self.post()
        self.assertEqual((result.status_code, result.data["retryable"]), (409, True))
        self.assert_no_grant()
        self.admit()
        _claim(self.run.pk, timezone.now())
        with patch("requests.get", side_effect=self.get) as get:
            result = self.post()
        self.assertEqual((result.status_code, result.data["retryable"], get.call_count), (409, True, 1))
        self.assert_no_grant()

    def test_any_outer_transaction_prevents_provider_http(self):
        with patch("requests.get", side_effect=self.get) as get, transaction.atomic():
            result = self.post()
        self.assertEqual((result.status_code, result.data["reason"]), (409, "execution_history_pending"))
        get.assert_not_called()
        self.assert_no_grant()

    def test_duplicate_after_revocation_is_not_an_authorization_to_continue(self):
        request = self.body()
        with patch("requests.get", side_effect=self.get):
            self.assertEqual(self.post(request).status_code, 200)
        User.objects.filter(pk=self.user.pk).update(is_active=False)
        self.run.refresh_from_db()
        before = ContentFactoryRun.objects.values().get(pk=self.run.pk)
        with patch("requests.get", side_effect=self.get) as get:
            self.assertEqual(self.post(request).status_code, 409)
        get.assert_not_called()
        self.assertEqual(ContentFactoryRun.objects.values().get(pk=self.run.pk), before)

    def test_original_admitted_body_drift_during_http_cannot_receive_a_ticket(self):
        def changed_body():
            current = ContentFactoryRun.objects.get(pk=self.run.pk)
            ContentFactoryRun.objects.filter(pk=current.pk).update(run_request={**current.run_request,
                "topic": "A different body was never admitted"})
        self.before_history = changed_body
        with patch("requests.get", side_effect=self.get):
            result = self.post()
        self.assertEqual((result.status_code, result.data["reason"]), (409, "execution_budget_unavailable"))
        self.assert_no_grant()

    def test_another_authorized_source_ticket_during_http_fences_the_stale_automatic_grant(self):
        from organizations.models import Organization
        from .run_execution_control import append_execution_ticket, require_current_principal
        original = deepcopy(self.run.run_request)

        def authorized_source_change():
            # A real backend ticket append, not a stubbed grant/reconciliation
            # helper. The separate source controller proves provider admission.
            with transaction.atomic():
                Organization.objects.select_for_update().get(pk=self.org.pk)
                run = ContentFactoryRun.objects.select_for_update().get(pk=self.run.pk)
                require_current_principal(run)
                current = current_execution_ticket(run)
                append_execution_ticket(run, control_key="c" * 64, reason="source_repin",
                    scope={**current["scope"], "expected_source_sha": "c" * 40},
                    increment_generation=False, expected_execution_id=current["execution_id"])
        self.before_history = authorized_source_change
        with patch("requests.get", side_effect=self.get):
            result = self.post()
        self.assertEqual((result.status_code, result.data["reason"]), (409, "execution_control_refused"))
        self.run.refresh_from_db()
        self.assertEqual(set(self.run.result[CONTROL_KEY]["grants"]), {"c" * 64})
        self.assertEqual(current_execution_ticket(self.run)["scope"]["expected_source_sha"], "c" * 40)
        self.assertEqual(self.run.run_request, original)

    def test_existing_admission_uses_original_cap_after_new_admission_configuration_is_off(self):
        original = deepcopy(self.run.run_request)
        with override_settings(ARTICLES_RUN_OWNERSHIP_ENABLED=False, ARTICLES_RUN_FACT_AUTHORITY_ENABLED=False,
                ARTICLES_RUN_EVENT_AUTHORITY_ENABLED=False, ARTICLES_RUN_CONTROL_MODEL_USD="0", ARTICLES_RUN_CONTROL_MODEL_TOKENS=0), \
                patch("requests.get", side_effect=self.get):
            result = self.post()
        self.assertEqual(result.status_code, 200, result.data)
        self.run.refresh_from_db()
        self.assertEqual(self.run.run_request, original)

    def test_original_unbudgeted_admission_is_refused_before_any_cf_get(self):
        with override_settings(ARTICLES_RUN_CONTROL_MODEL_USD="0", ARTICLES_RUN_CONTROL_MODEL_TOKENS=0):
            self.admit()
        self.assertNotIn(BUDGET_KEY, self.run.run_request)
        before = ContentFactoryRun.objects.values().get(pk=self.run.pk)
        with patch("requests.get", side_effect=self.get) as get:
            result = self.post()
        self.assertEqual((result.status_code, result.data["reason"], result.data["retryable"]),
                         (409, "execution_budget_unavailable", False))
        get.assert_not_called()
        self.assertEqual(ContentFactoryRun.objects.values().get(pk=self.run.pk), before)

    def test_replayed_control_key_cannot_change_its_original_reason(self):
        request = self.body()
        with patch("requests.get", side_effect=self.get):
            self.assertEqual(self.post(request).status_code, 200)
        self.run.refresh_from_db()
        before = ContentFactoryRun.objects.values().get(pk=self.run.pk)
        with patch("requests.get", side_effect=self.get) as get:
            result = self.post({**request, "reason": "orphan_resume"})
        self.assertEqual((result.status_code, result.data["reason"]), (409, "execution_control_key_conflict"))
        get.assert_not_called()
        self.assertEqual(ContentFactoryRun.objects.values().get(pk=self.run.pk), before)
