"""Real bridge ORM checks against the already approved disposable old schema.

Only the explicit bridge runner loads isolated worktree service code. No Django
test database is created, no migration runs, and HTTP transports are fixtures.
"""

import base64
import hashlib
import hmac
import json
import os
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

from django.test import override_settings
from django.utils import timezone
from rest_framework.exceptions import AuthenticationFailed, PermissionDenied

from agent_runtime.models import AgentCredential, AgentEvent, AgentGrant, AgentInvocation, AgentInvocationReply, AgentOAuthBinding, AgentSubscription
from agent_runtime.policy import AgentPermissionDenied
from agent_runtime.tests import test_native_database as native_fixture

RESOURCE, SECRET = native_fixture.RESOURCE, native_fixture.SECRET


@unittest.skipUnless(os.environ.get("MLAI_AGENT_UNIFIED_BRIDGE_TESTS") == "1",
                     "Use the reviewed disposable runner with its exact --bridge-source.")
class UnifiedBridgeDatabaseTests(unittest.TestCase):
    def setUp(self):
        # Reuse fixtures by composition, without inheriting the original 19 tests.
        self.fixture = native_fixture.NativeDatabaseTests("runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.addCleanup(self.fixture.tearDown)
        self.installation, _original, _standalone = self.fixture.active()
        from startup_updates.mcp import config, native_agents, oauth
        from agent_runtime import event_webhooks, native_installations, native_invocations, unified_authority
        self.config, self.adapter, self.oauth = config, native_agents, oauth
        self.webhooks, self.installations, self.invocations, self.authority = (
            event_webhooks, native_installations, native_invocations, unified_authority)
        settings = override_settings(
            MLAI_MCP_ENABLED=True, MLAI_MCP_NATIVE_AGENTS_ENABLED=True,
            COMMUNITY_CHAT_STARTUP_UPDATES_ENABLED=True,
            VALLEY_MCP_PUBLIC_BASE_URL="https://api.example.test",
        )
        settings.enable()
        self.addCleanup(settings.disable)
        self.current = {}
        resolver = patch.object(oauth, "valid_grant", side_effect=self.valid_grant)
        resolver.start()
        self.addCleanup(resolver.stop)
        self.callback_events = []

    def valid_grant(self, identifier, *, scope=None):
        value = self.current.get(identifier)
        if value is None or value.grant.get("revoked"):
            raise AuthenticationFailed("Synthetic unified account session unavailable.")
        if scope and scope not in value.grant["scopes"]:
            raise PermissionDenied("Synthetic account consent excludes this permission.")
        return value

    def consent(self):
        owner = self.fixture.owner
        session = SimpleNamespace(pk=str(uuid4()))
        value = self.oauth.Principal(owner, {
            "id": uuid4().hex, "user_id": owner.pk, "auth_version": owner.auth_version,
            "session_id": str(session.pk), "company_id": None,
            "resource": self.config.mlai_url(), "client_id": "valley_client_actual_registered_host",
            "scopes": sorted(self.authority.DOT_SCOPES),
            "expiresAt": int(timezone.now().timestamp()) + 3600,
        })
        self.current[value.grant["id"]] = value
        native = self.adapter.bind_native_grant(owner, session, value.grant, str(self.installation.pk))
        return value, native

    def subscribe(self, value):
        params = {"name": "agent.mentioned", "arguments": {
            "installation_id": str(self.installation.pk), "resource": RESOURCE},
            "delivery": {"mode": "webhook", "url": "https://provider.example/events", "secret": SECRET},
            "ttlMs": 3600000}
        receiver = self

        class Connection:
            sock = None

            def __init__(self, *_args, **_kwargs):
                pass

            def request(self, _method, _target, body, headers):
                material = headers["webhook-id"].encode() + b"." + headers["webhook-timestamp"].encode() + b"." + body
                expected = "v1," + base64.b64encode(hmac.new(
                    base64.b64decode(SECRET[6:]), material, hashlib.sha256).digest()).decode()
                receiver.assertEqual(headers["webhook-signature"], expected)
                payload = json.loads(body)
                if payload.get("type") == "verification":
                    self.answer = json.dumps({"challenge": payload["challenge"]}).encode()
                else:
                    receiver.callback_events.append(payload)
                    self.answer = b"{}"

            def getresponse(self):
                return SimpleNamespace(status=200, read=lambda _limit: self.answer)

            def close(self):
                pass

        from agent_runtime import webhook_transport
        resolved = ("provider.example", "8.8.8.8", "/events")
        with (
            patch.object(self.webhooks, "resolve_callback", return_value=resolved),
            patch.object(webhook_transport, "resolve_callback", return_value=resolved),
            patch.object(webhook_transport.http.client, "HTTPSConnection", Connection),
        ):
            result = self.adapter.handle_events(value, "events/subscribe", params)
        return AgentSubscription.objects.get(pk=result["id"])

    def queued_reply(self, value, native):
        invocation = self.fixture.invocation(native)
        result = self.adapter.call_tool(value, "reply_to_invocation", {
            "invocation_id": str(invocation.pk), "text": "Synthetic unified Dot answer",
            "idempotency_key": uuid4().hex,
        })
        return invocation, AgentInvocationReply.objects.get(invocation_id=result["invocation_id"])

    def test_consent_is_pending_until_bearer_authentication_then_real_tools_and_events_work(self):
        value, native = self.consent()
        credential = AgentCredential.objects.get(pk=value.grant["native_agent_credential_id"])
        self.assertLessEqual(credential.expires_at, timezone.now())
        self.installation.refresh_from_db()
        self.assertEqual(self.installations.installation_payload(self.installation)["status"], "needs_setup")
        with self.assertRaises((PermissionDenied, AgentPermissionDenied)):
            self.adapter.principal_for(value)
        accepted = self.adapter.on_authenticated(value)
        credential.refresh_from_db()
        self.assertGreater(credential.expires_at, timezone.now())
        self.assertEqual(accepted.credential.pk, credential.pk)
        profile = self.adapter.call_tool(value, "get_agent_account", {})
        self.assertEqual(profile["id"], str(self.installation.pk))
        self.assertEqual(profile["email"], self.fixture.owner.email)
        subscription = self.subscribe(value)
        self.assertEqual(subscription.delivery["unified_credential_id"], str(credential.pk))
        invocation = self.fixture.invocation(native)
        thread = self.adapter.call_tool(value, "read_invocation_thread", {"invocation_id": str(invocation.pk)})
        self.assertEqual(thread["messages"][0]["content"], "Synthetic permitted thread")
        result = self.adapter.call_tool(value, "reply_to_invocation", {
            "invocation_id": str(invocation.pk), "text": "An exact scoped answer", "idempotency_key": uuid4().hex,
        })
        row = AgentInvocationReply.objects.get(invocation_id=result["invocation_id"])
        self.assertEqual(row.receipt["unified_credential_id"], str(credential.pk))
        self.assertEqual(self.invocations.dispatch_native_replies(), 1)
        row.refresh_from_db()
        self.assertEqual(row.status, "sent")
        self.installation.refresh_from_db()
        self.assertEqual(self.installations.installation_payload(self.installation)["status"], "ready")

    def test_fresh_consent_keeps_installation_but_revokes_earlier_sql_authority_and_work(self):
        first, first_native = self.consent()
        self.adapter.on_authenticated(first)
        first_scopes = list(first.grant["scopes"])
        subscription = self.subscribe(first)
        invocation, row = self.queued_reply(first, first_native)
        second, second_native = self.consent()
        first_native.refresh_from_db()
        invocation.refresh_from_db()
        row.refresh_from_db()
        subscription.refresh_from_db()
        self.assertEqual(second_native.installation_id, first_native.installation_id)
        self.assertNotEqual(second_native.pk, first_native.pk)
        self.assertIsNotNone(first_native.revoked_at)
        self.assertFalse(subscription.is_active)
        self.assertEqual(invocation.status, "cancelled")
        self.assertEqual(row.status, "cancelled")
        self.assertEqual(first.grant["scopes"], first_scopes)
        self.assertFalse(AgentCredential.objects.filter(grant=first_native, revoked_at__isnull=True).exists())
        with self.assertRaises((PermissionDenied, AgentPermissionDenied)):
            self.adapter.principal_for(first)
        self.adapter.on_authenticated(second)
        self.assertEqual(self.adapter.call_tool(second, "get_agent_account", {})["id"], str(self.installation.pk))

    def test_cache_loss_stops_real_queued_callbacks_and_replies_without_changing_signed_event(self):
        value, native = self.consent()
        self.adapter.on_authenticated(value)
        subscription = self.subscribe(value)
        _invocation, row = self.queued_reply(value, native)
        event = dict(row.event)
        self.current.clear()
        callback_attempts = []
        self.assertEqual(self.webhooks.dispatch_webhooks(
            transport=lambda *args: callback_attempts.append(args)), 0)
        self.assertEqual(self.invocations.dispatch_native_replies(), 0)
        row.refresh_from_db()
        subscription.refresh_from_db()
        self.assertEqual(row.status, "cancelled")
        self.assertEqual(row.event, event)
        self.assertFalse(subscription.is_active)
        self.assertEqual(callback_attempts, [])
        with self.assertRaises(AuthenticationFailed):
            self.adapter.principal_for(value)

    def test_missing_or_replaced_exact_binding_never_restores_standalone_authority(self):
        value, native = self.consent()
        self.adapter.on_authenticated(value)
        subscription = self.subscribe(value)
        _invocation, row = self.queued_reply(value, native)
        subscription.delivery = {**subscription.delivery, "unified_credential_id": str(uuid4())}
        subscription.save(update_fields=("delivery",))
        row.receipt = {"unified_credential_id": str(uuid4())}
        row.save(update_fields=("receipt",))
        self.assertEqual(self.webhooks.dispatch_webhooks(transport=lambda *args: self.fail("Callback must stop.")), 0)
        self.assertEqual(self.invocations.dispatch_native_replies(), 0)
        row.refresh_from_db()
        subscription.refresh_from_db()
        self.assertEqual(row.status, "cancelled")
        self.assertFalse(subscription.is_active)
        AgentOAuthBinding.objects.filter(credential_id=value.grant["native_agent_credential_id"]).delete()
        with self.assertRaises(PermissionDenied):
            self.adapter.principal_for(value)
        with self.assertRaises(AgentPermissionDenied):
            self.authority.assert_unified_authority(native)

    def test_operator_daily_limit_and_installation_global_admission_survive_reconsent(self):
        original = self.installation.grants.order_by("-created_at").first()
        original.daily_task_limit = 1
        original.max_concurrent_tasks = 1
        original.daily_cost_limit_microunits = 12345
        original.max_task_seconds = 47
        original.save(update_fields=("daily_task_limit", "max_concurrent_tasks",
                                   "daily_cost_limit_microunits", "max_task_seconds"))
        first, first_native = self.consent()
        self.adapter.on_authenticated(first)
        self.assertIsNotNone(self.fixture.invocation(first_native))
        second, second_native = self.consent()
        self.adapter.on_authenticated(second)
        self.assertEqual((second_native.daily_task_limit, second_native.max_concurrent_tasks,
                          second_native.daily_cost_limit_microunits, second_native.max_task_seconds),
                         (1, 1, 12345, 47))
        self.assertIsNone(self.fixture.invocation(second_native))
        self.assertEqual(AgentInvocation.objects.filter(installation=self.installation).count(), 1)
        self.assertEqual(AgentEvent.objects.filter(source="native",
            payload__installation_id=str(self.installation.pk)).count(), 1)

    def test_revoke_then_fresh_consent_reuses_completed_signed_installation(self):
        first, native = self.consent()
        self.adapter.on_authenticated(first)
        self.adapter.revoke_native_grant(first.grant)
        self.assertFalse(AgentGrant.objects.filter(installation=self.installation, revoked_at__isnull=True).exists())
        options = self.adapter.consent_installations(
            self.fixture.owner, SimpleNamespace(pk=first.grant["session_id"]))
        self.assertEqual([item["id"] for item in options], [str(self.installation.pk)])
        self.assertEqual(options[0]["resource"], RESOURCE)
        second, renewed = self.consent()
        self.assertEqual(renewed.installation_id, self.installation.pk)
        self.assertNotEqual(renewed.pk, native.pk)
        self.adapter.on_authenticated(second)
        self.assertEqual(self.adapter.call_tool(second, "get_agent_account", {})["id"], str(self.installation.pk))
