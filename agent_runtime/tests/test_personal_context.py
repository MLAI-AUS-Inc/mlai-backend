"""Personal conversation authority contracts without a database or network."""

from copy import copy
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4

from django.test import SimpleTestCase, override_settings
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.test import APIRequestFactory, force_authenticate

from agent_runtime import native_invocations as inv, native_sources, personal_context as pc, serializers
from agent_runtime.native_views import NativePersonalMentionView
from agent_runtime.policy import AgentPermissionDenied
from agent_runtime.tests.test_native_pilot import CHANNEL, COMMUNITY, CONFIG, NOW, RESOURCE, fixture

OTHER_CHANNEL = "33333333-3333-4333-8333-333333333333"


@override_settings(AGENT_RUNTIME_ENABLED=True, AGENT_RUNTIME_NATIVE_ENABLED=True,
    AGENT_RUNTIME_NATIVE_REPLIES_ENABLED=True, AGENT_RUNTIME_PERSONAL_CONTEXT_ENABLED=True,
    AGENT_RUNTIME_NATIVE_COMMUNITIES=CONFIG)
class PersonalContextTests(SimpleTestCase):
    def setUp(self):
        self.installation, self.grant, self.invocation = fixture()
        self.invocation.resource = pc.resource_for(COMMUNITY, OTHER_CHANNEL)
        self.metadata = {"community_id": COMMUNITY, "channel_id": OTHER_CHANNEL,
            "event_id": self.invocation.source_event_id, "root_event_id": self.invocation.thread_id,
            "author_pubkey": self.invocation.owner_public_key, "created_at": int(NOW.timestamp()),
            "context_policy": pc.CONTEXT_POLICY, "public": False}
        self.device = self.enterContext(patch.object(native_sources, "current_device", return_value=self.installation.owner_device))
        self.transport = self.enterContext(patch.object(native_sources, "_request", return_value={"trigger": self.metadata}))
        self.enterContext(patch.object(native_sources.timezone, "now", return_value=NOW))

    def test_resource_marker_cannot_expand_generic_grant(self):
        self.assertEqual(pc.actual_resource(self.invocation.resource), f"community:{COMMUNITY}:channel:{OTHER_CHANNEL}")
        self.assertEqual(pc.notification_resource(self.invocation), RESOURCE)
        from agent_runtime.policy import authorize
        with self.assertRaises(AgentPermissionDenied):
            authorize(self.grant, "messages:read", self.invocation.resource, access_checker=lambda *args: True, now=NOW)
        self.assertEqual(self.grant.resources, [RESOURCE])

    def test_noncanonical_marker_and_other_community_are_denied(self):
        for value in (self.invocation.resource + ":extra", self.invocation.resource.replace(COMMUNITY, str(uuid4()))):
            with self.subTest(value=value), self.assertRaises((ValidationError, AgentPermissionDenied)):
                pc.validate_mode(self.grant, value)

    def test_community_mode_cannot_get_personal_authority(self):
        self.installation.app_id = "mlai-community"
        with self.assertRaises(AgentPermissionDenied):
            pc.authorize_invocation(self.grant, self.invocation, "messages:read", now=NOW)
        self.transport.assert_not_called()

    @override_settings(AGENT_RUNTIME_PERSONAL_CONTEXT_ENABLED=False)
    def test_flag_off_denies_read_delivery_and_reply_context(self):
        for operation in (
            lambda: inv.validate_invocation(self.invocation, self.grant, now=NOW),
            lambda: pc.notification_resource(self.invocation),
            lambda: native_sources.read_invocation(self.invocation),
            lambda: native_sources.invocation_context(self.invocation, operation="reply", now=NOW),
        ):
            with self.assertRaises(AgentPermissionDenied):
                operation()
        self.transport.assert_not_called()

    @override_settings(AGENT_RUNTIME_PERSONAL_CONTEXT_ENABLED=False)
    def test_flag_off_skips_personal_work_without_blocking_ordinary_watch(self):
        with patch.object(inv.AgentInvocation, "objects") as rows:
            rows.get.return_value = self.invocation
            self.assertFalse(inv.invocation_deliverable(self.grant, self.invocation.pk, expected_resource=RESOURCE, now=NOW))
            self.invocation.resource = RESOURCE
            self.assertTrue(inv.invocation_deliverable(self.grant, self.invocation.pk, expected_resource=RESOURCE, now=NOW))
        self.transport.assert_not_called()

    def test_http_denial_is_per_invocation_not_retryable_watch_failure(self):
        self.transport.side_effect = PermissionDenied("forbidden")
        with patch.object(inv.AgentInvocation, "objects") as rows:
            rows.get.return_value = self.invocation
            self.assertFalse(inv.invocation_deliverable(self.grant, self.invocation.pk, expected_resource=RESOURCE, now=NOW))

    def test_personal_scope_requires_live_exact_signed_trigger(self):
        result = pc.authorize_invocation(self.grant, self.invocation, "messages:read", expected_generation=2, now=NOW)
        self.assertEqual(result, self.metadata)
        directive = self.transport.call_args.args[2][0]["native_agent"]
        self.assertEqual(directive["execution_mode"], "personal_context")
        self.assertEqual(directive["owner_pubkey"], self.invocation.owner_public_key)
        self.assertEqual(directive["trigger_event_id"], self.invocation.source_event_id)
        self.assertEqual(directive["root_event_id"], self.invocation.thread_id)
        self.assertEqual(directive["channel_id"], OTHER_CHANNEL)
        self.assertEqual(directive["installation_id"], str(self.installation.pk))
        self.assertNotIn("caller_pubkeys", directive)

    def test_swapped_owner_channel_root_source_or_policy_is_denied(self):
        for field, value in (("author_pubkey", "e" * 64), ("channel_id", CHANNEL),
                ("community_id", str(uuid4())), ("event_id", "e" * 64),
                ("root_event_id", "e" * 64), ("context_policy", "triggering_public_thread_only")):
            with self.subTest(field=field):
                self.transport.return_value = {"trigger": {**self.metadata, field: value}}
                with self.assertRaises(PermissionDenied):
                    pc.authorize_invocation(self.grant, self.invocation, "messages:read", now=NOW)

    def test_lost_membership_or_device_is_denied(self):
        self.transport.side_effect = PermissionDenied("membership changed")
        with self.assertRaises(PermissionDenied):
            pc.authorize_invocation(self.grant, self.invocation, "messages:read", now=NOW)
        self.transport.reset_mock()
        self.device.side_effect = PermissionDenied("revoked")
        with self.assertRaises(PermissionDenied):
            pc.authorize_invocation(self.grant, self.invocation, "messages:read", now=NOW)
        self.transport.assert_not_called()

    def test_outage_is_retryable_without_losing_notification(self):
        self.transport.side_effect = native_sources.SourceUnavailable()
        with patch.object(inv.AgentInvocation, "objects") as rows:
            rows.get.return_value = self.invocation
            with self.assertRaisesRegex(AgentPermissionDenied, "resource_unavailable"):
                inv.invocation_deliverable(self.grant, self.invocation.pk, expected_resource=RESOURCE, now=NOW)

    def test_notification_uses_existing_watch_without_private_identifiers(self):
        with patch.object(inv.AgentInvocation, "objects") as rows:
            rows.get.return_value = self.invocation
            self.assertTrue(inv.invocation_deliverable(self.grant, self.invocation.pk, expected_resource=RESOURCE, now=NOW))
            self.assertFalse(inv.invocation_deliverable(self.grant, self.invocation.pk,
                expected_resource=pc.actual_resource(self.invocation.resource), now=NOW))

    def test_context_read_uses_original_private_channel_and_bounds(self):
        self.transport.return_value = {"messages": [{"text": "Invitation immediately before the mention"}], "truncated": False}
        result = native_sources.read_invocation(self.invocation, limit=40)
        self.assertEqual(result["messages"][0]["text"], "Invitation immediately before the mention")
        directive = self.transport.call_args.args[2][0]["native_agent"]
        self.assertEqual((directive["operation"], directive["channel_id"], directive["limit"]), ("thread", OTHER_CHANNEL, 40))
        self.assertEqual(directive["execution_mode"], "personal_context")
        with self.assertRaises(ValidationError):
            native_sources.read_invocation(self.invocation, cursor="other-conversation")

    def test_pending_lookup_checks_each_conversation_and_reports_outage_honestly(self):
        principal = SimpleNamespace(grant=self.grant, owner=self.installation.owner,
            credential=SimpleNamespace(scopes=["messages:read"], generation=2))
        with patch.object(inv, "_current_principal", return_value=principal), \
                patch.object(inv.runtime, "locked", side_effect=lambda value: value), \
                patch.object(inv.AgentInvocation, "objects") as rows:
            rows.all.return_value.filter.return_value.order_by.return_value = [self.invocation]
            self.assertEqual(inv.list_pending_invocations.__wrapped__(principal)["pending_count"], 1)
            self.transport.side_effect = PermissionDenied("left conversation")
            self.assertEqual(inv.list_pending_invocations.__wrapped__(principal)["pending_count"], 0)
            self.transport.side_effect = native_sources.SourceUnavailable()
            with self.assertRaisesRegex(AgentPermissionDenied, "resource_unavailable"):
                inv.list_pending_invocations.__wrapped__(principal)

    def test_grant_generation_and_status_fence_personal_source_reads(self):
        for field, value in (("generation", 1), ("status", "cancelled"), ("expires_at", NOW - timedelta(seconds=1))):
            row = copy(self.invocation)
            setattr(row, field, value)
            with self.subTest(field=field), self.assertRaises(AgentPermissionDenied):
                inv.validate_invocation(row, self.grant, now=NOW)
        self.transport.assert_not_called()

    def test_reply_fixes_original_destination_and_personal_mode(self):
        event = inv.reply_event(self.invocation, "Added the requested event", created_at=int(NOW.timestamp()), now=NOW)
        self.assertIn(["h", OTHER_CHANNEL], event["tags"])
        import json
        context = json.loads(next(tag[1] for tag in event["tags"] if tag[0] == "native_invocation"))
        self.assertEqual(context["execution_mode"], "personal_context")
        self.assertEqual(context["trigger_event_id"], self.invocation.source_event_id)
        self.assertEqual(context["root_event_id"], self.invocation.thread_id)
        self.assertEqual(event["pubkey"], self.installation.public_key)

    def test_payload_reports_actual_destination_and_explicit_context_policy(self):
        result = inv.payload(self.invocation, now=NOW)
        self.assertEqual(result["resource"], pc.actual_resource(self.invocation.resource))
        self.assertEqual(result["context_policy"], pc.CONTEXT_POLICY)
        self.assertNotIn("messages", result)
        self.invocation.resource = RESOURCE
        self.assertEqual(inv.payload(self.invocation, now=NOW)["context_policy"], "triggering_public_thread_only")

    def test_owner_request_schema_rejects_supplied_content_or_agent_tools(self):
        data = {"community_id": COMMUNITY, "channel_id": OTHER_CHANNEL, "event_id": "a" * 64,
            "owner_public_key": self.invocation.owner_public_key}
        self.assertEqual(serializers.validated(serializers.PersonalMentionSerializer, data)["channel_id"], UUID(OTHER_CHANNEL))
        for field in ("messages", "calendar_id", "tools", "resource", "grant_id"):
            with self.subTest(field=field), self.assertRaises(ValidationError):
                serializers.validated(serializers.PersonalMentionSerializer, {**data, field: "unsupported"})

    def test_owner_endpoint_returns_accepted_and_rejects_anonymous(self):
        body = {"community_id": COMMUNITY, "channel_id": OTHER_CHANNEL, "event_id": "a" * 64,
            "owner_public_key": self.invocation.owner_public_key}
        request = APIRequestFactory().post("/mentions/", body, format="json")
        self.assertEqual(NativePersonalMentionView.as_view()(request, installation_id=self.installation.pk).status_code, 401)
        user = SimpleNamespace(pk=7, is_active=True, is_authenticated=True)
        request = APIRequestFactory().post("/mentions/", body, format="json")
        force_authenticate(request, user=user)
        with patch.object(pc, "submit", return_value=self.invocation):
            response = NativePersonalMentionView.as_view()(request, installation_id=self.installation.pk)
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.data["invocation_id"], str(self.invocation.pk))


@override_settings(AGENT_RUNTIME_ENABLED=True, AGENT_RUNTIME_NATIVE_ENABLED=True,
    AGENT_RUNTIME_NATIVE_REPLIES_ENABLED=True, AGENT_RUNTIME_PERSONAL_CONTEXT_ENABLED=True,
    AGENT_RUNTIME_NATIVE_COMMUNITIES=CONFIG)
class PersonalAdmissionTests(SimpleTestCase):
    def setUp(self):
        self.installation, self.grant, self.invocation = fixture()
        self.invocation.resource = pc.resource_for(COMMUNITY, OTHER_CHANNEL)
        self.data = {"community_id": UUID(COMMUNITY), "channel_id": UUID(OTHER_CHANNEL),
            "event_id": self.invocation.source_event_id, "owner_public_key": self.invocation.owner_public_key}
        self.metadata = {"community_id": COMMUNITY, "channel_id": OTHER_CHANNEL,
            "event_id": self.invocation.source_event_id, "root_event_id": self.invocation.thread_id,
            "author_pubkey": self.invocation.owner_public_key, "created_at": int(NOW.timestamp()),
            "context_policy": pc.CONTEXT_POLICY}
        self.installs = self.enterContext(patch.object(pc.AgentInstallation, "objects"))
        self.installs.get.return_value = self.installation
        grants = self.enterContext(patch.object(pc.AgentGrant, "objects"))
        grants.filter.return_value.order_by.return_value.values_list.return_value.first.return_value = self.grant.pk
        self.enterContext(patch.object(pc.runtime, "_grant", return_value=self.grant))
        self.authority = self.enterContext(patch("agent_runtime.unified_authority.assert_unified_authority"))
        self.enterContext(patch.object(native_sources, "current_device", return_value=self.installation.owner_device))
        self.transport = self.enterContext(patch.object(native_sources, "_request", return_value={"trigger": self.metadata}))
        self.rows = self.enterContext(patch.object(pc.AgentInvocation, "objects"))
        self.rows.filter.return_value.first.return_value = None
        self.rows.filter.return_value.count.return_value = 0
        self.rows.create.return_value = self.invocation
        self.ingest = self.enterContext(patch("agent_runtime.events.ingest_event"))
        self.enterContext(patch.object(pc.runtime, "audit"))

    def submit(self):
        return pc.submit.__wrapped__(self.installation.owner, self.installation.pk, self.data, now=NOW)

    def test_explicit_request_admits_once_and_notifies_existing_watch(self):
        self.assertEqual(self.submit(), self.invocation)
        created = self.rows.create.call_args.kwargs
        self.assertEqual(created["resource"], self.invocation.resource)
        self.assertEqual(created["owner_public_key"], self.invocation.owner_public_key)
        self.assertEqual(self.ingest.call_args.kwargs["resource"], RESOURCE)
        self.assertEqual(set(self.ingest.call_args.kwargs["payload"]), {"installation_id", "invocation_id", "source_event_id"})
        self.assertEqual(self.grant.resources, [RESOURCE])
        self.authority.assert_called_once_with(self.grant)

    def test_retry_returns_same_invocation_without_second_notification(self):
        self.rows.filter.return_value.first.return_value = self.invocation
        self.assertEqual(self.submit(), self.invocation)
        self.rows.create.assert_not_called()
        self.ingest.assert_not_called()

    def test_retry_cannot_change_generation_resource_or_caller(self):
        for field, value in (("generation", 1), ("resource", RESOURCE), ("owner_public_key", "e" * 64)):
            row = copy(self.invocation)
            setattr(row, field, value)
            self.rows.filter.return_value.first.return_value = row
            with self.subTest(field=field), self.assertRaises(ValidationError):
                self.submit()
        self.rows.create.assert_not_called()

    def test_quota_rejects_without_enqueue_or_notification(self):
        self.rows.filter.return_value.count.return_value = 20
        with self.assertRaisesRegex(AgentPermissionDenied, "daily_limit_reached"):
            self.submit()
        self.rows.create.assert_not_called()
        self.ingest.assert_not_called()

    def test_revocation_wrong_owner_and_missing_scope_prevent_source_read(self):
        for change in ("revoked", "owner", "scope"):
            with self.subTest(change=change):
                self.grant.revoked_at = NOW if change == "revoked" else None
                self.grant.scopes = ["messages:read"] if change == "scope" else ["events:read", "messages:read", "messages:reply"]
                owner = self.installation.owner if change != "owner" else SimpleNamespace(pk=99, is_active=True)
                with self.assertRaises(AgentPermissionDenied):
                    pc.submit.__wrapped__(owner, self.installation.pk, self.data, now=NOW)
        self.transport.assert_not_called()
        self.rows.create.assert_not_called()

    def test_expired_future_and_pregrant_triggers_are_rejected(self):
        for stamp in (NOW - timedelta(hours=1), NOW + timedelta(minutes=2), self.grant.created_at - timedelta(minutes=6)):
            self.transport.return_value = {"trigger": {**self.metadata, "created_at": int(stamp.timestamp())}}
            with self.subTest(stamp=stamp), self.assertRaises(ValidationError):
                self.submit()
        self.rows.create.assert_not_called()

    @override_settings(AGENT_RUNTIME_NATIVE_ALWAYS_ADMIT=True, AGENT_RUNTIME_NATIVE_QUEUE_TTL_MINUTES=1440,
        AGENT_RUNTIME_NATIVE_ADMISSION_DAILY_CAP=200, AGENT_RUNTIME_NATIVE_LIMIT_TIMEZONE="Australia/Melbourne")
    def test_queued_request_uses_existing_bounded_ttl(self):
        self.submit()
        self.assertEqual(self.rows.create.call_args.kwargs["expires_at"], self.grant.expires_at)
