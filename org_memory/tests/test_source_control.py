import hashlib
import os
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from integrations.models import ExternalServiceConnection, GoogleConnection
from organizations.models import Organization
from org_memory.assertions import actor_identity_headers, build_actor_assertion
from org_memory.connectors.base import ScopeDescriptor, ScopePage
from org_memory.connectors.registry import connector_registry
from org_memory.control_plane import SourceControlError, validate_action_for_execution
from org_memory.governance import SUPPORTED_PROVIDERS
from org_memory.kernel import capture_source_version
from org_memory.models import (
    MemoryActionType,
    MemoryConnectionState,
    MemoryProvider,
    MemoryProviderEnablement,
    MemorySource,
    MemorySourceLifecycle,
    MemorySourceActionRequest,
    MemorySourceAuditEvent,
    MemorySourcePreview,
    MemorySourceScope,
    OrganizationCapability,
    OrganizationCapabilityGrant,
    OrganizationIdentity,
    OrganizationMembership,
    OrganizationRole,
    OrganizationRoleAssignment,
    OrganizationSlackWorkspace,
    ServicePrincipal,
)
from org_memory.service_principals import issue_service_principal_credential
from startup_updates.models import LinearProjectSelection, UserStartupBinding


class SourceControlPlaneTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.organization = Organization.objects.create(name="MLAI", domain="mlai.test")
        self.other_organization = Organization.objects.create(name="Other", domain="other.test")
        self.user = get_user_model().objects.create_user(email="operator@mlai.test")
        self.workspace = OrganizationSlackWorkspace.objects.create(
            organization=self.organization,
            slack_team_id="TMLAI123",
        )
        OrganizationIdentity.objects.create(
            organization=self.organization,
            user=self.user,
            provider="slack",
            external_tenant_id="TMLAI123",
            external_user_id="UOPERATOR1",
            email_at_link_time=self.user.email,
            verified_at=timezone.now(),
        )
        membership = OrganizationMembership.objects.create(
            organization=self.organization,
            user=self.user,
        )
        role = OrganizationRole.objects.create(
            organization=self.organization,
            slug="source-manager",
            name="Source manager",
        )
        OrganizationRoleAssignment.objects.create(membership=membership, role=role)
        OrganizationCapabilityGrant.objects.create(
            role=role,
            capability=OrganizationCapability.objects.get(key="manage_sources"),
        )
        self.principal = ServicePrincipal.objects.create(
            name="source-control-test",
            organization=self.organization,
            scopes=["source.manage"],
            allowed_surfaces=["admin_roo"],
        )
        self.credential, self.token = issue_service_principal_credential(self.principal)
        self.connection = ExternalServiceConnection.objects.create(
            provider="linear",
            user=self.user,
            organization=self.organization,
            external_account_id="linear-workspace-1",
            account_label="MLAI Linear",
        )
        MemoryProviderEnablement.objects.create(
            organization=self.organization,
            provider="linear",
            is_enabled=True,
            approved_by=self.user,
            approved_at=timezone.now(),
        )
        self.request_number = 0

    def _headers(self, *, idempotency_key=None):
        self.request_number += 1
        request_id = f"source-control-{self.request_number}"
        assertion = build_actor_assertion(
            self.token,
            credential_id=str(self.credential.pk),
            surface="admin_roo",
            slack_team_id="TMLAI123",
            acting_slack_user_id="UOPERATOR1",
            slack_channel_id="GADMIN123",
            slack_thread_ts="1700000000.123",
            event_id=f"EvSOURCE{self.request_number}",
            request_id=request_id,
        )
        identity = actor_identity_headers(
            assertion=assertion,
            surface="admin_roo",
            slack_team_id="TMLAI123",
            acting_slack_user_id="UOPERATOR1",
            slack_channel_id="GADMIN123",
            slack_thread_ts="1700000000.123",
            event_id=f"EvSOURCE{self.request_number}",
            request_id=request_id,
        )
        headers = {
            "HTTP_AUTHORIZATION": f"ServicePrincipal {self.token}",
            **{
                f"HTTP_{key.upper().replace('-', '_')}": value
                for key, value in identity.items()
            },
        }
        if idempotency_key:
            headers["HTTP_IDEMPOTENCY_KEY"] = idempotency_key
        return headers

    def _connect(self, provider="linear", connection=None):
        connection = connection or self.connection
        response = self.client.post(
            f"/api/v1/org-memory/connectors/{provider}/connect",
            {"external_connection_id": connection.pk},
            format="json",
            **self._headers(),
        )
        self.assertIn(response.status_code, (200, 201), response.data)
        return response.data["id"]

    def _select_scope(self, configuration_id, external_id="project-1"):
        return self.client.put(
            f"/api/v1/org-memory/connections/{configuration_id}/scopes",
            {
                "scopes": [
                    {
                        "scope_type": "project",
                        "external_id": external_id,
                        "name": "Project One",
                        "classification": "committee",
                        "selected": True,
                    }
                ]
            },
            format="json",
            **self._headers(),
        )

    def _preview_dry_run_approve(self, configuration_id):
        self.assertEqual(self._select_scope(configuration_id).status_code, 200)
        preview = self.client.post(
            f"/api/v1/org-memory/connections/{configuration_id}/preview",
            {},
            format="json",
            **self._headers(),
        )
        self.assertEqual(preview.status_code, 201, preview.data)
        dry_run = self.client.post(
            f"/api/v1/org-memory/connections/{configuration_id}/dry-run",
            {},
            format="json",
            **self._headers(),
        )
        self.assertEqual(dry_run.status_code, 200, dry_run.data)
        approval = self.client.post(
            f"/api/v1/org-memory/connections/{configuration_id}/approve",
            {"confirm": True},
            format="json",
            **self._headers(),
        )
        self.assertEqual(approval.status_code, 200, approval.data)

    def test_registry_covers_every_governed_provider_and_conforms(self):
        self.assertEqual(set(MemoryProvider.values), SUPPORTED_PROVIDERS)
        self.assertEqual(set(connector_registry.providers()), SUPPORTED_PROVIDERS)
        for provider in connector_registry.providers():
            self.assertEqual(connector_registry.validate_conformance(provider), [])

    def test_full_flow_requires_preview_dry_run_and_approval_before_backfill(self):
        LinearProjectSelection.objects.create(
            connection=self.connection,
            user=self.user,
            organization=self.organization,
            linear_project_id="project-1",
            project_name="Project One",
            selected=False,
        )
        configuration_id = self._connect()
        premature = self.client.post(
            f"/api/v1/org-memory/connections/{configuration_id}/backfill",
            {"confirm": True},
            format="json",
            **self._headers(),
        )
        self.assertEqual(premature.status_code, 409)
        self.assertEqual(premature.data["code"], "backfill_not_approved")
        self.assertFalse(
            MemorySourceActionRequest.objects.filter(action=MemoryActionType.BACKFILL).exists()
        )

        discovered = self.client.post(
            f"/api/v1/org-memory/connections/{configuration_id}/discover",
            {},
            format="json",
            **self._headers(),
        )
        self.assertEqual(discovered.status_code, 200, discovered.data)
        self.assertEqual(discovered.data["scopes"][0]["external_id"], "project-1")
        self.assertEqual(self._select_scope(configuration_id).status_code, 200)

        preview = self.client.post(
            f"/api/v1/org-memory/connections/{configuration_id}/preview",
            {},
            format="json",
            **self._headers(),
        )
        self.assertEqual(preview.status_code, 201, preview.data)
        self.assertFalse(preview.data["summary"]["content_activated"])
        approval_before_dry_run = self.client.post(
            f"/api/v1/org-memory/connections/{configuration_id}/approve",
            {"confirm": True},
            format="json",
            **self._headers(),
        )
        self.assertEqual(approval_before_dry_run.status_code, 400)

        dry_run = self.client.post(
            f"/api/v1/org-memory/connections/{configuration_id}/dry-run",
            {},
            format="json",
            **self._headers(),
        )
        self.assertEqual(dry_run.status_code, 200, dry_run.data)
        self.assertFalse(dry_run.data["dry_run_summary"]["active_memory_created"])
        approved = self.client.post(
            f"/api/v1/org-memory/connections/{configuration_id}/approve",
            {"confirm": True},
            format="json",
            **self._headers(),
        )
        self.assertEqual(approved.status_code, 200, approved.data)
        self.assertEqual(approved.data["lifecycle_state"], "approved")

        disabled = self.client.post(
            f"/api/v1/org-memory/connections/{configuration_id}/backfill",
            {"confirm": True},
            format="json",
            **self._headers(idempotency_key="linear-backfill-1"),
        )
        self.assertEqual(disabled.status_code, 409)
        self.assertEqual(disabled.data["code"], "provider_disabled")

        with patch.dict(os.environ, {"ORG_MEMORY_ENABLED_PROVIDERS": "linear"}):
            queued = self.client.post(
                f"/api/v1/org-memory/connections/{configuration_id}/backfill",
                {"confirm": True},
                format="json",
                **self._headers(idempotency_key="linear-backfill-1"),
            )
            replay = self.client.post(
                f"/api/v1/org-memory/connections/{configuration_id}/backfill",
                {"confirm": True},
                format="json",
                **self._headers(idempotency_key="linear-backfill-1"),
            )

        self.assertEqual(queued.status_code, 202, queued.data)
        self.assertTrue(queued.data["created"])
        self.assertEqual(replay.status_code, 202, replay.data)
        self.assertFalse(replay.data["created"])
        self.assertEqual(queued.data["id"], replay.data["id"])
        self.assertEqual(
            MemorySourceActionRequest.objects.filter(action="backfill").count(),
            1,
        )
        self.assertEqual(
            MemorySourceAuditEvent.objects.filter(event_type="backfill_requested").count(),
            1,
        )

    def test_scope_change_invalidates_approval_and_pending_worker_action(self):
        configuration_id = self._connect()
        self._preview_dry_run_approve(configuration_id)
        with patch.dict(os.environ, {"ORG_MEMORY_ENABLED_PROVIDERS": "linear"}):
            queued = self.client.post(
                f"/api/v1/org-memory/connections/{configuration_id}/backfill",
                {"confirm": True},
                format="json",
                **self._headers(idempotency_key="stale-backfill"),
            )
        self.assertEqual(queued.status_code, 202, queued.data)
        action = MemorySourceActionRequest.objects.get(pk=queued.data["id"])

        changed = self._select_scope(configuration_id, external_id="project-2")

        self.assertEqual(changed.status_code, 200, changed.data)
        configuration = action.configuration
        configuration.refresh_from_db()
        self.assertEqual(configuration.lifecycle_state, MemoryConnectionState.SCOPED)
        self.assertIsNone(configuration.approved_preview_id)
        self.assertFalse(MemorySourcePreview.objects.get().is_current)
        with patch.dict(os.environ, {"ORG_MEMORY_ENABLED_PROVIDERS": "linear"}):
            with self.assertRaises(SourceControlError):
                validate_action_for_execution(action)

    def test_active_runtime_actions_pause_resume_health_and_delete_are_audited(self):
        configuration_id = self._connect()
        self._preview_dry_run_approve(configuration_id)
        configuration = self.connection.memory_configuration
        configuration.lifecycle_state = MemoryConnectionState.ACTIVE
        configuration.save(update_fields=("lifecycle_state",))
        scope = configuration.source_scopes.get(external_id="project-1")
        evidence_text = "Linear project one is active."
        capture_source_version(
            organization=self.organization,
            provider="linear",
            external_account_id="linear-workspace-1",
            source_type="project",
            external_id="project-1",
            version_key="updated:1",
            content_hash=hashlib.sha256(evidence_text.encode("utf-8")).hexdigest(),
            classification="committee",
            acl={"is_accessible": True, "principal_refs": ["user:operator"]},
            chunks=[{"ordinal": 0, "text": evidence_text}],
            configuration=configuration,
            source_scope=scope,
        )

        with patch.dict(os.environ, {"ORG_MEMORY_ENABLED_PROVIDERS": "linear"}):
            sync = self.client.post(
                f"/api/v1/org-memory/connections/{configuration_id}/sync",
                {},
                format="json",
                **self._headers(idempotency_key="sync-1"),
            )
            reprocess = self.client.post(
                f"/api/v1/org-memory/connections/{configuration_id}/reprocess",
                {"scope_external_ids": ["project-1"]},
                format="json",
                **self._headers(idempotency_key="reprocess-1"),
            )
            paused = self.client.post(
                f"/api/v1/org-memory/connections/{configuration_id}/pause",
                {},
                format="json",
                **self._headers(),
            )
            resumed = self.client.post(
                f"/api/v1/org-memory/connections/{configuration_id}/resume",
                {},
                format="json",
                **self._headers(),
            )
        self.assertEqual(sync.status_code, 202, sync.data)
        self.assertEqual(reprocess.status_code, 202, reprocess.data)
        self.assertEqual(paused.data["lifecycle_state"], "paused")
        self.assertEqual(resumed.data["lifecycle_state"], "active")

        health = self.client.get(
            f"/api/v1/org-memory/connections/{configuration_id}/health",
            **self._headers(),
        )
        self.assertEqual(health.status_code, 200, health.data)
        self.assertEqual(health.data["pending_actions"]["sync"], 1)
        kernel_health = self.client.get(
            "/api/v1/org-memory/health",
            **self._headers(),
        )
        self.assertEqual(kernel_health.status_code, 200, kernel_health.data)
        self.assertEqual(kernel_health.data["counts"]["active_chunks"], 1)
        deleted = self.client.delete(
            f"/api/v1/org-memory/connections/{configuration_id}",
            {"confirm": True},
            format="json",
            **self._headers(idempotency_key="delete-1"),
        )
        self.assertEqual(deleted.status_code, 202, deleted.data)
        configuration.refresh_from_db()
        self.assertEqual(configuration.lifecycle_state, MemoryConnectionState.DELETE_PENDING)
        self.assertFalse(MemorySourceScope.objects.filter(selected=True).exists())
        source = MemorySource.objects.get(external_id="project-1")
        self.assertEqual(source.lifecycle_state, MemorySourceLifecycle.TOMBSTONED)
        self.assertFalse(source.versions.filter(chunks__active_for_retrieval=True).exists())
        self.assertTrue(
            MemorySourceAuditEvent.objects.filter(
                event_type="connection_delete_requested"
            ).exists()
        )

    def test_cross_organization_connections_and_missing_capability_are_denied(self):
        other_user = get_user_model().objects.create_user(email="other@example.test")
        other_connection = ExternalServiceConnection.objects.create(
            provider="linear",
            user=other_user,
            organization=self.other_organization,
            external_account_id="other-linear",
        )
        cross_org = self.client.post(
            "/api/v1/org-memory/connectors/linear/connect",
            {"external_connection_id": other_connection.pk},
            format="json",
            **self._headers(),
        )
        self.assertEqual(cross_org.status_code, 400)
        self.assertEqual(cross_org.data["code"], "connection_not_found")

        OrganizationCapabilityGrant.objects.filter(
            capability__key="manage_sources"
        ).delete()
        denied = self.client.get(
            "/api/v1/org-memory/connectors",
            **self._headers(),
        )
        self.assertEqual(denied.status_code, 403)

    def test_slack_direct_messages_and_unsafe_discovery_metadata_fail_closed(self):
        slack_connection = ExternalServiceConnection.objects.create(
            provider="slack",
            user=self.user,
            organization=self.organization,
            external_account_id="TMLAI123",
        )
        slack_configuration_id = self._connect("slack", slack_connection)
        direct_message = self.client.put(
            f"/api/v1/org-memory/connections/{slack_configuration_id}/scopes",
            {"scopes": [{"scope_type": "channel", "external_id": "DSECRET1"}]},
            format="json",
            **self._headers(),
        )
        self.assertEqual(direct_message.status_code, 400)
        self.assertIn("direct messages", direct_message.data["detail"])

        original = connector_registry.get("linear")

        class UnsafeConnector:
            provider = "linear"

            def __getattr__(self, name):
                return getattr(original, name)

            def discover_scopes(self, configuration, cursor=None):
                return ScopePage(
                    scopes=(
                        ScopeDescriptor(
                            scope_type="project",
                            external_id="unsafe",
                            name="Unsafe",
                            metadata={"access_token": "must-never-persist"},
                        ),
                    )
                )

        connector_registry.register(UnsafeConnector(), replace=True)
        try:
            configuration_id = self._connect()
            response = self.client.post(
                f"/api/v1/org-memory/connections/{configuration_id}/discover",
                {},
                format="json",
                **self._headers(),
            )
        finally:
            connector_registry.register(original, replace=True)
        self.assertEqual(response.status_code, 400)
        self.assertFalse(MemorySourceScope.objects.filter(external_id="unsafe").exists())

    def test_gmail_uses_an_explicit_organization_binding(self):
        google_connection = GoogleConnection.objects.create(
            user=self.user,
            google_email="shared@mlai.test",
            refresh_token="",
            scope="gmail.readonly",
        )
        unbound = self.client.post(
            "/api/v1/org-memory/connectors/gmail/connect",
            {"google_connection_id": google_connection.pk},
            format="json",
            **self._headers(),
        )
        self.assertEqual(unbound.status_code, 400)
        UserStartupBinding.objects.create(
            user=self.user,
            organization=self.organization,
            google_connection=google_connection,
        )
        bound = self.client.post(
            "/api/v1/org-memory/connectors/gmail/connect",
            {"google_connection_id": google_connection.pk},
            format="json",
            **self._headers(),
        )
        self.assertEqual(bound.status_code, 201, bound.data)
        self.assertEqual(bound.data["connection_type"], "gmail")
