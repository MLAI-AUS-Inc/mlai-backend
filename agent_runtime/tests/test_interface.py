"""Database/network-free tests for API and delegated-access trust boundaries."""
from contextlib import contextmanager, nullcontext, ExitStack
from datetime import timedelta
import base64
import hashlib
from types import SimpleNamespace as NS
from unittest.mock import MagicMock, patch
import uuid

from django.test import SimpleTestCase, override_settings
from django.template.loader import render_to_string
from django.utils import timezone
from rest_framework.exceptions import AuthenticationFailed, PermissionDenied, ValidationError
from rest_framework.test import APIRequestFactory, force_authenticate

from agent_runtime.authentication import AgentPrincipal, OwnerAuthentication, validate_credential
from agent_runtime.broker import require_scope
from agent_runtime import admission, oauth, sources
from agent_runtime.mcp import MCPView
from agent_runtime.views import ConnectedAgentsView, IngestEventView, SlackAdmissionView, CancelTaskView, decode_artifact
from agent_runtime.serializers import SearchSerializer, EventsSerializer, TaskCreateSerializer, SlackAdmissionSerializer, FailSerializer, SubscriptionSerializer, validated


def authority():
    now = timezone.now()
    owner = NS(pk=7, is_active=True, is_authenticated=True)
    installation = NS(owner=owner, is_active=True, name="Roo", pk=uuid.uuid4())
    grant = NS(pk=uuid.uuid4(), installation=installation, generation=2, revoked_at=None,
               expires_at=now + timedelta(days=1), scopes=["messages:read", "events:read"],
               resources=["slack:T123:C123", "slack:T123:C456"])
    credential = NS(grant=grant, kind="access", audience="https://api.example/mcp/",
                    revoked_at=None, expires_at=now + timedelta(minutes=15), generation=2,
                    scopes=list(grant.scopes), task_id=None, task=None, lease_epoch=None)
    return credential


class CredentialTests(SimpleTestCase):
    def test_owner_grant_and_audience_must_all_remain_valid(self):
        credential = authority()
        principal = validate_credential(credential, audience=credential.audience, allowed_kinds=("access",))
        self.assertEqual(principal.owner.pk, 7)
        cases = [("audience", "other"), ("generation", 1), ("expires_at", timezone.now() - timedelta(seconds=1)),
                 ("revoked_at", timezone.now()), ("scopes", ["messages:write"]), ("kind", "task_model")]
        for field, value in cases:
            current = authority()
            setattr(current, field, value)
            with self.subTest(field=field), self.assertRaises(AuthenticationFailed):
                validate_credential(current, audience="https://api.example/mcp/", allowed_kinds=("access",))

    def test_task_credentials_are_bound_to_lease_generation_and_deadline(self):
        credential = authority()
        credential.kind = "task_tool"
        credential.task_id = uuid.uuid4()
        credential.lease_epoch = 3
        credential.task = NS(grant_id=credential.grant.pk, status="running", generation=2, lease_epoch=3,
            lease_expires_at=timezone.now() + timedelta(seconds=30), deadline_at=timezone.now() + timedelta(minutes=2),
            resource="slack:T123:C123")
        validate_credential(credential, audience=credential.audience, allowed_kinds=("task_tool",))
        credential.task.lease_epoch = 4
        with self.assertRaises(AuthenticationFailed):
            validate_credential(credential, audience=credential.audience, allowed_kinds=("task_tool",))

    @override_settings(AGENT_RUNTIME_ENABLED=True)
    def test_task_cannot_expand_to_another_channel_in_owner_grant(self):
        credential = authority()
        principal = AgentPrincipal(credential.grant, credential, NS(resource="slack:T123:C123"))
        with patch("agent_runtime.sources.live_access") as source, self.assertRaises(PermissionDenied):
            require_scope(principal, "messages:read", "slack:T123:C456")
        source.assert_not_called()

    @override_settings(AGENT_RUNTIME_ISSUER="https://api.example", COMMUNITY_CHAT_ALLOWED_ORIGINS=["https://chat.example"])
    def test_new_owner_ui_cookie_requires_configured_origin(self):
        request = APIRequestFactory().post("/api/v1/agent-runtime/connected/", {}, HTTP_ORIGIN="https://attacker.example")
        request.COOKIES["mlai_chat_access"] = "synthetic"
        session = NS(user=NS(is_active=True), origin="https://chat.example")
        with patch("community_chat.account_sessions.authenticate_access_token", return_value=session), self.assertRaises(AuthenticationFailed):
            OwnerAuthentication().authenticate(request)
        request = APIRequestFactory().post("/api/v1/agent-runtime/connected/", {}, HTTP_ORIGIN="https://api.example")
        request.COOKIES["mlai_chat_access"] = "synthetic"
        with patch("community_chat.account_sessions.authenticate_access_token", return_value=session):
            self.assertEqual(OwnerAuthentication().authenticate(request)[0], session.user)


@override_settings(AGENT_RUNTIME_RESOURCE="https://api.example/mcp/", AGENT_RUNTIME_ACTIONS_ENABLED=False,
    AGENT_RUNTIME_OAUTH_CLIENTS={"test-client": {"name": "Test agent", "redirect_uris": ["https://client.example/callback"],
        "scopes": ["messages:read", "events:read", "messages:write"]}})
class OAuthTests(SimpleTestCase):
    def params(self):
        verifier = "a" * 43
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        return {"client_id": "test-client", "redirect_uri": "https://client.example/callback", "scope": "messages:read events:read",
            "response_type": "code", "resource": "https://api.example/mcp/", "state": "random-client-state", "code_challenge_method": "S256", "code_challenge": challenge}

    def test_exact_client_redirect_audience_pkce_and_readonly_scopes(self):
        self.assertEqual(oauth.validate_authorization(self.params())["client_id"], "test-client")
        attacks = {"redirect_uri": "https://client.example/callback?next=evil", "resource": "https://other.example/mcp/",
            "code_challenge_method": "plain", "scope": "messages:write", "response_type": "token", "state": "", "client_id": "unknown"}
        for key, value in attacks.items():
            with self.subTest(key=key), self.assertRaises(ValidationError):
                oauth.validate_authorization({**self.params(), key: value})
        for invalid in ([], "text", {**self.params(), "redirect_uri": "https://[invalid"}):
            with self.assertRaises(ValidationError):
                oauth.validate_authorization(invalid)

    def test_refresh_replay_revokes_family_before_error(self):
        credential = authority()
        credential.kind = "refresh"
        credential.revoked_at = timezone.now()
        credential.replacement_id = uuid.uuid4()
        credential.refresh_family = uuid.uuid4()
        credential.oauth_binding = NS(client_id="test-client")
        manager = MagicMock()
        manager.all.return_value = manager
        manager.filter.return_value.values.return_value.first.return_value = {"grant__installation__owner_id": 7}
        manager.select_for_update.return_value.select_related.return_value.get.return_value = credential
        committed = []
        @contextmanager
        def atomic():
            yield
            committed.append(True)
        with patch.object(oauth.AgentCredential, "objects", manager), patch.object(oauth.transaction, "atomic", atomic), patch.object(oauth, "get_user_model"):
            with self.assertRaises(AuthenticationFailed):
                oauth.rotate_refresh({"client_id": "test-client", "refresh_token": "synthetic", "resource": credential.audience})
        self.assertEqual(committed, [True])
        manager.filter.assert_any_call(grant=credential.grant, refresh_family=credential.refresh_family, revoked_at__isnull=True)
        manager.filter.return_value.update.assert_called_once()


class SourceTests(SimpleTestCase):
    @override_settings(AGENT_RUNTIME_SLACK_READ_ENABLED=True, AGENT_RUNTIME_SLACK_WORKSPACES=["T1"],
                       AGENT_RUNTIME_SLACK_CHANNEL_IDS=["C1"])
    def test_source_requires_current_personal_user_token_and_exact_live_membership(self):
        from integrations.models import ExternalServiceConnection
        owner = NS(pk=7, is_active=True)
        connection = NS(provider_metadata={"authed_user": {"id": "U1"}, "team": {"id": "T1"}},
            external_account_id="T1", access_token="synthetic-only", scopes=["channels:read", "channels:history"], token_expires_at=None)
        manager = MagicMock()
        manager.select_for_update.return_value.filter.return_value.order_by.return_value.first.return_value = connection
        users = MagicMock()
        users.objects.select_for_update.return_value.get.return_value = owner
        with ExitStack() as stack:
            stack.enter_context(patch.object(ExternalServiceConnection, "objects", manager))
            stack.enter_context(patch.object(sources, "get_user_model", return_value=users))
            stack.enter_context(patch.object(sources.transaction, "atomic", return_value=nullcontext()))
            stack.enter_context(patch("integrations.services.slack_oauth_authority.current_slack_oauth_generation", return_value=2))
            generation = stack.enter_context(patch("integrations.services.slack_oauth_authority.connection_slack_oauth_generation", return_value=2))
            sdk = stack.enter_context(patch.object(sources, "WebClient"))
            provider = stack.enter_context(patch.object(sources, "_call"))
            public = {"id": "C1", "is_channel": True, "is_member": True}
            provider.side_effect = [{"team_id": "T1", "user_id": "U1"}, {"channel": public}]
            with sources.slack_authority(owner, "slack:T1:C1", expected_slack_user="U1"):
                pass
            self.assertTrue(manager.select_for_update.return_value.filter.call_args.kwargs["organization__isnull"])
            self.assertEqual(sdk.call_args.kwargs, {"token": "synthetic-only", "timeout": 5, "retry_handlers": []})
            for identity in ({"team_id": "T2", "user_id": "U1"}, {"team_id": "T1", "user_id": "U2"},
                             {"team_id": "T1", "user_id": "U1", "bot_id": "B1"}):
                provider.side_effect = [identity]
                with self.assertRaises(PermissionDenied), sources.slack_authority(owner, "slack:T1:C1"):
                    self.fail("A different or bot identity must not yield authority")
            provider.side_effect = [{"team_id": "T1", "user_id": "U1"}, {"channel": {**public, "is_private": True}}]
            with self.assertRaises(PermissionDenied), sources.slack_authority(owner, "slack:T1:C1"):
                self.fail("Private channel must not yield authority")
            sdk.reset_mock()
            generation.return_value = 1
            with self.assertRaises(PermissionDenied), sources.slack_authority(owner, "slack:T1:C1"):
                self.fail("Disconnected OAuth generation must not yield authority")
            sdk.assert_not_called()

    def test_private_shared_nonmember_and_archived_channels_fail_closed(self):
        public = {"is_channel": True, "is_member": True}
        self.assertTrue(sources.public_member_channel(public))
        for key in ("is_private", "is_im", "is_mpim", "is_ext_shared", "is_archived"):
            self.assertFalse(sources.public_member_channel({**public, key: True}))
        self.assertFalse(sources.public_member_channel({"is_channel": True}))
        self.assertFalse(sources.public_member_channel({**public, "is_member": False}))

    def test_only_exact_public_slack_resources_are_accepted(self):
        self.assertEqual(sources.parse_resource("slack:T123:C123"), ("T123", "C123"))
        for value in ("slack:T123:D123", "slack:T123:*", "community:relay:channel", "https://internal/path"):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                sources.parse_resource(value)

    def test_permanent_revocation_is_distinct_from_provider_outage(self):
        with patch.object(sources, "slack_authority", side_effect=PermissionDenied()):
            self.assertFalse(sources.live_access(NS(pk=1), "messages:read", "slack:T1:C1"))
        with patch.object(sources, "slack_authority", side_effect=sources.SourceUnavailable()), self.assertRaises(sources.SourceUnavailable):
            sources.live_access(NS(pk=1), "messages:read", "slack:T1:C1")

    def test_message_output_excludes_files_hidden_metadata_and_tokens(self):
        value = sources.safe_message({"ts": "123.456", "text": "hello", "files": ["private"], "token": "secret", "metadata": {"hidden": True}})
        self.assertEqual(set(value), {"id", "thread_id", "author_id", "text", "trust"})
        self.assertEqual(value["trust"], "untrusted_source_content")

    def test_approved_send_has_stable_attribution_metadata_and_receipt(self):
        action = NS(pk=uuid.uuid4(), kind="message.send", status="sending", resource="slack:T1:C1",
            payload={"text": "[Agent Roo, acting for Member]\n\nApproved text", "thread_id": "123.456"},
            task=NS(grant=NS(installation=NS(owner=NS(pk=1)), installation_id=uuid.uuid4())))
        @contextmanager
        def source(*args, **kwargs):
            yield NS(), "T1", "C1", {}
        with patch.object(sources, "slack_authority", source), patch.object(sources, "_call", return_value={"channel": "C1", "ts": "123.789"}) as call:
            receipt = sources.execute_approved_action(action, idempotency_key="stable")
        kwargs = call.call_args.kwargs
        self.assertEqual(kwargs["text"], action.payload["text"])
        self.assertEqual(kwargs["client_msg_id"], str(action.pk))
        self.assertEqual(kwargs["metadata"]["event_type"], "mlai_agent_action")
        self.assertEqual(receipt["external_id"], "123.789")


class InputTests(SimpleTestCase):
    def test_unknown_fields_and_excessive_limits_are_rejected(self):
        for data in ({"resource": "slack:T1:C1", "query": "hi", "owner_id": 2},
                     {"resource": "slack:T1:C1", "query": "hi", "limit": 101}):
            with self.assertRaises(ValidationError):
                validated(SearchSerializer, data)
        self.assertEqual(validated(EventsSerializer, {"resource": "slack:T1:C1"})["cursor"], "")

    def test_artifact_bytes_must_match_digest(self):
        content = b"synthetic artifact"
        artifact = {"name": "result.txt", "sha256": hashlib.sha256(content).hexdigest(), "content_base64": base64.b64encode(content).decode()}
        self.assertEqual(decode_artifact(artifact), content)
        with self.assertRaises(ValidationError):
            decode_artifact({**artifact, "sha256": "0" * 64})
        with self.assertRaises(ValidationError):
            decode_artifact({**artifact, "path": "/etc/passwd"})

    def test_members_cannot_claim_internal_idempotency_or_webhook_storage(self):
        for key in ("slack-command:slack:T1:Ev1", "schedule:1", "event:1"):
            with self.assertRaises(ValidationError):
                validated(TaskCreateSerializer, {"resource": "slack:T1:C1", "instruction": "hello", "idempotency_key": key})
        with self.assertRaises(ValidationError):
            validated(SubscriptionSerializer, {"resource": "slack:T1:C1", "instruction": "hello",
                "event_types": ["message.created"], "delivery": {"sealed": "attacker"}})

    def test_review_details_are_data_and_do_not_accept_a_resume_instruction(self):
        fields = {"lease_token": str(uuid.uuid4()), "lease_epoch": 1, "error_code": "approval_required",
                  "details": {"command": "example", "reason": "needs review"}}
        self.assertEqual(validated(FailSerializer, fields)["details"], fields["details"])
        with self.assertRaises(ValidationError):
            validated(FailSerializer, {**fields, "resume": True})


class AdmissionTests(SimpleTestCase):
    def data(self):
        return {"team_id": "T1", "user_id": "U1", "channel_id": "C1",
                "source_event_id": "slack:T1:Ev1", "instruction": "Summarise this channel"}

    def test_caller_cannot_choose_an_owner_or_grant(self):
        self.assertEqual(validated(SlackAdmissionSerializer, self.data()), self.data())
        for key in ("owner_id", "grant_id", "limits"):
            with self.assertRaises(ValidationError):
                validated(SlackAdmissionSerializer, {**self.data(), key: "caller choice"})
        for values in ([], [7, 8]):
            with self.assertRaises(PermissionDenied):
                admission.unique_identity(values, empty_code="absent", ambiguous_code="ambiguous")
        self.assertEqual(admission.unique_identity([7, 7], empty_code="absent", ambiguous_code="ambiguous"), 7)

    def test_workspace_event_mismatch_is_rejected_before_identity_lookup(self):
        with self.assertRaises(ValidationError):
            admission.admit_slack_command(**{**self.data(), "source_event_id": "slack:T2:Ev1"})

    def test_duplicate_command_uses_original_task_and_checks_current_slack_identity(self):
        from integrations.models import ExternalServiceConnection
        connections = MagicMock()
        connections.filter.return_value.values_list.return_value.distinct.return_value.__getitem__.return_value = [7]
        owner = NS(pk=7, is_active=True)
        users = MagicMock()
        users.objects.select_for_update.return_value.get.return_value = owner
        task = NS(pk=uuid.uuid4(), grant=NS(installation=NS(owner_id=7)), resource="slack:T1:C1",
                  instruction=self.data()["instruction"])
        tasks = MagicMock()
        tasks.filter.return_value.select_related.return_value.order_by.return_value.__getitem__.return_value = [task]
        with patch.object(ExternalServiceConnection, "objects", connections), patch.object(admission, "get_user_model", return_value=users), \
                patch.object(admission.transaction, "atomic", return_value=nullcontext()), \
                patch.object(admission.AgentTask, "objects", tasks), patch.object(admission, "slack_authority", return_value=nullcontext()) as authority_check, \
                patch.object(admission, "enqueue_task") as enqueue:
            self.assertEqual(admission.admit_slack_command(**self.data()), (task, False))
        authority_check.assert_called_once_with(owner, "slack:T1:C1", expected_slack_user="U1")
        enqueue.assert_not_called()

    def test_new_command_requires_one_resource_matching_managed_grant(self):
        from integrations.models import ExternalServiceConnection
        connections = MagicMock()
        connections.filter.return_value.values_list.return_value.distinct.return_value.__getitem__.return_value = [7]
        owner = NS(pk=7, is_active=True)
        users = MagicMock()
        users.objects.select_for_update.return_value.get.return_value = owner
        tasks = MagicMock()
        tasks.filter.return_value.select_related.return_value.order_by.return_value.__getitem__.return_value = []
        grants = MagicMock()
        candidate = NS(pk=uuid.uuid4(), resources=["slack:T1:C1"], scopes=["tasks:write", "messages:read"])
        grant_query = grants.filter.return_value.order_by.return_value.__getitem__
        with ExitStack() as stack:
            stack.enter_context(patch.object(ExternalServiceConnection, "objects", connections))
            stack.enter_context(patch.object(admission, "get_user_model", return_value=users))
            stack.enter_context(patch.object(admission.transaction, "atomic", return_value=nullcontext()))
            stack.enter_context(patch.object(admission.AgentTask, "objects", tasks))
            stack.enter_context(patch.object(admission.AgentGrant, "objects", grants))
            locked = stack.enter_context(patch.object(admission, "_grant", return_value=candidate))
            enqueue = stack.enter_context(patch.object(admission, "enqueue_task", return_value=NS(pk=uuid.uuid4())))
            for candidates in ([], [candidate, NS(pk=uuid.uuid4(), resources=candidate.resources, scopes=candidate.scopes)]):
                grant_query.return_value = candidates
                with self.assertRaises(PermissionDenied):
                    admission.admit_slack_command(**self.data())
            enqueue.assert_not_called()
            grant_query.return_value = [candidate]
            task, created = admission.admit_slack_command(**self.data())
            self.assertTrue(created)
            self.assertEqual(task, enqueue.return_value)
            self.assertEqual(locked.call_args.args, (candidate.pk,))
            self.assertEqual(enqueue.call_args.kwargs["idempotency_key"], "slack-command:slack:T1:Ev1")
            checker = enqueue.call_args.kwargs["access_checker"]
            self.assertFalse(checker(NS(pk=8), "messages:read", "slack:T1:C1"))
            with patch.object(admission, "slack_authority", return_value=nullcontext()) as access:
                self.assertTrue(checker(owner, "messages:read", "slack:T1:C1"))
            access.assert_called_once_with(owner, "slack:T1:C1", expected_slack_user="U1")

    @override_settings(AGENT_RUNTIME_ENABLED=True, AGENT_RUNTIME_ADMISSION_KEY="a" * 40,
                       AGENT_RUNTIME_WORKER_KEY="w" * 40, AGENT_RUNTIME_ISSUER="https://api.example")
    def test_admission_has_a_separate_key_and_private_idempotent_receipt(self):
        factory = APIRequestFactory()
        with patch.object(SlackAdmissionView, "throttle_classes", ()), patch.object(admission, "admit_slack_command") as admit:
            wrong = factory.post("/admit/", self.data(), format="json", HTTP_AUTHORIZATION="Bearer " + "w" * 40)
            response = SlackAdmissionView.as_view()(wrong)
            self.assertIn(response.status_code, (401, 403))
            admit.assert_not_called()
            admit.return_value = (NS(pk=uuid.uuid4(), status="queued"), False)
            correct = factory.post("/admit/", self.data(), format="json", HTTP_AUTHORIZATION="Bearer " + "a" * 40)
            response = SlackAdmissionView.as_view()(correct)
        self.assertEqual(response.status_code, 202)
        self.assertFalse(response.data["new"])
        self.assertEqual(set(response.data), {"task_id", "status", "new", "owner_url"})
        self.assertEqual(response["Cache-Control"], "no-store")


class OwnerUITests(SimpleTestCase):
    def test_connected_page_escapes_agent_names_and_uses_text_nodes_for_results(self):
        malicious = "<script>alert('untrusted')</script>"
        grant = NS(pk=uuid.uuid4(), installation=NS(name=malicious, runtime="hermes"),
            expires_at=timezone.now(), revoked_at=None, scopes=["messages:read"], resources=["slack:T1:C1"])
        html = render_to_string("agent_runtime/connected.html", {"agents": [grant], "active_installations": [str(uuid.uuid4())]})
        self.assertNotIn(malicious, html)
        self.assertIn("&lt;script&gt;", html)
        self.assertNotIn("innerHTML", html)
        self.assertIn("node.textContent=text", html)
        for label in ("Watch a channel", "Stop all my MLAI agent work", "Cancel task", "Download", "Review message draft"):
            self.assertIn(label, html)

    @override_settings(AGENT_RUNTIME_ENABLED=True)
    def test_cancel_does_not_reveal_results_after_source_access_is_removed(self):
        owner = NS(pk=7, is_active=True, is_authenticated=True)
        task = NS(pk=uuid.uuid4(), status="cancelled", result={"text": "previous private result"})
        request = APIRequestFactory().post("/cancel/", {}, format="json")
        force_authenticate(request, user=owner)
        with patch.object(CancelTaskView, "throttle_classes", ()), \
                patch("agent_runtime.views.get_object_or_404"), patch("agent_runtime.views.runtime.cancel_task", return_value=task):
            response = CancelTaskView.as_view()(request, task_id=task.pk)
        self.assertEqual(response.data, {"task_id": str(task.pk), "status": "cancelled"})
        self.assertEqual(response["Cache-Control"], "no-store")


@override_settings(AGENT_RUNTIME_ENABLED=True, AGENT_RUNTIME_MCP_EVENTS_ENABLED=False)
class ProtocolTests(SimpleTestCase):
    def rpc(self, payload, **headers):
        request = APIRequestFactory().post("/api/v1/agent-runtime/mcp/", payload, format="json", **headers)
        force_authenticate(request, user=NS(is_authenticated=True, is_active=True))
        with patch.object(MCPView, "throttle_classes", ()):
            return MCPView.as_view()(request)

    def test_legacy_initialization_and_modern_discovery_coexist(self):
        legacy = self.rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25"}})
        self.assertEqual(legacy.data["result"]["protocolVersion"], "2025-11-25")
        modern = self.rpc({"jsonrpc": "2.0", "id": 1, "method": "server/discover"})
        self.assertEqual(modern.data["result"]["resultType"], "complete")
        self.assertNotIn("events", modern.data["result"]["capabilities"])

    def test_modern_catalog_is_private_and_has_complete_framing(self):
        response = self.rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {"_meta": {"io.modelcontextprotocol/protocolVersion": "2026-07-28"}}})
        self.assertEqual(response.data["result"]["cacheScope"], "private")
        self.assertEqual(response.data["result"]["ttlMs"], 0)
        self.assertEqual(response.data["result"]["resultType"], "complete")

    @override_settings(AGENT_RUNTIME_ENABLED=False)
    def test_disabled_rollout_stops_before_database_and_authentication(self):
        request = APIRequestFactory().get("/api/v1/agent-runtime/connected/")
        response = ConnectedAgentsView.as_view()(request)
        self.assertEqual(response.status_code, 404)

    @override_settings(AGENT_RUNTIME_EVENT_KEY="e" * 40, AGENT_RUNTIME_SLACK_WORKSPACES=["T1"], AGENT_RUNTIME_SLACK_CHANNEL_IDS=["C1"])
    def test_ingress_rejects_message_bodies_even_for_event_producer(self):
        payload = {"source": "slack", "source_event_id": "slack:T1:Ev1", "resource": "slack:T1:C1",
            "event_type": "message.created", "occurred_at": timezone.now().isoformat(), "payload": {"text": "do not store"}}
        request = APIRequestFactory().post("/api/v1/agent-runtime/internal/events/", payload, format="json", HTTP_AUTHORIZATION="Bearer " + "e" * 40)
        with patch.object(IngestEventView, "throttle_classes", ()):
            response = IngestEventView.as_view()(request)
        self.assertEqual(response.status_code, 400)
