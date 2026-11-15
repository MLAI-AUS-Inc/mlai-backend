"""Personal Dot renewal: owner re-signs before access ends, without a database."""

import time
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

from django.core.cache import cache
from django.test import SimpleTestCase, override_settings
from django.urls import resolve
from rest_framework.exceptions import PermissionDenied

from agent_runtime import native_installations as ni
from startup_updates.mcp import oauth

NOW = datetime(2026, 11, 15, 2, tzinfo=timezone.utc)
COMMUNITY = "11111111-1111-4111-8111-111111111111"
CHANNEL = "22222222-2222-4222-8222-222222222222"
RESOURCE = f"community:{COMMUNITY}:channel:{CHANNEL}"
LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "renewal-tests"}}
ON = {"AGENT_RUNTIME_NATIVE_RENEWAL_ENABLED": True}


def installation(**overrides):
    values = dict(pk=uuid4(), app_id="openai-dot", community_id=COMMUNITY, is_active=True, state="active",
                  owner_device_id=uuid4(), owner_attestation=["auth", "a" * 64, "created_at<1", "b" * 128])
    values.update(overrides)
    return SimpleNamespace(**values)


def grant(expires_at, **overrides):
    values = dict(pk=uuid4(), expires_at=expires_at, resources=[RESOURCE], generation=3, revoked_at=None)
    values.update(overrides)
    return SimpleNamespace(**values)


class RenewalWindowTests(SimpleTestCase):
    def test_flag_off_is_todays_behaviour(self):
        state = ni.renewal_state(installation(), grant(NOW + timedelta(days=1)), now=NOW)
        self.assertFalse(state["renewable"])
        self.assertFalse(state["lapsed"])

    @override_settings(**ON)
    def test_window_opens_seven_days_before_expiry(self):
        self.assertFalse(ni.renewal_state(installation(), grant(NOW + timedelta(days=8)), now=NOW)["renewable"])
        self.assertTrue(ni.renewal_state(installation(), grant(NOW + timedelta(days=7)), now=NOW)["renewable"])
        self.assertTrue(ni.renewal_state(installation(), grant(NOW + timedelta(hours=1)), now=NOW)["renewable"])

    @override_settings(**ON)
    def test_grace_after_expiry_then_lapsed(self):
        inside = ni.renewal_state(installation(), grant(NOW - timedelta(days=13)), now=NOW)
        self.assertTrue(inside["renewable"])
        self.assertFalse(inside["lapsed"])
        after = ni.renewal_state(installation(), grant(NOW - timedelta(days=14)), now=NOW)
        self.assertFalse(after["renewable"])
        self.assertTrue(after["lapsed"])
        self.assertEqual(after["renew_by"], NOW.isoformat())

    @override_settings(**ON)
    def test_revoked_paused_and_other_apps(self):
        soon = grant(NOW + timedelta(days=1))
        self.assertFalse(ni.renewal_state(installation(state="revoked"), soon, now=NOW)["renewable"])
        self.assertFalse(ni.renewal_state(installation(is_active=False), soon, now=NOW)["renewable"])
        self.assertTrue(ni.renewal_state(installation(state="paused"), soon, now=NOW)["renewable"])
        self.assertFalse(ni.renewal_state(installation(app_id="mlai-community"), soon, now=NOW)["renewable"])
        self.assertFalse(ni.renewal_state(installation(), None, now=NOW)["renewable"])


@override_settings(CACHES=LOCMEM)
class UnifiedExtensionTests(SimpleTestCase):
    def setUp(self):
        cache.clear()
        self.native_id = uuid4()
        self.grant_id = "g" * 40
        cache.set(oauth.key("grant", self.grant_id), {"id": self.grant_id, "user_id": 7, "revoked": False,
            "expiresAt": int(time.time()) + 3600, "native_agent_grant_id": str(self.native_id)}, timeout=3600)

    def test_extends_only_the_bound_owner_grant(self):
        until = time.time() + 20 * 86400
        self.assertIsNone(oauth.extend_native_grant(self.grant_id, native_grant_id=uuid4(), user_id=7, until=until))
        self.assertIsNone(oauth.extend_native_grant(self.grant_id, native_grant_id=self.native_id, user_id=8, until=until))
        result = oauth.extend_native_grant(self.grant_id, native_grant_id=self.native_id, user_id=7, until=until)
        self.assertEqual(result, int(until))
        self.assertEqual(cache.get(oauth.key("grant", self.grant_id))["expiresAt"], int(until))

    def test_never_beyond_thirty_days_or_backwards(self):
        far = time.time() + 90 * 86400
        result = oauth.extend_native_grant(self.grant_id, native_grant_id=self.native_id, user_id=7, until=far)
        self.assertLessEqual(result, time.time() + oauth.GRANT_TTL + 1)
        earlier = time.time() + 60
        self.assertEqual(oauth.extend_native_grant(self.grant_id, native_grant_id=self.native_id, user_id=7,
                                                   until=earlier), result)

    def test_revoked_or_missing_grant_is_not_revived(self):
        value = cache.get(oauth.key("grant", self.grant_id))
        cache.set(oauth.key("grant", self.grant_id), {**value, "revoked": True}, timeout=3600)
        self.assertIsNone(oauth.extend_native_grant(self.grant_id, native_grant_id=self.native_id, user_id=7,
                                                    until=time.time() + 86400))
        self.assertIsNone(oauth.extend_native_grant("missing", native_grant_id=self.native_id, user_id=7,
                                                    until=time.time() + 86400))

    def _bindings(self, ids):
        manager = MagicMock()
        manager.filter.return_value.values_list.return_value = ids
        return patch("agent_runtime.models.AgentOAuthBinding.objects", manager)

    @override_settings(MLAI_MCP_AGENT_GRANT_EXTENSION_ENABLED=True)
    def test_renewal_extends_bound_consent_and_caps_grant(self):
        native = SimpleNamespace(pk=self.native_id)
        until = datetime.now(timezone.utc) + timedelta(days=30)
        with self._bindings(["unified-mcp:" + self.grant_id]):
            expiry, reconnect = ni._extend_unified(native, SimpleNamespace(pk=7), until)
        self.assertFalse(reconnect)
        self.assertLessEqual(expiry, until)
        self.assertGreater(expiry, datetime.now(timezone.utc) + timedelta(days=29))

    def test_without_extension_flag_grant_stays_within_consent(self):
        native = SimpleNamespace(pk=self.native_id)
        until = datetime.now(timezone.utc) + timedelta(days=30)
        with self._bindings(["unified-mcp:" + self.grant_id]):
            expiry, reconnect = ni._extend_unified(native, SimpleNamespace(pk=7), until)
        self.assertFalse(reconnect)
        self.assertLess(expiry, datetime.now(timezone.utc) + timedelta(hours=2))

    @override_settings(MLAI_MCP_AGENT_GRANT_EXTENSION_ENABLED=True)
    def test_ended_consent_asks_for_reconnect_but_keeps_the_dot(self):
        cache.clear()
        until = datetime.now(timezone.utc) + timedelta(days=30)
        with self._bindings(["unified-mcp:" + self.grant_id]):
            expiry, reconnect = ni._extend_unified(SimpleNamespace(pk=self.native_id), SimpleNamespace(pk=7), until)
        self.assertTrue(reconnect)
        self.assertEqual(expiry, until)

    def test_standalone_grant_uses_signed_expiry(self):
        until = datetime.now(timezone.utc) + timedelta(days=30)
        with self._bindings([]):
            self.assertEqual(ni._extend_unified(SimpleNamespace(pk=self.native_id), SimpleNamespace(pk=7), until),
                             (until, False))


class RenewalFlowTests(SimpleTestCase):
    def setUp(self):
        self.device = SimpleNamespace(pk=uuid4(), public_key="c" * 64)
        self.inst = installation(owner_device=self.device, owner_device_id=self.device.pk)
        self.grant = grant(NOW + timedelta(days=2))
        self.grant.save = MagicMock()
        self.grant.credentials = MagicMock()
        self.grant.credentials.filter.return_value = []
        self.inst.save = MagicMock()
        self.enrollment = SimpleNamespace(pk=uuid4(), idempotency_key=ni.RENEWAL_PREFIX + "x", resource=RESOURCE,
            auth_conditions=f"created_at<{int((NOW + timedelta(days=30)).timestamp())}", confirmed_at=None,
            save=MagicMock())
        self.owner = SimpleNamespace(pk=7)

    def run_confirm(self, *, sources=True, relay=None, settings_extra=None):
        relay = relay or (lambda config, path, event: {"event_id": event["id"], "accepted": True})
        events = [{"id": "e1", "kind": 0}, {"id": "e2", "kind": 10100}]
        locked = MagicMock()
        locked.return_value.first.return_value = self.grant
        with ExitStack() as stack, override_settings(**{**ON, **(settings_extra or {})}):
            stack.enter_context(patch.object(ni, "dot_available", return_value=True))
            stack.enter_context(patch.object(ni, "latest_grant", return_value=self.grant))
            stack.enter_context(patch.object(ni.runtime, "locked", locked))
            stack.enter_context(patch.object(ni.runtime, "audit"))
            stack.enter_context(patch.object(ni.native_sources, "current_device", return_value=self.device))
            stack.enter_context(patch.object(ni.native_sources, "community_config",
                                             return_value=(COMMUNITY, {"relay_url": "https://chat.example"})))
            stack.enter_context(patch.object(ni.native_sources, "_request", side_effect=relay))
            stack.enter_context(patch.object(ni.native_crypto, "verify_enrollment"))
            stack.enter_context(patch.object(ni.native_crypto, "verify_owner_attestation",
                                             return_value=["auth", "c" * 64, self.enrollment.auth_conditions, "d" * 128]))
            stack.enter_context(patch.object(ni, "sources_available", return_value=sources))
            stack.enter_context(patch.object(ni, "_extend_unified", side_effect=lambda g, o, until: (until, False)))
            stack.enter_context(patch.object(ni, "_extend_leases"))
            stack.enter_context(patch.object(ni, "profile_events", return_value=events))
            stack.enter_context(patch("agent_runtime.models.AgentGrant.objects"))
            return ni._confirm_renewal(self.owner, self.inst, self.enrollment, event={}, auth_tag="[]", now=NOW)

    def test_renews_in_place_and_republishes_profile(self):
        self.run_confirm()
        self.assertEqual(self.grant.expires_at, NOW + timedelta(days=30))
        self.assertEqual(self.inst.owner_attestation[2], self.enrollment.auth_conditions)
        self.assertEqual(self.inst.profile_events[0]["id"], "e1")
        self.assertEqual(self.enrollment.confirmed_at, NOW)
        self.assertEqual(self.grant.resources, [RESOURCE])

    def test_renews_inside_grace(self):
        self.grant.expires_at = NOW - timedelta(days=3)
        self.run_confirm()
        self.assertEqual(self.grant.expires_at, NOW + timedelta(days=30))

    def test_refuses_after_grace(self):
        self.grant.expires_at = NOW - timedelta(days=15)
        with self.assertRaises(PermissionDenied):
            self.run_confirm()

    def test_refuses_revoked_installation(self):
        self.inst.state = "revoked"
        with self.assertRaises(PermissionDenied):
            self.run_confirm()

    def test_refuses_when_flag_off(self):
        with self.assertRaises(PermissionDenied):
            self.run_confirm(settings_extra={"AGENT_RUNTIME_NATIVE_RENEWAL_ENABLED": False})

    def test_refuses_channel_owner_lost(self):
        with self.assertRaises(PermissionDenied):
            self.run_confirm(sources=False)
        self.assertEqual(self.grant.expires_at, NOW + timedelta(days=2))

    def test_relay_refusal_does_not_confirm(self):
        with self.assertRaises(ni.native_sources.SourceUnavailable):
            self.run_confirm(relay=lambda config, path, event: {"event_id": event["id"], "accepted": False})
        self.assertIsNone(self.enrollment.confirmed_at)

    def test_retry_after_success_is_idempotent(self):
        self.run_confirm()
        self.inst.owner_attestation = ["auth", "c" * 64, self.enrollment.auth_conditions, "d" * 128]
        self.assertIs(self.run_confirm(), self.inst)


class LimitsTests(SimpleTestCase):
    def _rows(self, rows):
        query = MagicMock()
        query.exclude.return_value.exclude.return_value.distinct.return_value = query
        query.only.return_value = rows
        query.count.return_value = len(rows)
        return patch.object(ni.AgentInstallation.objects, "filter", return_value=query)

    def test_lapsed_dot_frees_the_slot_only_with_renewal_on(self):
        rows = [installation(), installation()]
        lapsed = {rows[0].pk}
        with self._rows(rows), patch.object(ni, "lapsed", side_effect=lambda row, now: row.pk in lapsed):
            self.assertEqual(ni.limits(SimpleNamespace(pk=7), COMMUNITY, now=NOW)["personal_used"], 2)
            with override_settings(**ON):
                self.assertEqual(ni.limits(SimpleNamespace(pk=7), COMMUNITY, now=NOW)["personal_used"], 1)


class RouteTests(SimpleTestCase):
    def test_renew_route(self):
        match = resolve(f"/api/v1/agent-runtime/installations/{uuid4()}/renew/")
        self.assertEqual(match.func.view_class.__name__, "NativeInstallationRenewView")


class PromptTests(SimpleTestCase):
    def test_wake_and_schedule_prompts_use_the_pending_tool(self):
        self.assertIn("list_pending_invocations", ni.WAKE_PROMPT)
        self.assertIn("list_pending_invocations", ni.SCHEDULE_PROMPT)
        self.assertIn("no end date", ni.WAKE_PROMPT)
