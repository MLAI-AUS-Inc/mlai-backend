"""Real PostgreSQL Dot transactions, explicitly gated to the disposable runner.

Source and callback transports use synthetic fixtures. ORM writes, constraints,
locks, OAuth credentials, committed profiles and outbox state are real.
"""

import base64
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from collections import Counter
from datetime import timedelta
import hashlib
import json
import os
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

from coincurve import PrivateKey
from django.contrib.auth import get_user_model
from django.db import IntegrityError, close_old_connections, connection, transaction
from django.test import override_settings
from django.utils import timezone
from rest_framework.exceptions import AuthenticationFailed, PermissionDenied, ValidationError

from agent_runtime import broker, event_webhooks, native_crypto, native_installations, native_invocations, native_sources, oauth, runtime
from agent_runtime.authentication import token_hash, validate_credential
from agent_runtime.models import AgentCredential, AgentEnrollment, AgentEvent, AgentGrant, AgentInstallation, AgentInvocation, AgentInvocationReply, AgentSubscription
from agent_runtime.policy import AgentPermissionDenied
from agent_runtime.webhook_transport import CallbackEndpointError, WebhookResponse
from community_chat.models import CommunityChatDevice

COMMUNITY = "11111111-1111-4111-8111-111111111111"
CHANNEL = "22222222-2222-4222-8222-222222222222"
RESOURCE = f"community:{COMMUNITY}:channel:{CHANNEL}"
AUDIENCE = "https://gateway.example/api/v1/agent-runtime/mcp/"
SECRET = "whsec_" + base64.b64encode(b"synthetic-dot-database-callback-key").decode()
TRANSPORT_COUNTS = Counter()


@unittest.skipUnless(os.environ.get("MLAI_AGENT_DISPOSABLE_DB_TESTS") == "1", "Use the reviewed disposable PostgreSQL runner.")
class NativeDatabaseTests(unittest.TestCase):
    def setUp(self):
        database = connection.settings_dict
        self.assertEqual((database["HOST"], str(database["PORT"]), database["NAME"], database["USER"]),
                         ("127.0.0.1", "58331", "mlai_dot_test", "dotpilot"))
        self.assertEqual(connection.vendor, "postgresql")
        settings = override_settings(AGENT_RUNTIME_ENABLED=True, AGENT_RUNTIME_NATIVE_ENABLED=True,
            AGENT_RUNTIME_NATIVE_REPLIES_ENABLED=True, AGENT_RUNTIME_MCP_EVENTS_ENABLED=True,
            AGENT_RUNTIME_NATIVE_PERSONAL_LIMIT=1, AGENT_RUNTIME_RESOURCE=AUDIENCE,
            AGENT_RUNTIME_DOT_SETUP_URL="https://provider.example/pilot",
            AGENT_RUNTIME_NATIVE_COMMUNITIES={COMMUNITY: {"relay_url": "https://chat.example", "channel_ids": [CHANNEL]}},
            AGENT_RUNTIME_OAUTH_CLIENTS={"dot-pilot": {"enabled": True, "app_id": "openai-dot",
                "name": "Synthetic Dot", "redirect_uris": ["https://provider.example/oauth"],
                "scopes": native_installations.DOT_SCOPES}})
        settings.enable()
        self.addCleanup(settings.disable)
        self.owner_key = PrivateKey()
        self.owner = get_user_model().objects.create(email=f"dot-{uuid4().hex}@example.test", is_active=True)
        self.device = CommunityChatDevice.objects.create(user=self.owner,
            public_key=native_crypto.public_key(self.owner_key), status="verified", verified_at=timezone.now())
        self.membership = True
        self.allowed_keys = {self.device.public_key}
        self.source_events = []
        self.source_calls = []
        self.receipts = []
        self.epoch = str(uuid4())
        self.source_patch = patch("agent_runtime.native_sources._request", side_effect=self.source)
        self.source_patch.start()
        self.addCleanup(self.source_patch.stop)

    def tearDown(self):
        get_user_model().objects.filter(pk=self.owner.pk).update(is_active=True)
        self.owner.refresh_from_db()
        for installation in AgentInstallation.objects.filter(owner=self.owner):
            runtime.revoke_installation(installation.pk, owner=self.owner)
        close_old_connections()

    def source(self, _config, path, payload):
        self.source_calls.append((path, json.loads(json.dumps(payload))))
        if path == "/events":
            self.receipts.append(payload["id"])
            TRANSPORT_COUNTS["profile_receipts" if payload["kind"] in {0, 10100} else "reply_receipts"] += 1
            return {"accepted": True, "event_id": payload["id"]}
        directive = payload[0]["native_agent"]
        if directive["operation"] == "channels":
            return {"channels": [{"id": CHANNEL, "name": "dot-pilot"}]
                    if self.membership and directive["owner_pubkey"] in self.allowed_keys else []}
        if directive["operation"] == "mentions":
            return {"events": list(self.source_events), "cursor": {"epoch": self.epoch, "sequence": len(self.source_events)}}
        if directive["operation"] == "thread":
            return {"messages": [{"id": directive["trigger_event_id"], "content": "Synthetic permitted thread"}], "truncated": False}
        raise AssertionError(f"Unexpected source operation: {directive['operation']}")

    def reservation(self, *, key=None, name="My Dot"):
        return native_installations.enroll(self.owner, {"community_id": COMMUNITY, "app_id": "openai-dot",
            "name": name, "resource": RESOURCE, "idempotency_key": key or uuid4().hex,
            "owner_public_key": self.device.public_key, "consent_version": native_installations.CONSENT_VERSION})

    def proof(self, enrollment, *, owner_key=None):
        owner_key = owner_key or self.owner_key
        payload = native_installations.enrollment_payload(enrollment)
        challenge = payload["challenge"]
        signed = native_crypto.sign_event(owner_key, kind=challenge["kind"], created_at=challenge["created_at"],
            tags=challenge["tags"], content=challenge["content"])
        digest = hashlib.sha256(f"nostr:agent-auth:{payload['bot_public_key']}:{payload['auth_conditions']}".encode()).digest()
        tag = ["auth", native_crypto.public_key(owner_key), payload["auth_conditions"], owner_key.sign_schnorr(digest).hex()]
        return signed, json.dumps(tag)

    def active(self):
        enrollment = self.reservation()
        event, tag = self.proof(enrollment)
        installation = native_installations.confirm(self.owner, enrollment.pk, event=event, auth_tag=tag)
        self.assertEqual(installation.state, "provisioning")
        self.assertEqual(native_installations.provision_native_installations(), 1)
        installation.refresh_from_db()
        grant = installation.grants.get()
        token, _refresh = oauth.issue_pair(grant, client_id="dot-pilot", scopes=grant.scopes, audience=AUDIENCE)
        credential = AgentCredential.objects.select_related("grant__installation__owner", "task").get(token_hash=token_hash(token["access_token"]))
        principal = validate_credential(credential, audience=AUDIENCE, allowed_kinds=("access",))
        return installation, grant, principal

    def metadata(self, *, caller=None, event_id=None):
        return {"community_id": COMMUNITY, "channel_id": CHANNEL, "author_pubkey": caller or self.device.public_key,
                "event_id": event_id or uuid4().hex * 2, "root_event_id": "f" * 64,
                "created_at": int(timezone.now().timestamp())}

    def invocation(self, grant, *, caller=None):
        with transaction.atomic():
            locked = runtime._grant(grant.pk)
            return native_invocations.admit_metadata(locked, self.metadata(caller=caller))

    def subscription(self, principal):
        params = {"name": "agent.mentioned", "arguments": {"installation_id": str(principal.grant.installation_id), "resource": RESOURCE},
                  "delivery": {"mode": "webhook", "url": "https://provider.example/events", "secret": SECRET},
                  "ttlMs": 60 * 60 * 1000}
        def callback(_url, body, _headers):
            request = json.loads(body)
            TRANSPORT_COUNTS["callback_verification_receipts"] += 1
            return WebhookResponse(200, json.dumps({"challenge": request.get("challenge")}).encode())
        with patch("agent_runtime.event_webhooks.resolve_callback", return_value=[]):
            result = event_webhooks.subscribe(principal, params, transport=callback)
        return AgentSubscription.objects.get(pk=result["id"])

    def test_slot_race_serializes_real_postgres_owner_lock(self):
        barrier = threading.Barrier(2)
        def claim(index):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                return str(self.reservation(key=f"race-{index}").installation_id)
            except PermissionDenied:
                return "quota_denied"
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as pool:
            result = list(pool.map(claim, range(2)))
        self.assertEqual(result.count("quota_denied"), 1)
        self.assertEqual(AgentInstallation.objects.filter(owner=self.owner, is_active=True).count(), 1)
        self.assertEqual(native_installations.limits(self.owner, COMMUNITY)["personal_used"], 1)

    def test_reservation_retry_keeps_installation_and_expires_slot(self):
        original = self.reservation(key="same-request")
        self.assertEqual(self.reservation(key="same-request").pk, original.pk)
        with self.assertRaises(ValidationError):
            self.reservation(key="same-request", name="Changed consent")
        AgentEnrollment.objects.filter(pk=original.pk).update(expires_at=timezone.now() - timedelta(seconds=1))
        replacement = self.reservation(key="new-request")
        original.installation.refresh_from_db()
        self.assertEqual(original.installation.state, "revoked")
        self.assertEqual(original.installation.sealed_signing_key, "")
        self.assertNotEqual(replacement.installation_id, original.installation_id)

    def test_signed_proof_commits_profiles_and_retry_preserves_them(self):
        enrollment = self.reservation()
        event, tag = self.proof(enrollment)
        installation = native_installations.confirm(self.owner, enrollment.pk, event=event, auth_tag=tag)
        profiles = installation.profile_events
        self.assertEqual([profile["kind"] for profile in profiles], [0, 10100])
        self.assertEqual(len(installation.grants.all()), 1)
        self.assertEqual(self.receipts, [])
        def committed_profiles():
            close_old_connections()
            try:
                return AgentInstallation.objects.get(pk=installation.pk).profile_events
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=1) as pool:
            self.assertEqual(pool.submit(committed_profiles).result(timeout=10), profiles)
        retry_event, retry_tag = self.proof(enrollment)
        retry = native_installations.confirm(self.owner, enrollment.pk, event=retry_event, auth_tag=retry_tag)
        self.assertEqual(retry.pk, installation.pk)
        self.assertEqual(retry.profile_events, profiles)
        self.assertEqual(retry.grants.count(), 1)
        self.assertEqual(native_installations.provision_native_installations(), 1)
        self.assertEqual(self.receipts, [profile["id"] for profile in profiles])

    def test_invalid_proof_does_not_commit_grant_or_profiles(self):
        enrollment = self.reservation()
        event, tag = self.proof(enrollment, owner_key=PrivateKey())
        with self.assertRaises(PermissionDenied):
            native_installations.confirm(self.owner, enrollment.pk, event=event, auth_tag=tag)
        enrollment.installation.refresh_from_db()
        self.assertEqual(enrollment.installation.profile_events, [])
        self.assertEqual(enrollment.installation.grants.count(), 0)

    def test_oauth_reconnect_reuses_installation_and_refresh_replay_fences_family(self):
        installation, grant, _principal = self.active()
        arguments = {"name": "My Dot", "runtime": "external", "scopes": list(grant.scopes), "resources": [RESOURCE],
                     "expires_in_days": 30, "installation_id": str(installation.pk), "client_app_id": "openai-dot"}
        self.assertEqual(oauth.create_grant(self.owner, **arguments).pk, grant.pk)
        self.assertEqual(oauth.create_grant(self.owner, **arguments).installation_id, installation.pk)
        self.assertEqual(AgentInstallation.objects.filter(owner=self.owner).count(), 1)
        pair, refresh = oauth.issue_pair(grant, client_id="dot-pilot", scopes=grant.scopes, audience=AUDIENCE)
        rotated = oauth.rotate_refresh({"client_id": "dot-pilot", "resource": AUDIENCE, "refresh_token": pair["refresh_token"]})
        with self.assertRaises(AuthenticationFailed):
            oauth.rotate_refresh({"client_id": "dot-pilot", "resource": AUDIENCE, "refresh_token": pair["refresh_token"]})
        self.assertFalse(AgentCredential.objects.filter(grant=grant, refresh_family=refresh.refresh_family, revoked_at__isnull=True).exists())
        self.assertNotEqual(rotated["refresh_token"], pair["refresh_token"])

    def test_invocation_and_reply_retry_keep_one_signed_event(self):
        installation, grant, principal = self.active()
        metadata = self.metadata()
        with transaction.atomic():
            locked = runtime._grant(grant.pk)
            first = native_invocations.admit_metadata(locked, metadata)
            duplicate = native_invocations.admit_metadata(locked, metadata)
        self.assertEqual(first.pk, duplicate.pk)
        self.assertEqual(AgentEvent.objects.filter(source="native", source_event_id=str(first.pk)).count(), 1)
        reply = native_invocations.reply_to_invocation(principal, first.pk, "Synthetic answer", "same-reply")
        event = AgentInvocationReply.objects.get(invocation=first).event
        self.assertEqual(native_invocations.reply_to_invocation(principal, first.pk, "Synthetic answer", "same-reply")["event_id"], reply["event_id"])
        with self.assertRaises(ValidationError):
            native_invocations.reply_to_invocation(principal, first.pk, "Changed answer", "same-reply")
        self.assertEqual(AgentInvocationReply.objects.filter(invocation=first).count(), 1)
        self.assertEqual(native_invocations.dispatch_native_replies(), 1)
        first.refresh_from_db()
        self.assertEqual(first.status, "replied")
        self.assertEqual(AgentInvocationReply.objects.get(invocation=first).event, event)
        self.assertEqual(self.receipts.count(reply["event_id"]), 1)

    def test_uncertain_reply_resends_identical_committed_event(self):
        _installation, grant, principal = self.active()
        invocation = self.invocation(grant)
        native_invocations.reply_to_invocation(principal, invocation.pk, "Synthetic retry", "uncertain")
        row = AgentInvocationReply.objects.get(invocation=invocation)
        signed = row.event
        def accepted_then_lost_receipt(reply):
            self.receipts.append(reply.event["id"])
            TRANSPORT_COUNTS["ambiguous_reply_attempts"] += 1
            raise native_sources.SourceUnavailable()
        with patch("agent_runtime.native_sources.send_reply", side_effect=accepted_then_lost_receipt):
            self.assertEqual(native_invocations.dispatch_native_replies(), 0)
        row.refresh_from_db()
        self.assertEqual(row.status, "unknown")
        self.assertEqual(row.event, signed)
        self.assertEqual(native_invocations.dispatch_native_replies(), 1)
        row.refresh_from_db()
        self.assertEqual(row.status, "sent")
        self.assertEqual(row.event, signed)
        self.assertEqual(self.receipts[-2:], [signed["id"], signed["id"]])

    def test_second_verified_device_is_actual_caller_and_outsider_is_denied(self):
        _installation, grant, principal = self.active()
        second_key = PrivateKey()
        second = CommunityChatDevice.objects.create(user=self.owner, public_key=native_crypto.public_key(second_key),
            status="verified", verified_at=timezone.now())
        self.allowed_keys.add(second.public_key)
        invocation = self.invocation(grant, caller=second.public_key)
        self.assertEqual(invocation.owner_public_key, second.public_key)
        self.assertEqual(native_invocations.get_invocation(principal, invocation.pk)["caller_public_key"], second.public_key)
        self.assertTrue(native_invocations.read_invocation_thread(principal, invocation.pk)["messages"])
        outsider = get_user_model().objects.create(email=f"outsider-{uuid4().hex}@example.test")
        outsider_device = CommunityChatDevice.objects.create(user=outsider, public_key=native_crypto.public_key(PrivateKey()),
            status="verified", verified_at=timezone.now())
        with transaction.atomic(), self.assertRaises(PermissionDenied):
            native_invocations.admit_metadata(runtime._grant(grant.pk), self.metadata(caller=outsider_device.public_key))
        CommunityChatDevice.objects.filter(pk=second.pk).update(status="revoked", revoked_at=timezone.now())
        with self.assertRaises(PermissionDenied):
            native_invocations.get_invocation(principal, invocation.pk)

    def test_membership_loss_and_expiry_prevent_context_and_reply(self):
        _installation, grant, principal = self.active()
        invocation = self.invocation(grant)
        self.membership = False
        with self.assertRaises(AgentPermissionDenied):
            native_invocations.read_invocation_thread(principal, invocation.pk)
        with self.assertRaises(AgentPermissionDenied):
            native_invocations.reply_to_invocation(principal, invocation.pk, "Denied", "denied")
        self.membership = True
        AgentInvocation.objects.filter(pk=invocation.pk).update(expires_at=timezone.now() - timedelta(seconds=1))
        with self.assertRaises(AgentPermissionDenied):
            native_invocations.read_invocation_thread(principal, invocation.pk)
        self.assertFalse(AgentInvocationReply.objects.filter(invocation=invocation).exists())

    def test_pause_revoke_cancel_outbox_and_fence_credentials_callbacks(self):
        installation, grant, principal = self.active()
        subscription = self.subscription(principal)
        invocation = self.invocation(grant)
        native_invocations.reply_to_invocation(principal, invocation.pk, "Never send after pause", "pause-reply")
        native_installations.change_state(self.owner, installation.pk, "paused")
        invocation.refresh_from_db()
        subscription.refresh_from_db()
        self.assertEqual(invocation.status, "cancelled")
        self.assertFalse(subscription.is_active)
        self.assertEqual(AgentInvocationReply.objects.get(invocation=invocation).status, "cancelled")
        callback = unittest.mock.Mock(return_value=WebhookResponse(200, b"{}"))
        self.assertEqual(event_webhooks.dispatch_webhooks(transport=callback), 0)
        callback.assert_not_called()
        native_installations.change_state(self.owner, installation.pk, "active")
        native_installations.provision_native_installations()
        runtime.revoke_installation(installation.pk, owner=self.owner)
        installation.refresh_from_db()
        grant.refresh_from_db()
        self.assertEqual(installation.state, "revoked")
        self.assertEqual(installation.sealed_signing_key, "")
        self.assertEqual(grant.generation, 2)
        self.assertFalse(AgentCredential.objects.filter(grant=grant, revoked_at__isnull=True).exists())
        with self.assertRaises(AuthenticationFailed):
            native_invocations.get_invocation(principal, invocation.pk)

    def test_unknown_reply_stopped_by_revoke_is_not_claimed_unsent(self):
        installation, grant, principal = self.active()
        invocation = self.invocation(grant)
        native_invocations.reply_to_invocation(principal, invocation.pk, "Ambiguous send", "ambiguous")
        AgentInvocationReply.objects.filter(invocation=invocation).update(status="unknown")
        runtime.revoke_installation(installation.pk, owner=self.owner)
        row = AgentInvocationReply.objects.get(invocation=invocation)
        self.assertEqual(row.status, "stopped_unknown")
        self.assertEqual(native_invocations.dispatch_native_replies(), 0)

    def test_resubscribed_binding_needs_new_reply_after_pause(self):
        installation, grant, principal = self.active()
        self.subscription(principal)
        invocation = self.invocation(grant)
        native_invocations.reply_to_invocation(principal, invocation.pk, "First run", "first-run")
        native_invocations.dispatch_native_replies()
        installation.refresh_from_db()
        self.assertEqual(native_installations.installation_payload(installation)["status"], "ready")
        native_installations.change_state(self.owner, installation.pk, "paused")
        native_installations.change_state(self.owner, installation.pk, "active")
        self.subscription(principal)
        installation.refresh_from_db()
        summary = native_installations.installation_payload(installation)
        self.assertFalse(summary["test_completed"])
        self.assertNotEqual(summary["status"], "ready")
        self.assertIsNotNone(summary["last_reply_at"])

    def test_postgres_uniqueness_enforces_invocation_and_enrollment_identity(self):
        _installation, grant, _principal = self.active()
        invocation = self.invocation(grant)
        with self.assertRaises(IntegrityError), transaction.atomic():
            AgentInvocation.objects.create(installation=invocation.installation, grant=grant, generation=grant.generation,
                resource=RESOURCE, source_event_id=invocation.source_event_id, thread_id=invocation.thread_id,
                owner_public_key=self.device.public_key, expires_at=timezone.now() + timedelta(minutes=1))
        enrollment = AgentEnrollment.objects.get(installation=invocation.installation)
        with self.assertRaises(IntegrityError), transaction.atomic():
            AgentEnrollment.objects.create(installation=enrollment.installation, idempotency_key=enrollment.idempotency_key,
                resource=RESOURCE, consent_version=enrollment.consent_version, nonce="another", auth_conditions=enrollment.auth_conditions,
                expires_at=enrollment.expires_at)

    def test_postgres_key_credential_and_generation_constraints(self):
        installation, grant, principal = self.active()
        with self.assertRaises(IntegrityError), transaction.atomic():
            AgentInstallation.objects.create(owner=self.owner, name="Conflicting bot", public_key=installation.public_key)
        with self.assertRaises(IntegrityError), transaction.atomic():
            AgentCredential.objects.create(grant=grant, kind="access", token_hash=principal.credential.token_hash,
                generation=grant.generation, audience=AUDIENCE, scopes=grant.scopes, expires_at=grant.expires_at)
        with self.assertRaises(IntegrityError), transaction.atomic():
            AgentGrant.objects.create(installation=installation, generation=0, scopes=grant.scopes,
                resources=grant.resources, expires_at=grant.expires_at)

    def test_inflight_webhook_holds_revoke_fence_on_real_postgres(self):
        installation, grant, principal = self.active()
        subscription = self.subscription(principal)
        invocation = self.invocation(grant)
        entered, release, revoking = threading.Event(), threading.Event(), threading.Event()
        deliveries = []
        def callback(_url, body, _headers):
            deliveries.append(json.loads(body))
            entered.set()
            if not release.wait(timeout=10):
                raise AssertionError("The test did not release its bounded callback fixture.")
            TRANSPORT_COUNTS["callback_delivery_receipts"] += 1
            return WebhookResponse(200, b"{}")
        def dispatch():
            close_old_connections()
            try:
                return event_webhooks.dispatch_webhooks(transport=callback)
            finally:
                close_old_connections()
        def revoke():
            close_old_connections()
            try:
                revoking.set()
                return runtime.revoke_installation(installation.pk, owner=self.owner)
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as pool:
            delivery = pool.submit(dispatch)
            try:
                self.assertTrue(entered.wait(timeout=10))
                revocation = pool.submit(revoke)
                self.assertTrue(revoking.wait(timeout=10))
                with self.assertRaises(TimeoutError):
                    revocation.result(timeout=0.2)
            finally:
                release.set()
            self.assertEqual(delivery.result(timeout=10), 1)
            revocation.result(timeout=10)
        subscription.refresh_from_db()
        self.assertFalse(subscription.is_active)
        self.assertEqual(len(deliveries), 1)
        with self.assertRaises(AuthenticationFailed):
            native_invocations.get_invocation(principal, invocation.pk)
        blocked = unittest.mock.Mock(return_value=WebhookResponse(200, b"{}"))
        self.assertEqual(event_webhooks.dispatch_webhooks(transport=blocked), 0)
        blocked.assert_not_called()

    def test_broker_tools_after_refresh_pin_enrolled_source_identity(self):
        installation, grant, _principal = self.active()
        CommunityChatDevice.objects.create(user=self.owner, public_key=native_crypto.public_key(PrivateKey()),
            status="verified", verified_at=timezone.now() + timedelta(seconds=1))
        # The newest verified account key has not joined this channel. Consent
        # remains bound to the original enrollment key, which is still verified.
        pair, _refresh = oauth.issue_pair(grant, client_id="dot-pilot", scopes=grant.scopes, audience=AUDIENCE)
        rotated = oauth.rotate_refresh({"client_id": "dot-pilot", "resource": AUDIENCE,
                                        "refresh_token": pair["refresh_token"]})
        credential = AgentCredential.objects.select_related("grant__installation__owner", "task").get(
            token_hash=token_hash(rotated["access_token"]))
        principal = validate_credential(credential, audience=AUDIENCE, allowed_kinds=("access",))
        self.assertEqual(broker.execute(principal, "get_profile", {})["agent_id"], str(installation.pk))
        channels = broker.execute(principal, "list_channels", {})["channels"]
        self.assertEqual(len(channels), 1)
        invocation = self.invocation(grant)
        self.assertTrue(broker.execute(principal, "read_invocation_thread", {"invocation_id": str(invocation.pk)})["messages"])
        reply = broker.execute(principal, "reply_to_invocation", {"invocation_id": str(invocation.pk),
            "text": "After refresh", "idempotency_key": "refreshed-reply"})
        self.assertEqual(reply["status"], "pending")
        self.assertEqual(native_invocations.dispatch_native_replies(), 1)
        self.assertEqual(broker.execute(principal, "get_invocation_status", {"invocation_id": str(invocation.pk)})["reply"]["status"], "sent")

    def test_operator_shutdown_cannot_report_ready_or_deliver(self):
        installation, grant, principal = self.active()
        self.subscription(principal)
        invocation = self.invocation(grant)
        native_invocations.reply_to_invocation(principal, invocation.pk, "Before shutdown", "before-shutdown")
        native_invocations.dispatch_native_replies()
        installation.refresh_from_db()
        self.assertEqual(native_installations.installation_payload(installation)["status"], "ready")
        with override_settings(AGENT_RUNTIME_NATIVE_ENABLED=False):
            summary = native_installations.installation_payload(installation)
            self.assertNotEqual(summary["status"], "ready")
            self.assertFalse(summary["subscription_active"])
            self.assertEqual(native_invocations.dispatch_native_replies(), 0)

    def test_daily_admission_limit_preserves_replay_without_extra_provider_event(self):
        installation, grant, _principal = self.active()
        AgentGrant.objects.filter(pk=grant.pk).update(daily_task_limit=1)
        metadata = self.metadata()
        with transaction.atomic():
            locked = runtime._grant(grant.pk)
            first = native_invocations.admit_metadata(locked, metadata)
            replay = native_invocations.admit_metadata(locked, metadata)
            denied = native_invocations.admit_metadata(locked, self.metadata())
        self.assertEqual(first.pk, replay.pk)
        self.assertIsNone(denied)
        self.assertEqual(AgentInvocation.objects.filter(installation=installation).count(), 1)
        self.assertEqual(AgentEvent.objects.filter(source="native", payload__installation_id=str(installation.pk)).count(), 1)
        self.assertFalse(AgentInvocationReply.objects.filter(invocation__installation=installation).exists())

    def test_concurrent_daily_admissions_spend_only_one_real_locked_slot(self):
        installation, grant, _principal = self.active()
        AgentGrant.objects.filter(pk=grant.pk).update(daily_task_limit=1)
        barrier = threading.Barrier(2)
        def request(_index):
            close_old_connections()
            try:
                metadata = self.metadata()
                barrier.wait(timeout=10)
                with transaction.atomic():
                    invocation = native_invocations.admit_metadata(runtime._grant(grant.pk), metadata)
                return str(invocation.pk) if invocation else None
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(request, range(2)))
        self.assertEqual(results.count(None), 1)
        self.assertEqual(AgentInvocation.objects.filter(installation=installation).count(), 1)
        self.assertEqual(AgentEvent.objects.filter(source="native", payload__installation_id=str(installation.pk)).count(), 1)
