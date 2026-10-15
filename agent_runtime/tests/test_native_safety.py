"""Maek safety groundwork under mocked fences, with DB and network forbidden."""

from contextlib import nullcontext
from datetime import timedelta
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace as Obj
from unittest.mock import MagicMock, patch
from uuid import uuid4

from django.conf import settings
from django.core.management import call_command
from django.test import SimpleTestCase, override_settings
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.test import APIRequestFactory

from agent_runtime import event_webhooks, native_invocations as inv, native_sources, runtime
from agent_runtime.management.commands import run_agent_scheduler as scheduler
from agent_runtime.policy import AgentPermissionDenied
from agent_runtime.tests.test_native_pilot import NOW, fixture
from startup_updates.mcp import native_agents


@override_settings(AGENT_RUNTIME_ENABLED=True)
class NativeSafetyTests(SimpleTestCase):
    def test_thread_context_fresh_for_3h_old_invocation(self):
        _, _, invocation = fixture()
        invocation.created_at = NOW - timedelta(hours=3)
        invocation.expires_at = NOW + timedelta(hours=20)
        for stamp in (NOW, NOW + timedelta(minutes=20)):
            with patch.object(native_sources.timezone, "now", return_value=stamp), patch.object(native_sources, "query", return_value={}) as query:
                native_sources.read_invocation(invocation)
            self.assertEqual(query.call_args.kwargs["expires_at"], int(stamp.timestamp()) + 840)
        context = native_sources.invocation_context(invocation, operation="thread", now=invocation.expires_at - timedelta(seconds=2))
        self.assertEqual(context["expires_at"], int(invocation.expires_at.timestamp()))

    def test_signed_reply_window_uses_created_at_clock_and_is_never_extended(self):
        _, _, invocation = fixture()
        invocation.expires_at = NOW + timedelta(hours=24)
        event = inv.reply_event(invocation, "Synthetic bounded answer", created_at=int(NOW.timestamp()))
        context = json.loads(next(tag[1] for tag in event["tags"] if tag[0] == "native_invocation"))
        self.assertEqual(event["created_at"], int(NOW.timestamp()))
        self.assertEqual(context["expires_at"], event["created_at"] + 840)

    def test_invocation_status_codes_remain_distinct(self):
        _, grant, invocation = fixture()
        for state, code in (("expired", "invocation_expired"), ("cancelled", "invocation_cancelled"), ("replied", "invocation_replied")):
            invocation.status = state
            with self.subTest(state=state), self.assertRaises(AgentPermissionDenied) as error:
                inv.validate_invocation(invocation, grant, now=NOW)
            self.assertEqual(error.exception.code, code)
        invocation.status = "pending"
        invocation.expires_at = NOW
        with self.assertRaises(AgentPermissionDenied) as error:
            inv.validate_invocation(invocation, grant, now=NOW)
        self.assertEqual(error.exception.code, "invocation_expired")
        inv.validate_invocation(invocation, grant, now=NOW, allow_expired=True)
        grant.revoked_at = NOW
        with self.assertRaises(AgentPermissionDenied):
            inv.validate_invocation(invocation, grant, now=NOW, allow_expired=True)

    def test_missing_owner_device_cannot_escape_webhook_delivery_check(self):
        installation, grant, invocation = fixture()
        installation.owner_device = None
        with patch.object(inv, "AgentInvocation") as rows, patch.object(native_sources, "require_native"), patch.object(native_sources, "current_device") as device:
            rows.objects.get.return_value = invocation
            self.assertFalse(inv.invocation_deliverable(grant, invocation.pk, now=NOW))
            device.assert_not_called()

    def test_lease_live_handles_missing_null_and_unbounded(self):
        for delivery in (None, [], {}, {"expires_at": None}, {"expires_at": "garbage"}, {"expires_at": "2026-10-12T02:00:00"}, {"expires_at": NOW.isoformat()}):
            self.assertFalse(event_webhooks.lease_live(delivery, NOW))
        self.assertTrue(event_webhooks.lease_live({"expires_at": (NOW + timedelta(seconds=1)).isoformat()}, NOW))
        unbounded = {"lease": "unbounded", "granted_at": NOW.isoformat()}
        with override_settings(AGENT_RUNTIME_MCP_EVENTS_LEASE_POLICY="24h"):
            self.assertTrue(event_webhooks.lease_live(unbounded, NOW + timedelta(hours=23)))
            self.assertFalse(event_webhooks.lease_live(unbounded, NOW + timedelta(hours=24)))
            self.assertFalse(event_webhooks.lease_live({"lease": "unbounded"}, NOW))
        with override_settings(AGENT_RUNTIME_MCP_EVENTS_LEASE_POLICY="grant"):
            self.assertTrue(event_webhooks.lease_live(unbounded, NOW + timedelta(days=30)))
            self.assertFalse(event_webhooks.lease_live({**unbounded, "expires_at": None}, NOW))

    def test_settings_integers_clamp_and_bad_values_do_not_crash(self):
        from mlai import settings as module
        for raw, expected in (("oops", 20), ("0", 15), ("999999", 10080)):
            with self.subTest(raw=raw), patch.dict(module.os.environ, {"SYNTHETIC_NATIVE_INT": raw}):
                self.assertEqual(module._native_bounded_int("SYNTHETIC_NATIVE_INT", 20, 15, 10080), expected)
        self.assertFalse(settings.AGENT_RUNTIME_NATIVE_ALWAYS_ADMIT)
        self.assertEqual(settings.AGENT_RUNTIME_MCP_EVENTS_LEASE_POLICY, "24h")
        self.assertEqual(settings.MLAI_MCP_REFRESH_REPLAY_GRACE_SECONDS, 0)

    def test_worker_failure_isolated_with_heartbeat_and_redacted_diagnostics(self):
        stream = io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "health"
            with (patch.object(runtime, "recover_tasks", side_effect=ValueError("private message MUST NOT LOG")),
                  patch.object(scheduler.actions, "recover_actions", return_value=2),
                  patch.object(scheduler.events, "dispatch_subscriptions", return_value=3) as fanout,
                  patch.object(scheduler.logger, "exception") as log):
                result = scheduler.tick(fanout=True, health_file=marker, stdout=stream)
            self.assertEqual(result["errors"], {"recovered_tasks": 1})
            self.assertEqual(result["recovered_tasks"], 0)
            self.assertEqual(result["unknown_actions"], 2)
            self.assertEqual(result["event_tasks"], 3)
            fanout.assert_called_once()
            self.assertTrue(marker.is_file())
            self.assertNotIn("private message", stream.getvalue())
            self.assertEqual(json.loads(stream.getvalue())["error_type"], "ValueError")
            self.assertNotIn("private message", str(log.call_args))

    def test_native_worker_stages_continue_and_expire_after_replies(self):
        stream, order = io.StringIO(), []
        with (patch.object(runtime, "recover_tasks", return_value=0),
              patch.object(scheduler.actions, "recover_actions", return_value=0),
              patch("agent_runtime.native_installations.provision_native_installations", side_effect=RuntimeError("synthetic")),
              patch.object(inv, "poll_native_mentions", side_effect=lambda **kw: order.append("poll") or 1),
              patch.object(inv, "dispatch_native_replies", side_effect=lambda **kw: order.append("replies") or 1),
              patch.object(inv, "expire_native_invocations", side_effect=lambda **kw: order.append("expire") or 2),
              patch.object(event_webhooks, "dispatch_webhooks", side_effect=lambda **kw: order.append("webhooks") or 1),
              patch.object(scheduler.logger, "exception")):
            result = scheduler.tick(native=True, webhooks=True, stdout=stream)
        self.assertEqual(order, ["poll", "replies", "expire", "webhooks"])
        self.assertEqual(result["native_provisioned"], 0)
        self.assertEqual(result["errors"], {"native_provisioned": 1})
        self.assertEqual(result["native_expired"], 2)

    def test_command_removes_stale_heartbeat_before_starting(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "health"
            marker.write_text("stale")
            def tick(**kwargs):
                self.assertFalse(marker.exists())
                marker.touch()
                return {"errors": {}}
            with patch.object(scheduler, "tick", side_effect=tick), patch.object(scheduler, "close_old_connections"):
                call_command("run_agent_scheduler", once=True, health_file=str(marker), stdout=io.StringIO())
            self.assertTrue(marker.exists())

    @override_settings(AGENT_RUNTIME_MCP_EVENTS_ENABLED=True)
    def test_one_bad_subscription_does_not_stop_others(self):
        from agent_runtime.tests.test_core import grant_fixture
        from agent_runtime.tests.test_dot_protocol import SECRET
        from agent_runtime.webhook_transport import WebhookResponse
        grant = grant_fixture()
        def subscription(config):
            return Obj(pk=uuid4(), grant_id=grant.pk, generation=grant.generation,
                       resource=grant.resources[0], cursor=0, is_active=True, event_types=["message.created"],
                       allow_agent_events=False, max_chain_depth=1, save=MagicMock(), delivery={
                           "mode": "webhook", "sealed": event_webhooks.seal(config),
                           "expires_at": (NOW + timedelta(hours=1)).isoformat()})
        bad = subscription({"url": "https://receiver.invalid"})  # missing secret, after unseal
        good = subscription({"url": "https://receiver.invalid", "secret": SECRET})
        rows = {bad.pk: bad, good.pk: good}
        event = Obj(pk=15, source="slack", resource=grant.resources[0], event_type="message.created",
                    payload={"message_ts": "1.001"}, actor_kind="human", chain_id=uuid4(), chain_depth=0, occurred_at=NOW)
        with (patch.object(event_webhooks, "AgentSubscription") as subscriptions,
              patch.object(event_webhooks, "AgentEvent") as events,
              patch.object(event_webhooks.transaction, "atomic", side_effect=lambda: nullcontext()),
              patch.object(event_webhooks, "locked", return_value=Obj(get=lambda **kw: rows[kw["pk"]])),
              patch.object(event_webhooks, "_grant", return_value=grant),
              patch.object(event_webhooks, "authorize"),
              patch("agent_runtime.unified_authority.assert_record_authority"),
              patch.object(event_webhooks.logger, "exception"), patch("builtins.print")):
            subscriptions.objects.filter.return_value.order_by.return_value.values_list.return_value.__getitem__.return_value = [bad.pk, good.pk]
            subscriptions.objects.values_list.return_value.get.return_value = grant.pk
            events.objects.filter.return_value.order_by.return_value.__getitem__.return_value = [event]
            transport = MagicMock(return_value=WebhookResponse(204, b""))
            self.assertEqual(event_webhooks.dispatch_webhooks(now=NOW, transport=transport), 1)
            self.assertEqual(bad.cursor, 0)
            self.assertEqual(good.cursor, event.pk)
            self.assertTrue(bad.is_active)
            transport.assert_called_once()
            subscriptions.objects.filter.assert_any_call(pk=bad.pk)
            subscriptions.objects.filter.return_value.update.assert_called_once()


@override_settings(AGENT_RUNTIME_ENABLED=True)
class NativeReplySafetyTests(SimpleTestCase):
    def setUp(self):
        self.installation, self.grant, self.invocation = fixture()
        self.invocation.save = MagicMock()
        self.row = Obj(invocation=self.invocation, invocation_id=self.invocation.pk, status="pending", error_code="",
                       receipt={}, event={"id": "c" * 64}, save=MagicMock())
        self.enterContext(patch.object(inv.transaction, "atomic", side_effect=lambda: nullcontext()))
        self.enterContext(patch.object(runtime, "locked", side_effect=lambda qs: qs))
        self.enterContext(patch.object(runtime, "_grant", return_value=self.grant))
        self.audit = self.enterContext(patch.object(runtime, "audit"))
        self.invocations = self.enterContext(patch.object(inv, "AgentInvocation"))
        self.replies = self.enterContext(patch.object(inv, "AgentInvocationReply"))
        self.invocations.objects.values_list.return_value.get.return_value = self.grant.pk
        self.invocations.objects.all.return_value.get.return_value = self.invocation
        self.invocations.objects.filter.return_value.order_by.return_value.values_list.return_value.__getitem__.return_value = [self.invocation.pk]
        self.replies.objects.filter.return_value.order_by.return_value.values_list.return_value.__getitem__.return_value = [self.invocation.pk]
        self.replies.objects.all.return_value.get.return_value = self.row
        self.replies.objects.all.return_value.filter.return_value.first.return_value = self.row
        self.enterContext(patch.object(native_sources, "require_native"))
        self.enterContext(patch("agent_runtime.unified_authority.assert_record_authority"))
        self.enterContext(patch.object(native_sources, "current_device", return_value=self.installation.owner_device))
        self.authorize = self.enterContext(patch.object(inv, "authorize"))
        self.send = self.enterContext(patch.object(native_sources, "send_reply", return_value={"accepted": True}))
        self.sign = self.enterContext(patch.object(inv, "reply_event"))

    def signed_deadline(self, deadline):
        self.row.event["tags"] = [["native_invocation", json.dumps({"expires_at": int(deadline.timestamp())})]]

    def test_dispatch_window_elapsed_marks_failed_without_send(self):
        for initial, expected in (("pending", "failed"), ("unknown", "stopped_unknown")):
            for expired_invocation in (False, True):
                with self.subTest(initial=initial, expired_invocation=expired_invocation):
                    self.row.status = initial
                    self.invocation.expires_at = NOW if expired_invocation else NOW + timedelta(hours=1)
                    self.signed_deadline(NOW + timedelta(seconds=5))
                    original = json.dumps(self.row.event, sort_keys=True)
                    self.assertEqual(inv.dispatch_native_replies(now=NOW), 0)
                    self.assertEqual(self.row.status, expected)
                    self.assertEqual(json.dumps(self.row.event, sort_keys=True), original)
        self.send.assert_not_called()
        self.sign.assert_not_called()

    def test_dispatch_rechecks_signed_deadline_after_slow_authority(self):
        clock = [NOW]
        self.signed_deadline(NOW + timedelta(seconds=10))
        self.authorize.side_effect = lambda *args, **kwargs: clock.__setitem__(0, NOW + timedelta(seconds=7))
        with patch.object(inv.timezone, "now", side_effect=lambda: clock[0]):
            self.assertEqual(inv.dispatch_native_replies(), 0)
        self.assertEqual(self.row.status, "failed")
        self.assertEqual(self.row.error_code, "reply_window_elapsed")
        self.send.assert_not_called()
        self.sign.assert_not_called()

    def test_resource_unavailable_does_not_cancel_reply(self):
        for initial in ("pending", "unknown"):
            self.row.status = initial
            self.authorize.side_effect = AgentPermissionDenied("resource_unavailable")
            self.assertEqual(inv.dispatch_native_replies(now=NOW), 0)
            self.assertEqual(self.row.status, initial)
        self.send.assert_not_called()
        self.row.save.assert_not_called()

    def test_sweeper_writes_expired_and_audits(self):
        for initial, expected in (("pending", "failed"), ("unknown", "stopped_unknown")):
            self.invocation.status = "pending"
            self.invocation.expires_at = NOW
            self.row.status = initial
            self.signed_deadline(NOW)
            self.assertEqual(inv.expire_native_invocations(now=NOW), 1)
            self.assertEqual(self.invocation.status, "expired")
            self.assertEqual(self.row.status, expected)
        self.audit.assert_any_call("native.invocation.expired", grant=self.grant, invocation_id=str(self.invocation.pk))
        self.send.assert_not_called()
        self.sign.assert_not_called()

    def test_sweeper_leaves_live_reply_window_to_dispatcher(self):
        self.invocation.expires_at = NOW
        self.signed_deadline(NOW + timedelta(seconds=10))
        self.assertEqual(inv.expire_native_invocations(now=NOW), 0)
        self.assertEqual(self.invocation.status, "pending")
        self.row.save.assert_not_called()

    def test_sweeper_without_reply_still_records_expiry(self):
        self.invocation.expires_at = NOW
        self.replies.objects.all.return_value.filter.return_value.first.return_value = None
        self.assertEqual(inv.expire_native_invocations(now=NOW), 1)
        self.assertEqual(self.invocation.status, "expired")

    def test_status_read_after_expiry(self):
        self.invocation.expires_at = NOW
        self.invocation.status = "expired"
        principal = Obj(grant=self.grant, credential=Obj(scopes=self.grant.scopes, generation=self.grant.generation), owner=self.installation.owner)
        with patch.object(inv, "_current_principal", return_value=principal), patch.object(inv.timezone, "now", return_value=NOW):
            self.replies.objects.filter.return_value.first.return_value = self.row
            result = inv.get_invocation_status.__wrapped__(principal, self.invocation.pk)
        self.assertEqual(result["status"], "expired")
        self.assertEqual(result["reply"]["status"], "pending")

    def test_reply_daily_limit_uses_application_error_without_queuing(self):
        self.replies.objects.filter.return_value.first.return_value = None
        self.replies.objects.filter.return_value.exclude.return_value.count.return_value = self.grant.daily_task_limit
        with patch.object(inv, "_authorized_invocation", return_value=(None, self.invocation)):
            with self.assertRaises(AgentPermissionDenied) as failure:
                inv.reply_to_invocation.__wrapped__(None, self.invocation.pk, "Synthetic answer", "synthetic-reply")
        self.assertEqual(failure.exception.code, "daily_limit_reached")
        self.replies.objects.create.assert_not_called()

    def test_conflicting_reply_stays_validation_error_with_specific_code(self):
        self.row.payload_hash, self.row.idempotency_key = "different", "other-reply"
        self.replies.objects.filter.return_value.first.return_value = self.row
        with patch.object(inv, "_authorized_invocation", return_value=(None, self.invocation)):
            with self.assertRaises(ValidationError) as failure:
                inv.reply_to_invocation.__wrapped__(None, self.invocation.pk, "Synthetic answer", "synthetic-reply")
        self.assertEqual(failure.exception.get_codes(), ["reply_conflict"])
        self.replies.objects.create.assert_not_called()

    def test_bad_signed_context_fails_closed_and_missing_tag_uses_invocation_expiry(self):
        self.assertEqual(inv._signed_context_expiry(self.row), self.invocation.expires_at)
        for raw in ("{bad", '{"expires_at": null}', '{"expires_at": true}', '{"expires_at": "9999999999"}'):
            self.row.status = "pending"
            self.row.event["tags"] = [["native_invocation", raw]]
            self.assertEqual(inv.dispatch_native_replies(now=NOW), 0)
            self.assertEqual(self.row.status, "failed")
        self.send.assert_not_called()


class NativeMcpErrorTests(SimpleTestCase):
    def test_application_errors_are_not_reconnect_challenges(self):
        cases = {"grant_inactive": "owner_action_required", "installation_inactive": "owner_action_required",
                 "owner_device_changed": "owner_action_required", "invocation_expired": "invocation_expired",
                 "invocation_cancelled": "invocation_cancelled", "invocation_replied": "invocation_replied",
                 "daily_limit_reached": "daily_limit_reached"}
        for original, expected in cases.items():
            def operation():
                raise AgentPermissionDenied(original)
            with self.subTest(code=original), self.assertRaises(native_agents.NativeAgentError) as failure:
                native_agents._api_errors(operation)()
            self.assertEqual(failure.exception.default_code, expected)
            self.assertEqual(failure.exception.status_code, 409)
        for code in ("unified_grant_revoked", "scope_denied"):
            with self.subTest(code=code), self.assertRaises(PermissionDenied):
                native_agents._api_errors(lambda: (_ for _ in ()).throw(AgentPermissionDenied(code)))()
        for code in ("invocation_ambiguous", "no_pending_invocation", "reply_conflict"):
            with self.subTest(code=code), self.assertRaises(native_agents.NativeAgentError) as failure:
                native_agents._api_errors(lambda: (_ for _ in ()).throw(ValidationError("synthetic", code=code)))()
            self.assertEqual(failure.exception.default_code, code)

    def test_late_reply_returns_invocation_expired_not_auth_challenge(self):
        from community_chat.tests import test_mlai_mcp_native_agents as protocol
        from startup_updates.mcp import registry
        self.factory = APIRequestFactory()
        with override_settings(**protocol.PILOT_SETTINGS):
            _, grant, invocation = fixture()
            invocation.expires_at = NOW
            def expired(*args):
                return native_agents._api_errors(lambda: inv.validate_invocation(invocation, grant, now=NOW))()
            with patch.object(registry, "call", side_effect=expired):
                response, body = protocol.UnifiedDotProtocolTests.post(self, protocol.account_principal(), {
                    "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                    "params": {"name": "reply_to_invocation", "arguments": {}}})
        result = body["result"]
        self.assertEqual(response.status_code, 200)
        self.assertTrue(result["isError"])
        self.assertEqual(result["structuredContent"]["error"], "invocation_expired")
        self.assertNotIn("mcp/www_authenticate", result.get("_meta", {}))
