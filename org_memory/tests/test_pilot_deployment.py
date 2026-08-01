import io
import json
import tempfile
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from organizations.models import Organization
from org_memory.assertions import actor_identity_headers, build_actor_assertion
from org_memory.models import (
    MemoryPilotDeployment,
    MemoryPilotDeploymentState,
    MemoryPilotSuspensionReason,
    OrganizationCapability,
    OrganizationCapabilityGrant,
    OrganizationIdentity,
    OrganizationMembership,
    OrganizationSlackWorkspace,
    ServicePrincipal,
)
from org_memory.pilot_deployment import (
    PilotDeploymentError,
    activate_pilot_deployment,
    actor_has_active_pilot_access,
    approval_allowlist_hashes,
    pilot_access_matrix_report,
    pilot_deployment_readiness,
    pilot_deployment_report,
    pilot_release_gate_report,
    stage_pilot_deployment,
    suspend_pilot_deployments,
)
from org_memory.pilot_readiness import pilot_approval_manifest_hash
from org_memory.service_principals import issue_service_principal_credential
from roo.models import PointsAdmin


@override_settings(
    ORG_MEMORY_QUERY_API_ENABLED=True,
    ORG_MEMORY_QUERY_VECTOR_ENABLED=False,
    ORG_MEMORY_PILOT_ALLOWLIST_KEY_VERSION="test-v1",
    ORG_MEMORY_PILOT_ALLOWLIST_HMAC_KEY="runtime-pilot-test-secret-value-1234",
)
class PilotDeploymentTests(TestCase):
    def setUp(self):
        self.now = timezone.now()
        self.organization = Organization.objects.create(
            name="Runtime Pilot",
            domain="runtime-pilot.test",
        )
        User = get_user_model()
        self.pilot_user = User.objects.create_user(email="pilot@runtime.test")
        self.stage_operator = User.objects.create_user(
            email="stage@runtime.test"
        )
        self.activate_operator = User.objects.create_user(
            email="activate@runtime.test"
        )
        self.emergency_operator = User.objects.create_user(
            email="emergency@runtime.test"
        )
        view_capability = OrganizationCapability.objects.get(
            key="view_general_memory"
        )
        manage_capability = OrganizationCapability.objects.get(
            key="manage_sources"
        )
        for user in (
            self.pilot_user,
            self.stage_operator,
            self.activate_operator,
            self.emergency_operator,
        ):
            membership = OrganizationMembership.objects.create(
                organization=self.organization,
                user=user,
                joined_at=self.now - timedelta(minutes=1),
            )
            if user == self.pilot_user:
                OrganizationCapabilityGrant.objects.create(
                    membership=membership,
                    capability=view_capability,
                    valid_from=self.now - timedelta(minutes=1),
                )
            else:
                OrganizationCapabilityGrant.objects.create(
                    membership=membership,
                    capability=manage_capability,
                    valid_from=self.now - timedelta(minutes=1),
                )
        OrganizationSlackWorkspace.objects.create(
            organization=self.organization,
            slack_team_id="TRUNTIME1",
        )
        OrganizationIdentity.objects.create(
            organization=self.organization,
            user=self.pilot_user,
            provider="slack",
            external_tenant_id="TRUNTIME1",
            external_user_id="UPILOT1",
            verified_at=self.now,
        )
        PointsAdmin.objects.create(
            slack_user_id="UPILOT1",
            user=self.pilot_user,
            role="committee",
            is_active=True,
        )
        self.principal = ServicePrincipal.objects.create(
            name="runtime-pilot-admin-roo",
            organization=self.organization,
            scopes=["org_memory.read"],
            allowed_surfaces=["admin_roo"],
        )
        self.credential, self.token = issue_service_principal_credential(
            self.principal
        )
        self.approval = {
            "schema_version": 1,
            "organization_domain": self.organization.domain,
            "approval_status": "approved",
            "approved_at": (self.now - timedelta(days=1)).isoformat(),
            "review_due_at": (self.now + timedelta(days=30)).isoformat(),
            "approvers": {
                "data": "Data Owner",
                "security": "Security Owner",
                "review": "Review Owner",
                "operations": "Operations Owner",
            },
            "pilot_admin_refs": ["slack:UPILOT1"],
            "allowed_slack_contexts": [
                "dm:UPILOT1",
                "channel:GPRIVATE1",
            ],
            "approved_providers": ["linear"],
            "approved_source_scopes": {
                "linear": ["project:pilot-project"],
            },
            "controls": {
                "data_processing_terms_approved": True,
                "retention_and_deletion_approved": True,
                "backup_restore_tested": True,
                "incident_response_runbook_approved": True,
                "freshness_latency_cost_slos_approved": True,
                "public_roo_isolation_verified": True,
            },
        }
        self.request_number = 0

    def readiness(self, *, live):
        return {
            "schema_version": "org-memory-pilot-readiness-v1",
            "organization_domain": self.organization.domain,
            "stage": (
                "live_read_only_pilot"
                if live
                else "preflight_read_only_pilot"
            ),
            "approval_manifest_hash": pilot_approval_manifest_hash(
                self.approval
            ),
            "ready": True,
            "blockers": [],
            "warnings": [],
            "checks": [],
        }

    def stage(self, *, approval=None, operator=None, key="pilot-stage-1"):
        return stage_pilot_deployment(
            organization=self.organization,
            approval_manifest=approval or self.approval,
            readiness_report=self.readiness(live=False),
            operator=operator or self.stage_operator,
            idempotency_key=key,
            now=self.now,
        )

    def activate(self, *, operator=None, key="pilot-activate-1"):
        return activate_pilot_deployment(
            organization=self.organization,
            approval_manifest=self.approval,
            readiness_report=self.readiness(live=True),
            operator=operator or self.activate_operator,
            idempotency_key=key,
            now=self.now + timedelta(minutes=1),
        )

    def actor(self, *, user="UPILOT1", channel="GPRIVATE1"):
        return SimpleNamespace(
            organization=self.organization,
            surface="admin_roo",
            slack_user_id=user,
            slack_channel_id=channel,
        )

    def headers(
        self,
        *,
        channel="GPRIVATE1",
        user="UPILOT1",
        surface="admin_roo",
    ):
        self.request_number += 1
        request_id = f"runtime-pilot-{self.request_number}"
        event_id = f"EvRUNTIME{self.request_number}"
        assertion = build_actor_assertion(
            self.token,
            credential_id=str(self.credential.pk),
            surface=surface,
            slack_team_id="TRUNTIME1",
            acting_slack_user_id=user,
            slack_channel_id=channel,
            slack_thread_ts="1700000000.123",
            event_id=event_id,
            request_id=request_id,
        )
        identity = actor_identity_headers(
            assertion=assertion,
            surface=surface,
            slack_team_id="TRUNTIME1",
            acting_slack_user_id=user,
            slack_channel_id=channel,
            slack_thread_ts="1700000000.123",
            event_id=event_id,
            request_id=request_id,
        )
        return {
            "HTTP_AUTHORIZATION": f"ServicePrincipal {self.token}",
            **{
                f"HTTP_{key.upper().replace('-', '_')}": value
                for key, value in identity.items()
            },
        }

    def test_staging_is_hashed_idempotent_and_operator_separated(self):
        deployment, created = self.stage()
        self.assertTrue(created)
        rendered = json.dumps(
            {
                "actors": deployment.actor_ref_hashes,
                "contexts": deployment.context_ref_hashes,
            }
        )
        self.assertNotIn("UPILOT1", rendered)
        self.assertNotIn("GPRIVATE1", rendered)
        repeated, created = self.stage()
        self.assertFalse(created)
        self.assertEqual(repeated.pk, deployment.pk)

        pilot_membership = OrganizationMembership.objects.get(
            organization=self.organization,
            user=self.pilot_user,
        )
        OrganizationCapabilityGrant.objects.create(
            membership=pilot_membership,
            capability=OrganizationCapability.objects.get(key="manage_sources"),
            valid_from=self.now - timedelta(minutes=1),
        )
        other_approval = dict(self.approval)
        other_approval["review_due_at"] = (
            self.now + timedelta(days=29)
        ).isoformat()
        other_readiness = self.readiness(live=False)
        other_readiness["approval_manifest_hash"] = pilot_approval_manifest_hash(
            other_approval
        )
        with self.assertRaises(PilotDeploymentError):
            stage_pilot_deployment(
                organization=self.organization,
                approval_manifest=other_approval,
                readiness_report=other_readiness,
                operator=self.pilot_user,
                idempotency_key="pilot-stage-actor",
                now=self.now,
            )

    def test_staging_rejects_malformed_approval_despite_ready_report(self):
        malformed = dict(self.approval)
        malformed["pilot_admin_refs"] = ["slack:UPILOT1", "not-a-slack-user"]
        readiness = self.readiness(live=False)
        readiness["approval_manifest_hash"] = pilot_approval_manifest_hash(
            malformed
        )
        with self.assertRaisesMessage(
            PilotDeploymentError,
            "Pilot approval manifest is invalid.",
        ):
            stage_pilot_deployment(
                organization=self.organization,
                approval_manifest=malformed,
                readiness_report=readiness,
                operator=self.stage_operator,
                idempotency_key="malformed-approval",
                now=self.now,
            )
        self.assertFalse(MemoryPilotDeployment.objects.exists())

    def test_activation_requires_live_evidence_flag_and_independent_operator(self):
        self.stage()
        with self.assertRaises(PilotDeploymentError):
            activate_pilot_deployment(
                organization=self.organization,
                approval_manifest=self.approval,
                readiness_report=self.readiness(live=True),
                operator=self.stage_operator,
                idempotency_key="same-operator",
                now=self.now + timedelta(minutes=1),
            )
        invalid = self.readiness(live=True)
        invalid["approval_manifest_hash"] = "f" * 64
        with self.assertRaises(PilotDeploymentError):
            activate_pilot_deployment(
                organization=self.organization,
                approval_manifest=self.approval,
                readiness_report=invalid,
                operator=self.activate_operator,
                idempotency_key="invalid-readiness",
                now=self.now + timedelta(minutes=1),
            )
        with override_settings(ORG_MEMORY_QUERY_API_ENABLED=False):
            with self.assertRaises(PilotDeploymentError):
                self.activate()

        deployment, changed = self.activate()
        self.assertTrue(changed)
        self.assertEqual(deployment.state, MemoryPilotDeploymentState.ACTIVE)
        repeated, changed = self.activate()
        self.assertFalse(changed)
        self.assertEqual(repeated.pk, deployment.pk)

    def test_runtime_access_is_exact_context_expiring_and_key_bound(self):
        self.stage()
        deployment, _changed = self.activate()
        self.assertTrue(actor_has_active_pilot_access(self.actor(), now=self.now))
        self.assertTrue(
            actor_has_active_pilot_access(
                self.actor(channel="DPRIVATE1"),
                now=self.now,
            )
        )
        self.assertFalse(
            actor_has_active_pilot_access(
                self.actor(channel="GPUBLIC1"),
                now=self.now,
            )
        )
        self.assertFalse(
            actor_has_active_pilot_access(
                self.actor(user="UOTHER1"),
                now=self.now,
            )
        )
        self.assertFalse(
            actor_has_active_pilot_access(
                self.actor(),
                now=deployment.approval_review_due_at,
            )
        )
        with override_settings(
            ORG_MEMORY_PILOT_ALLOWLIST_KEY_VERSION="rotated-v2",
            ORG_MEMORY_PILOT_ALLOWLIST_HMAC_KEY="rotated-pilot-secret-value-123456",
        ):
            self.assertFalse(actor_has_active_pilot_access(self.actor()))

    def test_suspension_is_immediate_and_covers_staged_and_active(self):
        self.stage()
        self.activate()
        changed = suspend_pilot_deployments(
            organization=self.organization,
            operator=self.emergency_operator,
            reason=MemoryPilotSuspensionReason.SUSPECTED_LEAK,
            now=self.now + timedelta(minutes=2),
        )
        self.assertEqual(changed, 1)
        self.assertFalse(actor_has_active_pilot_access(self.actor()))
        deployment = MemoryPilotDeployment.objects.get()
        self.assertEqual(
            deployment.state,
            MemoryPilotDeploymentState.SUSPENDED,
        )
        self.assertEqual(
            deployment.suspension_reason,
            MemoryPilotSuspensionReason.SUSPECTED_LEAK,
        )

    def test_runtime_readiness_distinguishes_staged_and_active(self):
        initial = pilot_deployment_readiness(
            organization=self.organization,
            approval_manifest=self.approval,
            live=False,
            now=self.now,
        )
        self.assertEqual(initial["status"], "warn")
        self.assertEqual(initial["code"], "runtime_pilot_not_staged")
        self.stage()
        staged_live = pilot_deployment_readiness(
            organization=self.organization,
            approval_manifest=self.approval,
            live=True,
            now=self.now,
        )
        self.assertEqual(staged_live["status"], "block")
        transition = pilot_deployment_readiness(
            organization=self.organization,
            approval_manifest=self.approval,
            live=True,
            allow_staged_activation=True,
            now=self.now,
        )
        self.assertEqual(transition["status"], "pass")
        self.activate()
        active = pilot_deployment_readiness(
            organization=self.organization,
            approval_manifest=self.approval,
            live=True,
            now=self.now + timedelta(minutes=2),
        )
        self.assertEqual(active["status"], "pass")
        self.assertEqual(active["code"], "runtime_pilot_binding_active")

    def test_private_query_api_denies_until_exact_deployment_is_active(self):
        client = APIClient()
        denied = client.post(
            "/api/v1/org-memory/search",
            {"query": "What is the current pilot status?"},
            format="json",
            **self.headers(),
        )
        self.assertEqual(denied.status_code, 403)
        self.stage()
        staged = client.post(
            "/api/v1/org-memory/search",
            {"query": "What is the current pilot status?"},
            format="json",
            **self.headers(),
        )
        self.assertEqual(staged.status_code, 403)
        self.activate()
        allowed = client.post(
            "/api/v1/org-memory/search",
            {"query": "What is the current pilot status?"},
            format="json",
            **self.headers(),
        )
        self.assertEqual(allowed.status_code, 200)
        wrong_context = client.post(
            "/api/v1/org-memory/search",
            {"query": "What is the current pilot status?"},
            format="json",
            **self.headers(channel="GPUBLIC1"),
        )
        self.assertEqual(wrong_context.status_code, 403)

    def test_signed_access_probe_is_content_free_and_fail_closed(self):
        endpoint = "/api/v1/org-memory/pilot/access-check"
        client = APIClient()

        inactive = client.get(endpoint, **self.headers())
        self.assertEqual(inactive.status_code, 403)

        self.stage()
        staged = client.get(endpoint, **self.headers())
        self.assertEqual(staged.status_code, 403)

        self.activate()
        allowed = client.get(endpoint, **self.headers())
        self.assertEqual(allowed.status_code, 200)
        self.assertEqual(
            allowed.data,
            {
                "schema_version": "org-memory-pilot-access-probe-v1",
                "ready": True,
                "code": "active_pilot_access_granted",
            },
        )
        rendered = json.dumps(allowed.data)
        self.assertNotIn("UPILOT1", rendered)
        self.assertNotIn("GPRIVATE1", rendered)
        self.assertNotIn(self.organization.domain, rendered)

        wrong_private_context = client.get(
            endpoint,
            **self.headers(channel="GUNAPPROVED1"),
        )
        public_context = client.get(
            endpoint,
            **self.headers(channel="CPUBLIC1"),
        )
        unmapped_actor = client.get(
            endpoint,
            **self.headers(user="UUNAPPROVED1"),
        )
        public_surface = client.get(
            endpoint,
            **self.headers(surface="public_roo"),
        )
        self.assertEqual(wrong_private_context.status_code, 403)
        self.assertEqual(public_context.status_code, 403)
        self.assertEqual(unmapped_actor.status_code, 401)
        self.assertEqual(public_surface.status_code, 401)

        with override_settings(ORG_MEMORY_QUERY_API_ENABLED=False):
            disabled = client.get(endpoint, **self.headers())
        self.assertEqual(disabled.status_code, 503)
        self.assertEqual(disabled.data["code"], "private_query_api_disabled")

    def test_commands_are_dry_run_first_content_free_and_reversible(self):
        approval_hash = pilot_approval_manifest_hash(self.approval)
        preflight = self.readiness(live=False)
        live = self.readiness(live=True)
        with tempfile.TemporaryDirectory() as temporary:
            approval_path = Path(temporary) / "approval.json"
            approval_path.write_text(json.dumps(self.approval), encoding="utf-8")

            output = io.StringIO()
            with patch(
                "org_memory.management.commands.stage_org_memory_pilot."
                "build_pilot_readiness_report",
                return_value=preflight,
            ):
                call_command(
                    "stage_org_memory_pilot",
                    organization_domain=self.organization.domain,
                    approval_manifest=str(approval_path),
                    operator_email=self.stage_operator.email,
                    idempotency_key="command-stage",
                    environment="staging",
                    stdout=output,
                )
            self.assertFalse(json.loads(output.getvalue())["applied"])
            self.assertEqual(MemoryPilotDeployment.objects.count(), 0)

            output = io.StringIO()
            with patch(
                "org_memory.management.commands.stage_org_memory_pilot."
                "build_pilot_readiness_report",
                return_value=preflight,
            ):
                call_command(
                    "stage_org_memory_pilot",
                    organization_domain=self.organization.domain,
                    approval_manifest=str(approval_path),
                    operator_email=self.stage_operator.email,
                    idempotency_key="command-stage",
                    environment="staging",
                    apply=True,
                    stdout=output,
                )
            self.assertEqual(
                MemoryPilotDeployment.objects.get().state,
                MemoryPilotDeploymentState.STAGED,
            )
            self.assertNotIn("UPILOT1", output.getvalue())
            self.assertNotIn("GPRIVATE1", output.getvalue())
            self.assertNotIn(self.stage_operator.email, output.getvalue())
            self.assertIn(approval_hash, output.getvalue())

            output = io.StringIO()
            with patch(
                "org_memory.management.commands.activate_org_memory_pilot."
                "build_pilot_readiness_report",
                return_value=live,
            ):
                call_command(
                    "activate_org_memory_pilot",
                    organization_domain=self.organization.domain,
                    approval_manifest=str(approval_path),
                    operator_email=self.activate_operator.email,
                    idempotency_key="command-activate",
                    environment="staging",
                    apply=True,
                    stdout=output,
                )
            self.assertEqual(
                MemoryPilotDeployment.objects.get().state,
                MemoryPilotDeploymentState.ACTIVE,
            )

            output = io.StringIO()
            call_command(
                "report_org_memory_pilot_deployment",
                organization_domain=self.organization.domain,
                fail_if_ineffective=True,
                stdout=output,
            )
            self.assertTrue(json.loads(output.getvalue())["effective"])

            output = io.StringIO()
            call_command(
                "suspend_org_memory_pilot",
                organization_domain=self.organization.domain,
                operator_email=self.emergency_operator.email,
                reason=MemoryPilotSuspensionReason.MANUAL_STOP,
                apply=True,
                stdout=output,
            )
            self.assertEqual(json.loads(output.getvalue())["suspended"], 1)
            self.assertFalse(pilot_deployment_report(self.organization)["effective"])

    def test_invalid_hmac_configuration_fails_closed(self):
        with override_settings(
            ORG_MEMORY_PILOT_ALLOWLIST_HMAC_KEY="too-short"
        ):
            with self.assertRaises(PilotDeploymentError):
                self.stage()
            self.assertFalse(actor_has_active_pilot_access(self.actor()))

    def test_deployment_report_requires_the_live_query_flag(self):
        self.stage()
        self.activate()
        with override_settings(ORG_MEMORY_QUERY_API_ENABLED=False):
            report = pilot_deployment_report(self.organization)
        self.assertFalse(report["effective"])
        self.assertFalse(report["query_api_enabled"])

    def test_release_gate_requires_a_current_key_matched_binding(self):
        missing = pilot_release_gate_report(
            organization_domain=self.organization.domain,
            now=self.now,
        )
        self.assertFalse(missing["ready"])
        self.assertEqual(
            missing["blockers"],
            ["staged_or_active_pilot_binding_missing"],
        )

        self.stage()
        staged = pilot_release_gate_report(
            organization_domain=self.organization.domain,
            now=self.now,
        )
        self.assertTrue(staged["ready"])
        self.assertEqual(staged["metrics"]["current_key_matched"], 1)

        active_required = pilot_release_gate_report(
            organization_domain=self.organization.domain,
            require_active=True,
            now=self.now,
        )
        self.assertFalse(active_required["ready"])
        self.assertIn(
            "active_pilot_binding_missing",
            active_required["blockers"],
        )

        self.activate()
        active = pilot_release_gate_report(
            organization_domain=self.organization.domain,
            require_active=True,
            now=self.now + timedelta(minutes=2),
        )
        self.assertTrue(active["ready"])

    def test_release_gate_blocks_optional_features_and_invalid_configuration(self):
        self.stage()
        with override_settings(ORG_MEMORY_PUBLICATION_ENABLED=True):
            optional = pilot_release_gate_report(
                organization_domain=self.organization.domain,
                now=self.now,
            )
        self.assertEqual(
            optional["blockers"],
            ["read_only_optional_features_enabled"],
        )

        with override_settings(ORG_MEMORY_PILOT_ALLOWLIST_HMAC_KEY="short"):
            invalid_key = pilot_release_gate_report(
                organization_domain=self.organization.domain,
                now=self.now,
            )
        self.assertIn(
            "pilot_allowlist_key_invalid",
            invalid_key["blockers"],
        )
        self.assertIn(
            "staged_or_active_pilot_binding_missing",
            invalid_key["blockers"],
        )

        with override_settings(
            ORG_MEMORY_QUERY_API_ENABLED=False,
            ORG_MEMORY_PILOT_ALLOWLIST_HMAC_KEY="",
        ):
            disabled = pilot_release_gate_report()
        self.assertTrue(disabled["ready"])
        self.assertEqual(disabled["code"], "private_query_api_disabled")

    def test_release_gate_command_is_content_free(self):
        self.stage()
        output = io.StringIO()
        with override_settings(
            ORG_MEMORY_PILOT_ORGANIZATION_DOMAIN=self.organization.domain,
        ):
            call_command(
                "check_org_memory_pilot_release_gate",
                stdout=output,
            )
        report = json.loads(output.getvalue())
        self.assertTrue(report["ready"])
        self.assertNotIn("UPILOT1", output.getvalue())
        self.assertNotIn("GPRIVATE1", output.getvalue())

    def test_access_matrix_requires_the_exact_active_binding(self):
        missing = pilot_access_matrix_report(
            organization=self.organization,
            approval_manifest=self.approval,
            now=self.now,
        )
        self.assertFalse(missing["ready"])
        self.assertIn(
            "active_pilot_binding_mismatch",
            missing["blockers"],
        )

        self.stage()
        staged = pilot_access_matrix_report(
            organization=self.organization,
            approval_manifest=self.approval,
            now=self.now,
        )
        self.assertFalse(staged["ready"])

        self.activate()
        active = pilot_access_matrix_report(
            organization=self.organization,
            approval_manifest=self.approval,
            now=self.now + timedelta(minutes=2),
        )
        self.assertTrue(active["ready"])
        self.assertEqual(
            active["metrics"],
            {
                "approved_actor_count": 1,
                "approved_private_channel_count": 1,
                "approved_dm_count": 1,
                "expected_allow_cases": 2,
                "allowed_cases": 2,
                "expected_deny_cases": 5,
                "denied_cases": 5,
            },
        )
        rendered = json.dumps(active)
        self.assertNotIn("UPILOT1", rendered)
        self.assertNotIn("GPRIVATE1", rendered)

        changed_approval = dict(self.approval)
        changed_approval["review_due_at"] = (
            self.now + timedelta(days=29)
        ).isoformat()
        mismatched = pilot_access_matrix_report(
            organization=self.organization,
            approval_manifest=changed_approval,
            now=self.now + timedelta(minutes=2),
        )
        self.assertEqual(
            mismatched["blockers"],
            ["active_pilot_binding_mismatch"],
        )

    def test_access_matrix_blocks_disabled_query_and_malformed_approval(self):
        self.stage()
        self.activate()
        with override_settings(ORG_MEMORY_QUERY_API_ENABLED=False):
            disabled = pilot_access_matrix_report(
                organization=self.organization,
                approval_manifest=self.approval,
                now=self.now + timedelta(minutes=2),
            )
        self.assertFalse(disabled["ready"])
        self.assertEqual(
            disabled["blockers"],
            ["query_api_not_enabled"],
        )
        self.assertEqual(disabled["code"], "pilot_access_matrix_blocked")

        malformed = dict(self.approval)
        malformed["allowed_slack_contexts"] = ["channel:CPUBLIC1"]
        invalid = pilot_access_matrix_report(
            organization=self.organization,
            approval_manifest=malformed,
            now=self.now + timedelta(minutes=2),
        )
        self.assertEqual(
            invalid["blockers"],
            ["approval_manifest_invalid"],
        )
        self.assertEqual(
            invalid["metrics"]["expected_allow_cases"],
            0,
        )

    def test_access_matrix_command_is_content_free(self):
        self.stage()
        self.activate()
        with tempfile.TemporaryDirectory() as temporary:
            approval_path = Path(temporary) / "approval.json"
            approval_path.write_text(
                json.dumps(self.approval),
                encoding="utf-8",
            )
            output = io.StringIO()
            call_command(
                "check_org_memory_pilot_access_matrix",
                organization_domain=self.organization.domain,
                approval_manifest=str(approval_path),
                stdout=output,
            )
        report = json.loads(output.getvalue())
        self.assertTrue(report["ready"])
        self.assertNotIn("UPILOT1", output.getvalue())
        self.assertNotIn("GPRIVATE1", output.getvalue())
