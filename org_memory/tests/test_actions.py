import hashlib
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from integrations.models import ExternalServiceConnection
from organizations.models import Organization
from org_memory.action_adapters import (
    ActionAdapterError,
    ActionExecutionResult,
    action_adapter_registry,
)
from org_memory.assertions import actor_identity_headers, build_actor_assertion
from org_memory.kernel import capture_source_version, revoke_source_access
from org_memory.models import (
    AgentActionEvent,
    AgentActionProposal,
    AgentActionRiskLevel,
    AgentActionStatus,
    AgentActionType,
    MemoryClaim,
    MemoryClaimKind,
    MemoryClaimStatus,
    MemoryConnectionConfiguration,
    MemoryConnectionState,
    MemoryEvidence,
    MemoryExtractionRun,
    MemoryExtractionStatus,
    MemoryProviderEnablement,
    MemoryScopeStatus,
    MemorySourceActionRequest,
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


def digest(value):
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


class FakeLinearActionAdapter:
    target_system = "linear"

    def __init__(self, action_type):
        self.action_type = action_type
        self.revision = 1
        self.executions = 0
        self.reversals = 0
        self.fail_execution = False

    def validate_payload(self, payload):
        if not isinstance(payload, dict):
            raise ActionAdapterError("input_payload must be an object.")
        project_id = str(payload.get("project_id") or "").strip()
        if self.action_type == AgentActionType.CREATE_LINEAR_ISSUE:
            title = str(payload.get("title") or "").strip()
            team_id = str(payload.get("team_id") or "").strip()
            if not title or not team_id:
                raise ActionAdapterError("title and team_id are required.")
            return {
                "title": title,
                "team_id": team_id,
                "project_id": project_id,
            }
        issue_id = str(payload.get("issue_id") or "").strip()
        if not issue_id:
            raise ActionAdapterError("issue_id is required.")
        return {
            "issue_id": issue_id,
            "title": str(payload.get("title") or "").strip(),
            "project_id": project_id,
        }

    def refresh_preconditions(self, proposal):
        return {
            "target_system": "linear",
            "action_type": self.action_type,
            "target_revision": self.revision,
        }

    def execute(self, proposal):
        self.executions += 1
        if self.fail_execution:
            raise ActionAdapterError("controlled fixture failure")
        external_id = (
            "linear_issue:ISSUE-NEW"
            if self.action_type == AgentActionType.CREATE_LINEAR_ISSUE
            else f"linear_issue:{proposal.input_payload['issue_id']}"
        )
        return ActionExecutionResult(
            result={
                "target_system": "linear",
                "id": external_id.split(":", 1)[1],
                "title": proposal.input_payload.get("title") or "Existing issue",
            },
            reversal_payload={
                "operation": "archive_issue",
                "issue_id": external_id.split(":", 1)[1],
            },
            external_id=external_id,
        )

    def reverse(self, proposal):
        self.reversals += 1
        return {
            "target_system": "linear",
            "issue_id": proposal.reversal_payload["issue_id"],
            "archived": True,
        }

    def validate_reversal(self, proposal):
        if self.revision != proposal.precondition_snapshot["target_revision"]:
            raise ActionAdapterError("The Linear issue changed after execution.")


@override_settings(
    ORG_MEMORY_ACTIONS_ENABLED=True,
    ORG_MEMORY_ACTION_LINEAR_EXECUTION_ENABLED=True,
    ORG_MEMORY_ACTION_REQUIRE_SEPARATE_APPROVER=True,
)
class ControlledActionGatewayTests(TestCase):
    def setUp(self):
        self.enabled_provider_environment = patch.dict(
            "os.environ",
            {"ORG_MEMORY_ENABLED_PROVIDERS": "linear"},
        )
        self.enabled_provider_environment.start()
        self.client = APIClient()
        self.organization = Organization.objects.create(
            name="Action Brain",
            domain="action-brain.test",
        )
        self.proposer = get_user_model().objects.create_user(
            email="proposer@action-brain.test",
        )
        self.reviewer = get_user_model().objects.create_user(
            email="reviewer@action-brain.test",
        )
        OrganizationSlackWorkspace.objects.create(
            organization=self.organization,
            slack_team_id="TACTION1",
        )
        role = OrganizationRole.objects.create(
            organization=self.organization,
            slug="action-operator",
            name="Action operator",
        )
        for capability in ("view_general_memory", "approve_actions"):
            OrganizationCapabilityGrant.objects.create(
                role=role,
                capability=OrganizationCapability.objects.get(key=capability),
            )
        for user, slack_user_id in (
            (self.proposer, "UPROPOSE1"),
            (self.reviewer, "UAPPROVE1"),
        ):
            OrganizationIdentity.objects.create(
                organization=self.organization,
                user=user,
                provider="slack",
                external_tenant_id="TACTION1",
                external_user_id=slack_user_id,
                email_at_link_time=user.email,
                verified_at=timezone.now(),
            )
            membership = OrganizationMembership.objects.create(
                organization=self.organization,
                user=user,
            )
            OrganizationRoleAssignment.objects.create(
                membership=membership,
                role=role,
            )
        self.admin_principal = ServicePrincipal.objects.create(
            name="admin-action-test",
            organization=self.organization,
            scopes=["org_memory.actions"],
            allowed_surfaces=["admin_roo"],
        )
        self.admin_credential, self.admin_token = (
            issue_service_principal_credential(self.admin_principal)
        )
        self.public_principal = ServicePrincipal.objects.create(
            name="public-action-test",
            organization=self.organization,
            scopes=["org_memory.actions"],
            allowed_surfaces=["public_roo"],
        )
        self.public_credential, self.public_token = (
            issue_service_principal_credential(self.public_principal)
        )
        self.connection = ExternalServiceConnection.objects.create(
            provider="linear",
            user=self.proposer,
            organization=self.organization,
            access_token="encrypted-fixture-token",
            external_account_id="action-linear",
            status="connected",
        )
        self.configuration = MemoryConnectionConfiguration.objects.create(
            organization=self.organization,
            provider="linear",
            external_connection=self.connection,
            lifecycle_state=MemoryConnectionState.ACTIVE,
            created_by=self.proposer,
        )
        MemoryProviderEnablement.objects.create(
            organization=self.organization,
            provider="linear",
            is_enabled=True,
            approved_by=self.proposer,
            approved_at=timezone.now(),
        )
        self.scope = MemorySourceScope.objects.create(
            configuration=self.configuration,
            scope_type="project",
            external_id="PROJECT-1",
            name="Approved Project",
            selected=True,
            status=MemoryScopeStatus.SELECTED,
            default_classification="committee",
        )
        self.original_adapters = {
            action_type: action_adapter_registry.get(action_type)
            for action_type in (
                AgentActionType.CREATE_LINEAR_ISSUE,
                AgentActionType.UPDATE_LINEAR_ISSUE,
            )
        }
        self.fake_create = FakeLinearActionAdapter(
            AgentActionType.CREATE_LINEAR_ISSUE
        )
        self.fake_update = FakeLinearActionAdapter(
            AgentActionType.UPDATE_LINEAR_ISSUE
        )
        action_adapter_registry.register(self.fake_create, replace=True)
        action_adapter_registry.register(self.fake_update, replace=True)
        self.request_number = 0

    def tearDown(self):
        for adapter in self.original_adapters.values():
            action_adapter_registry.register(adapter, replace=True)
        self.enabled_provider_environment.stop()
        super().tearDown()

    def _headers(
        self,
        *,
        reviewer=False,
        idempotency_key=None,
        public_surface=False,
    ):
        self.request_number += 1
        slack_user_id = "UAPPROVE1" if reviewer else "UPROPOSE1"
        request_id = f"action-{self.request_number}"
        event_id = f"EvACTION{self.request_number}"
        surface = "public_roo" if public_surface else "admin_roo"
        token = self.public_token if public_surface else self.admin_token
        credential = (
            self.public_credential if public_surface else self.admin_credential
        )
        assertion = build_actor_assertion(
            token,
            credential_id=str(credential.pk),
            surface=surface,
            slack_team_id="TACTION1",
            acting_slack_user_id=slack_user_id,
            slack_channel_id="CACTION1",
            slack_thread_ts="1700000000.123",
            event_id=event_id,
            request_id=request_id,
        )
        identity = actor_identity_headers(
            assertion=assertion,
            surface=surface,
            slack_team_id="TACTION1",
            acting_slack_user_id=slack_user_id,
            slack_channel_id="CACTION1",
            slack_thread_ts="1700000000.123",
            event_id=event_id,
            request_id=request_id,
        )
        headers = {
            "HTTP_AUTHORIZATION": f"ServicePrincipal {token}",
            **{
                f"HTTP_{key.upper().replace('-', '_')}": value
                for key, value in identity.items()
            },
        }
        if idempotency_key:
            headers["HTTP_IDEMPOTENCY_KEY"] = idempotency_key
        return headers

    def _create_linear_action(
        self,
        *,
        key="create-linear-action",
        evidence_claim_ids=None,
    ):
        return self.client.post(
            "/api/v1/org-memory/actions",
            {
                "action_type": AgentActionType.CREATE_LINEAR_ISSUE,
                "configuration_id": str(self.configuration.pk),
                "input_payload": {
                    "title": "Follow up with the venue",
                    "team_id": "TEAM-1",
                    "project_id": "PROJECT-1",
                },
                "evidence_claim_ids": evidence_claim_ids or [],
            },
            format="json",
            **self._headers(idempotency_key=key),
        )

    def _approve(self, proposal_id, *, key="approve-linear-action"):
        return self.client.post(
            f"/api/v1/org-memory/actions/{proposal_id}/approve",
            {},
            format="json",
            **self._headers(reviewer=True, idempotency_key=key),
        )

    def _claim(self):
        statement = "The venue follow-up is due this week."
        source, version, _ = capture_source_version(
            organization=self.organization,
            provider="linear",
            external_account_id="action-linear",
            source_type="linear_issue",
            external_id="venue-follow-up",
            version_key="v1",
            content_hash=digest(statement),
            classification="committee",
            acl={
                "is_accessible": True,
                "provider_revision": "acl-v1",
                "principal_refs": ["group:committee"],
            },
            chunks=[{"ordinal": 0, "text": statement}],
            configuration=self.configuration,
            source_scope=self.scope,
            title="Venue follow-up",
        )
        run = MemoryExtractionRun.objects.create(
            organization=self.organization,
            source_version=version,
            idempotency_key=digest("action-extraction"),
            status=MemoryExtractionStatus.EXTRACTED,
            extractor_version="test-v1",
            schema_version="test-v1",
            prompt_version="test-v1",
            model="deterministic-test",
            prompt_input_hash=digest(statement),
        )
        claim = MemoryClaim.objects.create(
            organization=self.organization,
            extraction_run=run,
            candidate_key=digest("action-candidate"),
            kind=MemoryClaimKind.TASK,
            epistemic_type="observation",
            predicate="venue_follow_up",
            object_value=statement,
            statement=statement,
            normalized_key=digest("action-normalized"),
            status=MemoryClaimStatus.ACTIVE,
            classification="committee",
            confidence=Decimal("0.950"),
            importance=Decimal("0.800"),
            source_authority=Decimal("0.800"),
            volatility="normal",
            review_required=False,
            reviewed_by=self.proposer,
            reviewed_at=timezone.now(),
            extractor_version="test-v1",
            extractor_model="deterministic-test",
            extractor_prompt_version="test-v1",
            extractor_schema_version="test-v1",
        )
        MemoryEvidence.objects.create(
            claim=claim,
            source=source,
            source_version=version,
            chunk=version.chunks.get(ordinal=0),
            quote=statement,
            quote_hash=digest(statement),
            source_locator={"issue_id": "venue-follow-up"},
        )
        return claim, source

    def test_draft_action_needs_no_approval_and_executes_locally_idempotently(self):
        secret_body = "Draft body that must not enter audit metadata."
        created = self.client.post(
            "/api/v1/org-memory/actions",
            {
                "action_type": AgentActionType.DRAFT_GMAIL,
                "input_payload": {
                    "to": ["member@example.com"],
                    "subject": "Meeting follow-up",
                    "body": secret_body,
                },
            },
            format="json",
            **self._headers(idempotency_key="draft-gmail-action"),
        )
        self.assertEqual(created.status_code, 201, created.data)
        self.assertFalse(created.data["requires_approval"])
        self.assertEqual(created.data["risk_level"], AgentActionRiskLevel.LOW)
        self.assertEqual(created.data["status"], AgentActionStatus.PROPOSED)

        proposal_id = created.data["id"]
        executed = self.client.post(
            f"/api/v1/org-memory/actions/{proposal_id}/execute",
            {},
            format="json",
            **self._headers(idempotency_key="execute-draft-gmail"),
        )
        self.assertEqual(executed.status_code, 200, executed.data)
        self.assertEqual(executed.data["status"], AgentActionStatus.COMPLETED)
        self.assertEqual(executed.data["result_payload"]["kind"], "draft")
        self.assertIsNone(executed.data["ingestion_action_request_id"])

        replay = self.client.post(
            f"/api/v1/org-memory/actions/{proposal_id}/execute",
            {},
            format="json",
            **self._headers(idempotency_key="execute-draft-gmail"),
        )
        self.assertEqual(replay.status_code, 200, replay.data)
        self.assertFalse(replay.data["changed"])
        self.assertNotIn(
            secret_body,
            str(
                list(
                    AgentActionEvent.objects.filter(
                        proposal_id=proposal_id
                    ).values_list("metadata", flat=True)
                )
            ),
        )

    def test_linear_write_requires_independent_approval_and_live_preconditions(self):
        created = self._create_linear_action()
        self.assertEqual(created.status_code, 201, created.data)
        self.assertTrue(created.data["requires_approval"])
        self.assertEqual(created.data["risk_level"], AgentActionRiskLevel.MEDIUM)
        self.assertEqual(created.data["status"], AgentActionStatus.AWAITING_APPROVAL)
        proposal_id = created.data["id"]

        self_approval = self.client.post(
            f"/api/v1/org-memory/actions/{proposal_id}/approve",
            {},
            format="json",
            **self._headers(idempotency_key="self-approve-linear"),
        )
        self.assertEqual(self_approval.status_code, 400, self_approval.data)
        self.assertIn("different", self_approval.data["detail"])

        approved = self._approve(proposal_id)
        self.assertEqual(approved.status_code, 200, approved.data)
        self.assertEqual(approved.data["status"], AgentActionStatus.APPROVED)

        self.fake_create.revision += 1
        stale = self.client.post(
            f"/api/v1/org-memory/actions/{proposal_id}/execute",
            {},
            format="json",
            **self._headers(idempotency_key="execute-stale-linear"),
        )
        self.assertEqual(stale.status_code, 400, stale.data)
        self.assertIn("fresh approval", stale.data["detail"])
        proposal = AgentActionProposal.objects.get(pk=proposal_id)
        self.assertEqual(proposal.status, AgentActionStatus.STALE)
        self.assertIsNone(proposal.approved_by_id)
        self.assertEqual(self.fake_create.executions, 0)

        reapproved = self._approve(proposal_id, key="reapprove-linear-action")
        self.assertEqual(reapproved.status_code, 200, reapproved.data)
        executed = self.client.post(
            f"/api/v1/org-memory/actions/{proposal_id}/execute",
            {},
            format="json",
            **self._headers(idempotency_key="execute-approved-linear"),
        )
        self.assertEqual(executed.status_code, 200, executed.data)
        self.assertEqual(executed.data["status"], AgentActionStatus.COMPLETED)
        self.assertTrue(executed.data["reversal_supported"])
        self.assertEqual(self.fake_create.executions, 1)
        self.assertEqual(MemorySourceActionRequest.objects.count(), 1)
        self.assertIsNotNone(executed.data["ingestion_action_request_id"])

        replay = self.client.post(
            f"/api/v1/org-memory/actions/{proposal_id}/execute",
            {},
            format="json",
            **self._headers(idempotency_key="execute-approved-linear"),
        )
        self.assertEqual(replay.status_code, 200, replay.data)
        self.assertFalse(replay.data["changed"])
        self.assertEqual(self.fake_create.executions, 1)

        self.fake_create.revision += 1
        unsafe_reversal = self.client.post(
            f"/api/v1/org-memory/actions/{proposal_id}/reverse",
            {"confirm": True},
            format="json",
            **self._headers(
                reviewer=True,
                idempotency_key="unsafe-reverse-linear",
            ),
        )
        self.assertEqual(unsafe_reversal.status_code, 400, unsafe_reversal.data)
        self.assertIn("changed", unsafe_reversal.data["detail"])
        self.assertEqual(
            AgentActionProposal.objects.get(pk=proposal_id).status,
            AgentActionStatus.COMPLETED,
        )
        self.fake_create.revision -= 1
        reversed_response = self.client.post(
            f"/api/v1/org-memory/actions/{proposal_id}/reverse",
            {"confirm": True},
            format="json",
            **self._headers(
                reviewer=True,
                idempotency_key="reverse-approved-linear",
            ),
        )
        self.assertEqual(
            reversed_response.status_code,
            200,
            reversed_response.data,
        )
        self.assertEqual(
            reversed_response.data["status"],
            AgentActionStatus.REVERSED,
        )
        self.assertEqual(self.fake_create.reversals, 1)

    def test_proposal_idempotency_scope_and_unsupported_actions_fail_closed(self):
        created = self._create_linear_action(key="same-linear-proposal")
        self.assertEqual(created.status_code, 201, created.data)
        replay = self._create_linear_action(key="same-linear-proposal")
        self.assertEqual(replay.status_code, 200, replay.data)
        self.assertFalse(replay.data["created"])
        self.assertEqual(AgentActionProposal.objects.count(), 1)

        changed = self.client.post(
            "/api/v1/org-memory/actions",
            {
                "action_type": AgentActionType.CREATE_LINEAR_ISSUE,
                "configuration_id": str(self.configuration.pk),
                "input_payload": {
                    "title": "A different issue",
                    "team_id": "TEAM-1",
                    "project_id": "PROJECT-1",
                },
            },
            format="json",
            **self._headers(idempotency_key="same-linear-proposal"),
        )
        self.assertEqual(changed.status_code, 400, changed.data)

        outside_scope = self.client.post(
            "/api/v1/org-memory/actions",
            {
                "action_type": AgentActionType.CREATE_LINEAR_ISSUE,
                "configuration_id": str(self.configuration.pk),
                "input_payload": {
                    "title": "Outside scope",
                    "team_id": "TEAM-1",
                    "project_id": "PROJECT-OTHER",
                },
            },
            format="json",
            **self._headers(idempotency_key="outside-linear-scope"),
        )
        self.assertEqual(outside_scope.status_code, 400, outside_scope.data)
        self.assertIn("approved scope", outside_scope.data["detail"])

        unsupported = self.client.post(
            "/api/v1/org-memory/actions",
            {
                "action_type": "create_xero_payment",
                "input_payload": {"amount": "1000.00"},
            },
            format="json",
            **self._headers(idempotency_key="unsupported-payment"),
        )
        self.assertEqual(unsupported.status_code, 400, unsupported.data)
        self.assertEqual(AgentActionProposal.objects.count(), 1)

    def test_revoked_evidence_blocks_approval(self):
        claim, source = self._claim()
        created = self._create_linear_action(
            key="evidence-linear-action",
            evidence_claim_ids=[str(claim.pk)],
        )
        self.assertEqual(created.status_code, 201, created.data)
        revoke_source_access(source, reason="fixture_revocation")
        blocked = self._approve(created.data["id"], key="approve-revoked-evidence")
        self.assertEqual(blocked.status_code, 400, blocked.data)
        self.assertIn("inaccessible", blocked.data["detail"])
        self.assertEqual(
            AgentActionProposal.objects.get(pk=created.data["id"]).status,
            AgentActionStatus.AWAITING_APPROVAL,
        )

    def test_failed_external_execution_is_visible_and_never_auto_retried(self):
        created = self._create_linear_action(key="failing-linear-action")
        approved = self._approve(
            created.data["id"],
            key="approve-failing-linear-action",
        )
        self.assertEqual(approved.status_code, 200, approved.data)
        self.fake_create.fail_execution = True
        failed = self.client.post(
            f"/api/v1/org-memory/actions/{created.data['id']}/execute",
            {},
            format="json",
            **self._headers(idempotency_key="execute-failing-linear"),
        )
        self.assertEqual(failed.status_code, 400, failed.data)
        proposal = AgentActionProposal.objects.get(pk=created.data["id"])
        self.assertEqual(proposal.status, AgentActionStatus.FAILED)
        self.assertFalse(proposal.result_payload["retry_safe"])
        self.assertEqual(self.fake_create.executions, 1)

        retried = self.client.post(
            f"/api/v1/org-memory/actions/{created.data['id']}/execute",
            {},
            format="json",
            **self._headers(idempotency_key="execute-failing-linear"),
        )
        self.assertEqual(retried.status_code, 400, retried.data)
        self.assertEqual(self.fake_create.executions, 1)

    def test_public_roo_surface_and_disabled_feature_cannot_reach_gateway(self):
        denied = self.client.get(
            "/api/v1/org-memory/actions",
            **self._headers(public_surface=True),
        )
        self.assertEqual(denied.status_code, 401, denied.data)

        with self.settings(ORG_MEMORY_ACTIONS_ENABLED=False):
            disabled = self.client.get(
                "/api/v1/org-memory/actions",
                **self._headers(),
            )
        self.assertEqual(disabled.status_code, 503, disabled.data)

    def test_database_constraint_blocks_unapproved_write_execution(self):
        created = self._create_linear_action(key="constraint-linear-action")
        self.assertEqual(created.status_code, 201, created.data)
        with self.assertRaises(IntegrityError), transaction.atomic():
            AgentActionProposal.objects.filter(pk=created.data["id"]).update(
                status=AgentActionStatus.EXECUTING,
            )
