"""User resume keeps one original execution, charge and command identity."""
from copy import deepcopy
from unittest.mock import Mock, patch

from django.db import transaction
from django.test import TestCase, override_settings

from roo.models import Ledger
from workflow_runs.models import ContentFactoryRun
from .run_execution_control import ExecutionControlDenied, append_execution_ticket
from .run_ownership import (BUDGET_KEY, OWNER_EXECUTION_KEY, WIRE_REQUEST_KEY, WIRE_EXECUTION_KEY,
    current_execution_ticket, reserve_admitted_run, user_principal)
from .run_user_resume import user_resume_payload
from .self_heal_testing import make_p2_org


@override_settings(ARTICLES_RUN_OWNERSHIP_ENABLED=True, ARTICLES_RUN_FACT_AUTHORITY_ENABLED=True,
                   ARTICLES_RUN_EVENT_AUTHORITY_ENABLED=True)
class UserResumeTests(TestCase):
    def setUp(self):
        fixture = make_p2_org()
        self.org, self.user = fixture.organization, fixture.owner
        self.actor = "mlai_user:" + str(self.user.pk)
        payload = {"domain": self.org.domain, "client_request_id": "resume-original", "delivery_mode": "content_only",
                   "delivery_mode_confirmed": True, "topic": "Retain this topic", "approved_body_sha256": "a" * 64}
        self.run = reserve_admitted_run(organization=self.org, principal=user_principal(self.user.pk, self.actor),
                                       workflow="article_generation", payload=payload)
        ContentFactoryRun.objects.filter(pk=self.run.pk).update(status="failed")
        self.run.refresh_from_db()

    def resume(self, key="same-saved-command", payload=None):
        return user_resume_payload(run=self.run, user=self.user, command_key=key, payload=payload or {})

    def test_same_user_command_after_lost_ack_gets_one_ticket_and_no_new_charge_request_or_clocks(self):
        before = ContentFactoryRun.objects.values().get(pk=self.run.pk)
        money = list(Ledger.objects.values())
        first = self.resume()
        self.assertEqual(first[WIRE_EXECUTION_KEY]["generation"], 1)
        self.run.refresh_from_db()
        second = self.resume()
        self.assertEqual(first, second)
        self.run.refresh_from_db()
        self.assertEqual(len(self.run.result[OWNER_EXECUTION_KEY]["tickets"]), 2)
        after = ContentFactoryRun.objects.values().get(pk=self.run.pk)
        for key in before.keys() - {"result"}:
            self.assertEqual(after[key], before[key], key)
        self.assertEqual(list(Ledger.objects.values()), money)

    def test_no_key_deactivated_current_requester_or_approval_pause_cannot_get_a_ticket(self):
        before = ContentFactoryRun.objects.values().get(pk=self.run.pk)
        with self.assertRaises(ExecutionControlDenied):
            self.resume(key=None)
        self.assertEqual(ContentFactoryRun.objects.values().get(pk=self.run.pk), before)
        type(self.user).objects.filter(pk=self.user.pk).update(is_active=False)
        with self.assertRaises(ExecutionControlDenied):
            self.resume()
        self.assertEqual(ContentFactoryRun.objects.values().get(pk=self.run.pk), before)
        type(self.user).objects.filter(pk=self.user.pk).update(is_active=True)
        ContentFactoryRun.objects.filter(pk=self.run.pk).update(approval_state="required")
        self.run.refresh_from_db()
        with self.assertRaises(ExecutionControlDenied):
            self.resume()

    def test_hard_terminal_run_is_never_revived(self):
        for status in ("completed", "cancelled", "denied", "awaiting_approval"):
            ContentFactoryRun.objects.filter(pk=self.run.pk).update(status=status)
            self.run.refresh_from_db()
            before = ContentFactoryRun.objects.values().get(pk=self.run.pk)
            with self.subTest(status=status), self.assertRaises(ExecutionControlDenied):
                self.resume()
            self.assertEqual(ContentFactoryRun.objects.values().get(pk=self.run.pk), before)

    def test_operation_controller_ticket_is_forwarded_without_an_extra_generation(self):
        with transaction.atomic():
            locked = ContentFactoryRun.objects.select_for_update().get(pk=self.run.pk)
            ticket = append_execution_ticket(locked, control_key="a" * 64, reason="operation_resume")
        self.run.refresh_from_db()
        payload = {WIRE_REQUEST_KEY: deepcopy(self.run.run_request[WIRE_REQUEST_KEY]), WIRE_EXECUTION_KEY: ticket}
        result = self.resume(key=None, payload=payload)
        self.assertEqual(result[WIRE_EXECUTION_KEY], ticket)
        self.run.refresh_from_db()
        self.assertEqual(len(self.run.result[OWNER_EXECUTION_KEY]["tickets"]), 2)
        self.assertEqual(current_execution_ticket(self.run)["generation"], 1)

    def test_caller_scope_budget_or_arbitrary_old_ticket_cannot_grant_resume_authority(self):
        initial = current_execution_ticket(self.run)
        with self.assertRaises(ExecutionControlDenied):
            self.resume(payload={WIRE_EXECUTION_KEY: initial})
        result = self.resume(payload={"expected_source_sha": "f" * 40, "operation_id": "forged", BUDGET_KEY: {"max_model_usd": "999"}})
        self.assertNotIn("expected_source_sha", result)
        self.assertNotIn("operation_id", result)
        self.assertNotIn(BUDGET_KEY, result)

    def test_actual_portable_http_uses_saved_ticket_and_same_command_retry(self):
        from . import vibe_marketing_views as views
        observed = []

        def post(url, *, json, **kwargs):
            self.run.refresh_from_db()
            self.assertEqual(json[WIRE_EXECUTION_KEY], current_execution_ticket(self.run))
            self.assertEqual(json[WIRE_REQUEST_KEY], self.run.run_request[WIRE_REQUEST_KEY])
            observed.append(deepcopy(json))
            response = Mock(status_code=202, content=b"{}")
            response.json.return_value = {"status": "queued", "run_id": self.run.run_id}
            return response

        with patch.object(views, "_content_factory_remote_config", return_value={"enabled": True, "base_url": "https://worker.invalid"}), \
                patch.object(views, "_content_factory_headers", return_value={}), \
                patch.object(views.http_client, "post", side_effect=post):
            for _ in range(2):
                result = views._call_content_factory_run_action(run_id=self.run.run_id, action="resume",
                    workflow=self.run.workflow, payload={}, control_user=self.user, control_command_key="same-command")
                self.assertEqual(result["status"], "queued")
        self.assertEqual(observed[0], observed[1])
        self.assertEqual(len(self.run.result[OWNER_EXECUTION_KEY]["tickets"]), 2)

    def test_authenticated_app_keeps_command_header_and_does_not_project_ack_status_or_clocks(self):
        from rest_framework.test import APIRequestFactory, force_authenticate
        from integrations import api_views_content_factory_app as views
        before = ContentFactoryRun.objects.values().get(pk=self.run.pk)
        observed = []

        def remote(method, path, *, payload):
            self.run.refresh_from_db()
            self.assertEqual(payload[WIRE_EXECUTION_KEY], current_execution_ticket(self.run))
            observed.append(deepcopy(payload))
            response = Mock(status_code=202, content=b"{}")
            response.json.return_value = {"run_id": self.run.run_id, "status": "running", "current_step": "worker-claim"}
            return response

        with patch.object(views, "_content_factory_request", side_effect=remote):
            for _ in range(2):
                request = APIRequestFactory().post("/app/owned/resume", {}, format="json",
                    HTTP_IDEMPOTENCY_KEY="saved-app-command")
                force_authenticate(request, user=self.user)
                response = views.ContentFactoryAppRunControlView.as_view()(request, run_id=self.run.run_id, action="resume")
                self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(observed[0], observed[1])
        after = ContentFactoryRun.objects.values().get(pk=self.run.pk)
        self.assertEqual(len(after["result"][OWNER_EXECUTION_KEY]["tickets"]), 2)
        for field in before.keys() - {"result"}:
            self.assertEqual(after[field], before[field], field)

    def test_authenticated_app_denial_records_intent_until_actual_typed_producer_stops_execution(self):
        from rest_framework.test import APIRequestFactory, force_authenticate
        from integrations import api_views_content_factory_app as views
        from .tests_run_event_authority import batch
        from .run_event_authority import receive_authoritative_run_events
        # This class normally seeds a failed run for resume tests. Denial here
        # starts from the real admitted/typed-running state instead.
        ContentFactoryRun.objects.filter(pk=self.run.pk).update(status="queued")
        self.run.refresh_from_db()
        self.assertEqual(receive_authoritative_run_events(batch(self.run, [{"status": "running"}])).status_code, 200)
        self.run.refresh_from_db()
        before = ContentFactoryRun.objects.values().get(pk=self.run.pk)
        remote = Mock(status_code=202, content=b"{}")
        remote.json.return_value = {"run_id": self.run.run_id, "status": "completed"}
        request = APIRequestFactory().post("/app/owned/deny", {}, format="json")
        force_authenticate(request, user=self.user)
        with patch.object(views, "_content_factory_request", return_value=remote):
            response = views.ContentFactoryAppRunControlView.as_view()(request, run_id=self.run.run_id, action="deny")
        self.assertEqual(response.status_code, 200, response.data)
        after = ContentFactoryRun.objects.values().get(pk=self.run.pk)
        from .run_ownership import user_stop_intent
        self.run.refresh_from_db()
        self.assertIsNotNone(user_stop_intent(self.run))
        self.assertEqual(after["status"], before["status"])
        for field in before.keys() - {"result"}:
            self.assertEqual(after[field], before[field], field)
        self.assertEqual(receive_authoritative_run_events(batch(self.run, [{"status": "running"}, {"status": "denied"}])).status_code, 200)
        self.run.refresh_from_db()
        self.assertEqual(self.run.status, "denied")

    def test_lost_denial_ack_keeps_same_durable_consent_withdrawal_for_retry(self):
        from rest_framework.test import APIRequestFactory, force_authenticate
        from integrations import api_views_content_factory_app as views
        from integrations.services.article_generation import ArticleGenerationError
        from .run_ownership import user_stop_intent
        before = ContentFactoryRun.objects.values().get(pk=self.run.pk)

        def request():
            command = APIRequestFactory().post("/app/owned/deny", {}, format="json")
            force_authenticate(command, user=self.user)
            return command

        with patch.object(views, "_content_factory_request", side_effect=ArticleGenerationError("Synthetic unknown acknowledgement")):
            response = views.ContentFactoryAppRunControlView.as_view()(request(), run_id=self.run.run_id, action="deny")
        self.assertEqual(response.status_code, 503)
        self.run.refresh_from_db()
        original = user_stop_intent(self.run)
        self.assertIsNotNone(original)
        remote = Mock(status_code=202, content=b"{}")
        remote.json.return_value = {"run_id": self.run.run_id, "status": "cancelled"}
        with patch.object(views, "_content_factory_request", return_value=remote):
            self.assertEqual(views.ContentFactoryAppRunControlView.as_view()(request(), run_id=self.run.run_id, action="deny").status_code, 200)
        self.run.refresh_from_db()
        self.assertEqual(user_stop_intent(self.run), original)
        after = ContentFactoryRun.objects.values().get(pk=self.run.pk)
        for field in before.keys() - {"result"}:
            self.assertEqual(after[field], before[field], field)
