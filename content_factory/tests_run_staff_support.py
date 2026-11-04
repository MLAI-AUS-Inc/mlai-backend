"""Verified staff recovery keeps the requester distinct from the original payer."""
from unittest.mock import Mock, patch
from uuid import uuid4

from requests import Timeout
from django.core.cache import cache
from django.test import TransactionTestCase, override_settings
from rest_framework.test import APIRequestFactory, force_authenticate

from core.models import User
from community_chat.models import CommunityChatDevice
from community_chat.staff_console_views import StartupActionView
from roo.models import Ledger
from roo.services import PointsService
from workflow_runs.models import ContentFactoryRun
from . import staff_console, vibe_marketing_views as views
from .dispatch_models import ContentFactoryDispatchOutbox
from .dispatch_outbox import _deliver
from .run_execution_control import ExecutionControlDenied, require_current_principal
from .run_ownership import (STAFF_SUPPORT_RESULT_KEY, WIRE_REQUEST_KEY, WIRE_EXECUTION_KEY,
    RunOwnershipConflict, bind_admitted_run, current_execution_ticket, owner_receipt,
    reserve_admitted_run, user_principal, wire_identity)
from .run_staff_support import require_staff_support
from .self_heal_testing import ALL_FLAGS_ON, make_p2_org
from .tests_run_event_authority import FLAGS, batch


@override_settings(**{**ALL_FLAGS_ON, **FLAGS, "CONTENT_FACTORY_URL": "https://cf.fixture.invalid",
    "CONTENT_FACTORY_API_KEY": "synthetic-staff-key", "ROO_API_KEY": "synthetic-staff-callback-key",
    "ARTICLES_FREE_FIRST_ARTICLE_ENABLED": True})
class StaffSupportTests(TransactionTestCase):
    def setUp(self):
        cache.clear()
        self.fixture = make_p2_org()
        self.org, self.owner = self.fixture.organization, self.fixture.owner
        self.staff = User.objects.create_superuser(email=uuid4().hex + "@staff.example.test")
        self.device = CommunityChatDevice.objects.create(user=self.staff, public_key="b" * 64, status="verified")

    def source(self, *, owned=True, free=False):
        from integrations.services.article_generation import _ledger_reference_id
        from .first_article_entitlement import reserve_entitlement, entitled_authorization
        key = "staff-source-" + uuid4().hex
        request = {"domain": self.org.domain, "client_request_id": key,
            "topic": "The original approved topic", "target_keyword": "Original topic"}
        if free:
            request.update(delivery_mode="content_only", delivery_mode_confirmed=True, expected_cost_points=0)
            grant = reserve_entitlement(organization=self.org, user=self.owner, client_request_id=key,
                data=request, config=self.fixture.config)
            self.assertIsNotNone(grant)
            request.update(entitled_authorization(domain=self.org.domain, action="article_generation", ledger=grant))
            self.spend = None
        else:
            PointsService.credit_purchased_topup(user=self.owner, delta=30,
                description="Synthetic staff recovery funding", idempotency_key="fund-" + key)
            self.spend, _ = PointsService.spend(user=self.owner, delta=12, source="CONTENT_FACTORY",
                description="Synthetic approved original article", created_by_slack_id=f"mlai_user:{self.owner.pk}",
                idempotency_key="content_factory:charge:" + key, reference_type="CONTENT_FACTORY",
                reference_id=_ledger_reference_id(key))
            request.update(roo_points_ledger_id=self.spend.pk, roo_points_cost=12,
                roo_points_billing_user_id=self.owner.pk, roo_points_billing_status="charged", roo_points_authorized=True)
        if owned:
            self.run = bind_admitted_run(reserve_admitted_run(organization=self.org, workflow="article_generation",
                principal=user_principal(self.owner.pk, f"mlai_user:{self.owner.pk}"), payload=request), "source-" + key)
        else:
            self.run = ContentFactoryRun.objects.create(run_id=key, organization=self.org, domain=self.org.domain,
                workflow="article_generation", status="failed", run_request=request)
        return self.run

    def action(self, *, key="support-command", user=None):
        request = APIRequestFactory().post("/staff/action", {"action": "switch_to_draft", "runId": self.run.run_id,
            "idempotencyKey": key}, format="json")
        force_authenticate(request, user=user or self.staff)
        request.community_chat_public_key = self.device.public_key
        request.community_chat_installation_id = self.device.installation_id
        with patch.object(views, "analytics_config_for_content_factory", return_value={}), \
                patch.object(views.http_client, "post") as post:
            response = StartupActionView.as_view()(request, organization_id=self.org.pk)
        post.assert_not_called()
        return response

    def child(self):
        row = ContentFactoryDispatchOutbox.objects.get(organization=self.org)
        return ContentFactoryRun.objects.get(result__run_owner_v1__identity__dispatch_key=row.client_request_id), row

    def test_actual_verified_staff_http_seals_actual_requester_and_original_charge_before_outbox(self):
        self.source()
        before = ContentFactoryRun.objects.values().get(pk=self.run.pk)
        response = self.action()
        self.assertEqual((response.status_code, response.data["status"]), (200, "done"), response.data)
        child, row = self.child()
        principal = owner_receipt(child)["identity"]["principal"]
        self.assertEqual(principal, user_principal(self.staff.pk, f"mlai_user:{self.staff.pk}"))
        self.assertEqual(row.payload["billing_user_id"], str(self.owner.pk))
        self.assertEqual(row.payload["request"]["roo_points_requested_by_user_id"], self.staff.pk)
        self.assertEqual(row.payload["request"]["roo_points_ledger_id"], self.spend.pk)
        self.assertEqual(row.payload["request"][WIRE_REQUEST_KEY], wire_identity(owner_receipt(child)))
        self.assertEqual(row.payload["request"][WIRE_EXECUTION_KEY], current_execution_ticket(child))
        self.assertEqual(require_staff_support(child).pk, self.staff.pk)
        require_current_principal(child)
        after = ContentFactoryRun.objects.values().get(pk=self.run.pk)
        for field in before.keys() - {"result"}:
            self.assertEqual(after[field], before[field], field)
        for key, value in before["result"].items():
            self.assertEqual(after["result"][key], value)
        self.assertEqual(Ledger.objects.filter(kind="SPEND").count(), 1)
        self.assertEqual(self.action().data, response.data)
        with override_settings(ARTICLES_RUN_OWNERSHIP_ENABLED=False):
            repeated = self.action(key="second-support-command")
        self.assertEqual(repeated.data["status"], "already_done", repeated.data)
        self.assertEqual(ContentFactoryDispatchOutbox.objects.count(), 1)

    def test_unmarked_source_is_not_enrolled_and_free_original_benefit_is_carried(self):
        self.source(owned=False, free=True)
        response = self.action()
        self.assertEqual(response.data["status"], "done", response.data)
        child, _ = self.child()
        self.run.refresh_from_db()
        self.assertIsNone(owner_receipt(self.run))
        self.assertEqual(require_staff_support(child).pk, self.staff.pk)
        self.assertFalse(Ledger.objects.filter(kind="SPEND").exists())

    def test_revoked_staff_and_changed_source_refuse_before_any_provider_effect(self):
        self.source()
        self.assertEqual(self.action().data["status"], "done")
        child, row = self.child()
        User.objects.filter(pk=self.staff.pk).update(is_staff=False)
        with patch.object(views.http_client, "post") as post, self.assertRaises(RunOwnershipConflict):
            _deliver(row)
        post.assert_not_called()
        with self.assertRaises(ExecutionControlDenied):
            require_current_principal(child)
        User.objects.filter(pk=self.staff.pk).update(is_staff=True)
        from .run_user_stop import record_user_denial
        record_user_denial(self.run, user=self.owner, actor_id=f"mlai_user:{self.owner.pk}")
        with patch.object(views.http_client, "post") as post, self.assertRaises(RunOwnershipConflict):
            _deliver(row)
        post.assert_not_called()
        self.assertEqual(self.action(key="source-stopped-command").data["status"], "refused")
        self.assertEqual(ContentFactoryDispatchOutbox.objects.count(), 1)

    def test_changed_source_ticket_and_disabled_new_admission_do_not_create_or_redispatch(self):
        self.source()
        with override_settings(ARTICLES_RUN_OWNERSHIP_ENABLED=False):
            disabled = self.action(key="disabled-new-support")
        self.assertEqual(disabled.data["status"], "refused", disabled.data)
        self.assertFalse(ContentFactoryDispatchOutbox.objects.exists())
        self.assertEqual(self.action().data["status"], "done")
        child, row = self.child()
        from .tests_run_event_authority import advance_ticket
        advance_ticket(self.run, generation=1)
        with patch.object(views.http_client, "post") as post, self.assertRaises(RunOwnershipConflict):
            _deliver(row)
        post.assert_not_called()
        with self.assertRaises(ExecutionControlDenied):
            require_current_principal(child)
        self.assertEqual(ContentFactoryDispatchOutbox.objects.count(), 1)

    def test_unknown_http_ack_then_typed_callback_first_replays_one_child_without_second_post(self):
        self.source()
        self.assertEqual(self.action().data["status"], "done")
        child, row = self.child()
        with patch.object(views, "_lookup_content_factory_dispatch_by_key", return_value=("not_received", {})), \
                patch.object(views.http_client, "post", side_effect=Timeout("unknown ack")) as post, \
                self.assertRaises(Timeout):
            _deliver(row)
        self.assertEqual(post.call_args.kwargs["json"][WIRE_REQUEST_KEY], wire_identity(owner_receipt(child)))
        payload = batch(child, [{"status": "running"}])
        payload["run_id"] = "staff-continuation-worker"
        from .run_event_authority import event_digest
        payload["events"][0]["digest"] = event_digest(payload["run_id"], payload[WIRE_REQUEST_KEY],
            payload[WIRE_EXECUTION_KEY], payload["events"][0])
        from .service_views import ContentFactoryCallbackView
        callback = APIRequestFactory().post("/callback", {**payload, "event": "run_events_v2"}, format="json",
            HTTP_X_API_KEY="synthetic-staff-callback-key")
        received = ContentFactoryCallbackView.as_view()(callback)
        self.assertEqual(received.status_code, 200, received.data)
        child.refresh_from_db()
        self.assertEqual(child.run_id, "staff-continuation-worker")
        with patch.object(views, "_lookup_content_factory_dispatch_by_key", return_value=("started", {"run_id": child.run_id})), \
                patch.object(views.http_client, "post") as post:
            self.assertEqual(_deliver(row), ("delivered", "", child.run_id))
        post.assert_not_called()
        self.assertEqual(ContentFactoryDispatchOutbox.objects.count(), 1)
        self.assertEqual(Ledger.objects.filter(kind="SPEND").count(), 1)

    def test_http_cannot_assert_staff_grant_or_replace_an_admitted_carried_request(self):
        self.source()
        self.assertEqual(self.action(user=self.owner).status_code, 403)
        self.assertFalse(ContentFactoryDispatchOutbox.objects.exists())
        self.assertEqual(self.action().data["status"], "done")
        child, _ = self.child()
        before = ContentFactoryRun.objects.values().get(pk=child.pk)
        from .no_delivery_refunds import carry_charge
        with self.assertRaises(ValueError):
            carry_charge(source_run=self.run, new_run=child)
        self.assertEqual(ContentFactoryRun.objects.values().get(pk=child.pk), before)
        from .run_ownership import public_run_value
        self.assertNotIn(STAFF_SUPPORT_RESULT_KEY, public_run_value(child.result))
