import copy
import hashlib
import io
import json
import tempfile
from datetime import timedelta
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.utils import timezone

from integrations.models import ExternalServiceConnection
from organizations.models import Organization
from org_memory.governance import load_policy_manifest
from org_memory.kernel import capture_source_version
from org_memory.models import (
    MemoryConnectionConfiguration,
    MemoryConnectionHealthSnapshot,
    MemoryConnectionHealthStatus,
    MemoryConnectionState,
    MemoryDailyReconciliationReport,
    MemoryDailyReconciliationStatus,
    MemoryPreviewStatus,
    MemoryProviderEnablement,
    MemoryScopeStatus,
    MemorySourcePolicy,
    MemorySourcePreview,
    MemorySourceScope,
    OrganizationCapability,
    OrganizationCapabilityGrant,
    OrganizationIdentity,
    OrganizationIdentityProvider,
    OrganizationMembership,
    ServicePrincipal,
)
from org_memory.pilot_readiness import (
    build_pilot_readiness_report,
    validate_pilot_approval_manifest,
)
from org_memory.service_principals import issue_service_principal_credential


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@override_settings(
    ORG_MEMORY_ENABLED_PROVIDERS="linear",
    ORG_MEMORY_QUERY_API_ENABLED=False,
    ORG_MEMORY_PILOT_ALLOWLIST_KEY_VERSION="test-v1",
    ORG_MEMORY_PILOT_ALLOWLIST_HMAC_KEY="p" * 32,
    ORG_MEMORY_PUBLICATION_ENABLED=False,
    ORG_MEMORY_ACTIONS_ENABLED=False,
    ORG_MEMORY_ACTION_LINEAR_EXECUTION_ENABLED=False,
    ORG_MEMORY_SELECTOR_EXPORT_ENABLED=False,
    ORG_MEMORY_SELECTOR_SHADOW_ENABLED=False,
    ORG_MEMORY_SELECTOR_MIN_LABELED_TRACES=3000,
    ORG_MEMORY_ACTOR_ASSERTION_MAX_AGE_SECONDS=60,
    ORG_MEMORY_ACTOR_ASSERTION_CLOCK_SKEW_SECONDS=5,
    ORG_MEMORY_PROVIDER_FRESHNESS_SLO_SECONDS={"linear": 86400},
)
class PilotReadinessTests(TestCase):
    def setUp(self):
        self.now = timezone.now()
        self.organization = Organization.objects.create(
            name="Pilot Brain",
            domain="pilot-brain.test",
        )
        self.user = get_user_model().objects.create_user(
            email="pilot-admin@mlai.test"
        )
        self.membership = OrganizationMembership.objects.create(
            organization=self.organization,
            user=self.user,
        )
        capability, _ = OrganizationCapability.objects.get_or_create(
            key="view_general_memory",
            defaults={"name": "View general memory"},
        )
        OrganizationCapabilityGrant.objects.create(
            membership=self.membership,
            capability=capability,
        )
        OrganizationIdentity.objects.create(
            organization=self.organization,
            user=self.user,
            provider=OrganizationIdentityProvider.SLACK,
            external_tenant_id="TPILOT123",
            external_user_id="UPILOT123",
            verified_at=self.now,
        )
        principal = ServicePrincipal.objects.create(
            name="pilot-readiness-admin-roo",
            organization=self.organization,
            scopes=["org_memory.read"],
            allowed_surfaces=["admin_roo"],
        )
        issue_service_principal_credential(principal)

        MemoryProviderEnablement.objects.create(
            organization=self.organization,
            provider="linear",
            is_enabled=True,
            approved_by=self.user,
            approved_at=self.now,
            reason="approved pilot provider",
        )
        external_connection = ExternalServiceConnection.objects.create(
            provider="linear",
            user=self.user,
            organization=self.organization,
            external_account_id="pilot-linear-account",
            account_label="Pilot Linear",
        )
        policy = MemorySourcePolicy.objects.create(
            organization=self.organization,
            provider="linear",
            policy_key="pilot-linear",
            name="Pilot Linear policy",
            scope_type="project",
            classification="committee",
            allowed_memory_kinds=["task", "project_status"],
            retention_policy={"raw_evidence_days": 365},
            reviewed_by=self.user,
            reviewed_at=self.now,
        )
        self.configuration = MemoryConnectionConfiguration.objects.create(
            organization=self.organization,
            provider="linear",
            external_connection=external_connection,
            default_policy=policy,
            lifecycle_state=MemoryConnectionState.ACTIVE,
            approved_by=self.user,
            approved_at=self.now,
            created_by=self.user,
            last_dry_run_at=self.now,
            last_successful_sync_at=self.now,
        )
        MemorySourceScope.objects.create(
            configuration=self.configuration,
            scope_type="project",
            external_id="pilot-project",
            name="Pilot project",
            selected=True,
            status=MemoryScopeStatus.SELECTED,
            default_classification="committee",
            policy=policy,
        )
        preview = MemorySourcePreview.objects.create(
            configuration=self.configuration,
            version=1,
            status=MemoryPreviewStatus.READY,
            is_current=True,
            selection_fingerprint=digest("pilot-selection"),
            selection_snapshot=[{"scope": "pseudonymised-test-scope"}],
            policy_snapshot={"policy_key": "pilot-linear"},
            dry_run_summary={"records": 1},
            dry_run_completed_at=self.now,
            dry_run_by=self.user,
            requested_by=self.user,
        )
        self.configuration.approved_preview = preview
        self.configuration.save(
            update_fields=("approved_preview", "updated_at")
        )
        capture_source_version(
            organization=self.organization,
            provider="linear",
            external_account_id="pilot-linear-account",
            source_type="issue",
            external_id="PILOT-1",
            version_key="v1",
            content_hash=digest("Pilot status is green."),
            classification="committee",
            acl={
                "is_accessible": True,
                "provider_revision": "acl-v1",
                "principal_refs": ["team:pilot"],
            },
            chunks=[{"ordinal": 0, "text": "Pilot status is green."}],
            configuration=self.configuration,
        )
        report = MemoryDailyReconciliationReport.objects.create(
            organization=self.organization,
            report_date=self.now.date(),
            time_zone="UTC",
            window_started_at=self.now - timedelta(minutes=5),
            status=MemoryDailyReconciliationStatus.COMPLETED,
            summary={"content_free": True},
            alerts=[],
            started_at=self.now - timedelta(minutes=5),
            completed_at=self.now,
        )
        MemoryConnectionHealthSnapshot.objects.create(
            report=report,
            organization=self.organization,
            configuration=self.configuration,
            provider="linear",
            health_status=MemoryConnectionHealthStatus.HEALTHY,
            schedule_status="completed",
            credential_status="connected",
            freshness_status="current",
            source_lag_seconds=0,
            last_successful_sync_at=self.now,
        )

    def approval_manifest(self):
        return {
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
            "pilot_admin_refs": ["slack:UPILOT123"],
            "allowed_slack_contexts": [
                "dm:UPILOT123",
                "channel:GPILOT123",
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

    def governance_manifest(self):
        manifest = copy.deepcopy(load_policy_manifest())
        manifest["status"] = "approved"
        manifest["last_reviewed_at"] = self.now.isoformat()
        manifest["owners"] = {
            "data": "Data Owner",
            "security": "Security Owner",
            "review": "Review Owner",
            "operations": "Operations Owner",
            "privacy_legal": "Privacy Owner",
        }
        manifest["global_rules"]["hard_delete_rules_approved"] = True
        manifest["slos"]["approval_status"] = "approved"
        manifest["slos"]["approved_by"] = "Operations Owner"
        manifest["slos"]["cost_limits"] = {
            "daily_model_budget_aud": 25,
            "monthly_model_budget_aud": 500,
            "on_limit": "pause_new_model_work_and_alert",
        }
        policy = manifest["providers"]["linear"]
        policy["production_enabled"] = True
        policy["source_scope"]["selectors"] = ["project:pilot-project"]
        policy["retention"] = {
            "raw_evidence_days": 365,
            "derived_memory_days": 730,
            "query_audit_days": 90,
        }
        policy["review_owner"] = "Review Owner"
        policy["approval"] = {
            "status": "approved",
            "approved_by": "Data Owner",
            "approved_at": self.now.isoformat(),
            "terms_reviewed_by": "Security Owner",
            "terms_reviewed_at": self.now.isoformat(),
        }
        return manifest

    def write_manifests(self, directory):
        approval_path = Path(directory) / "pilot-approval.json"
        governance_path = Path(directory) / "provider-governance.json"
        approval_path.write_text(
            json.dumps(self.approval_manifest()),
            encoding="utf-8",
        )
        governance_path.write_text(
            json.dumps(self.governance_manifest()),
            encoding="utf-8",
        )
        return approval_path, governance_path

    def test_approval_manifest_is_exact_scoped_and_expires(self):
        valid = self.approval_manifest()
        self.assertEqual(
            validate_pilot_approval_manifest(
                valid,
                organization_domain=self.organization.domain,
                now=self.now,
            ),
            [],
        )

        invalid = copy.deepcopy(valid)
        invalid["pilot_admin_refs"].append("slack:UOTHER123")
        invalid["allowed_slack_contexts"].append("dm:UNAPPROVED")
        invalid["review_due_at"] = (self.now - timedelta(seconds=1)).isoformat()
        errors = validate_pilot_approval_manifest(
            invalid,
            organization_domain=self.organization.domain,
            now=self.now,
        )
        self.assertIn("approval_dm_actor_mismatch", errors)
        self.assertIn("approval_review_expired", errors)

        public_channel = copy.deepcopy(valid)
        public_channel["allowed_slack_contexts"] = ["channel:CPUBLIC123"]
        errors = validate_pilot_approval_manifest(
            public_channel,
            organization_domain=self.organization.domain,
            now=self.now,
        )
        self.assertIn("approval_slack_contexts_invalid", errors)

        public_admin_scope = copy.deepcopy(valid)
        public_admin_scope["allowed_slack_contexts"].append(
            "public_channels:pilot_admins"
        )
        self.assertEqual(
            validate_pilot_approval_manifest(
                public_admin_scope,
                organization_domain=self.organization.domain,
                now=self.now,
            ),
            [],
        )

    def test_complete_preflight_is_ready_without_enabling_query_api(self):
        with tempfile.TemporaryDirectory() as directory:
            _, governance_path = self.write_manifests(directory)
            report = build_pilot_readiness_report(
                organization=self.organization,
                approval_manifest=self.approval_manifest(),
                governance_manifest_path=governance_path,
                environment="staging",
                now=self.now,
            )

        self.assertTrue(report["ready"], report["blockers"])
        self.assertEqual(report["blockers"], [])
        self.assertIn("query_api_activation_pending", report["warnings"])
        self.assertIn("selector_label_gate_not_met", report["warnings"])
        self.assertNotIn("UPILOT123", json.dumps(report))
        self.assertNotIn("Pilot status is green", json.dumps(report))

    def test_live_mode_requires_query_api_and_optional_features_remain_off(self):
        with tempfile.TemporaryDirectory() as directory:
            _, governance_path = self.write_manifests(directory)
            disabled = build_pilot_readiness_report(
                organization=self.organization,
                approval_manifest=self.approval_manifest(),
                governance_manifest_path=governance_path,
                environment="staging",
                live=True,
                now=self.now,
            )
            with override_settings(
                ORG_MEMORY_QUERY_API_ENABLED=True,
                ORG_MEMORY_ACTIONS_ENABLED=True,
            ):
                unsafe = build_pilot_readiness_report(
                    organization=self.organization,
                    approval_manifest=self.approval_manifest(),
                    governance_manifest_path=governance_path,
                    environment="staging",
                    live=True,
                    now=self.now,
                )

        self.assertIn("query_api_not_enabled", disabled["blockers"])
        self.assertNotIn("query_api_not_enabled", unsafe["blockers"])
        self.assertIn("optional_features_enabled", unsafe["blockers"])

    def test_scope_and_production_search_infrastructure_must_match(self):
        approval = self.approval_manifest()
        approval["approved_source_scopes"] = {
            "linear": ["project:not-the-selected-project"],
        }
        with tempfile.TemporaryDirectory() as directory:
            _, governance_path = self.write_manifests(directory)
            report = build_pilot_readiness_report(
                organization=self.organization,
                approval_manifest=approval,
                governance_manifest_path=governance_path,
                environment="production",
                now=self.now,
            )

        self.assertIn("provider_governance_invalid", report["blockers"])
        self.assertIn("selected_source_scopes_not_exact", report["blockers"])
        self.assertIn(
            "production_search_infrastructure_not_ready",
            report["blockers"],
        )
        self.assertNotIn("not-the-selected-project", json.dumps(report))

    def test_exact_actor_and_fresh_daily_report_are_enforced(self):
        extra_user = get_user_model().objects.create_user(
            email="extra-pilot@mlai.test"
        )
        extra_membership = OrganizationMembership.objects.create(
            organization=self.organization,
            user=extra_user,
        )
        capability = OrganizationCapability.objects.get(
            key="view_general_memory"
        )
        OrganizationCapabilityGrant.objects.create(
            membership=extra_membership,
            capability=capability,
        )
        OrganizationIdentity.objects.create(
            organization=self.organization,
            user=extra_user,
            provider=OrganizationIdentityProvider.SLACK,
            external_tenant_id="TPILOT123",
            external_user_id="UEXTRA123",
            verified_at=self.now,
        )
        report_row = self.organization.memory_daily_reconciliation_reports.get()
        report_row.status = MemoryDailyReconciliationStatus.DEGRADED
        report_row.save(update_fields=("status", "updated_at"))
        with tempfile.TemporaryDirectory() as directory:
            _, governance_path = self.write_manifests(directory)
            report = build_pilot_readiness_report(
                organization=self.organization,
                approval_manifest=self.approval_manifest(),
                governance_manifest_path=governance_path,
                environment="staging",
                now=self.now,
            )

        self.assertIn("pilot_actors_not_exact", report["blockers"])
        self.assertIn(
            "daily_reconciliation_not_healthy",
            report["blockers"],
        )

    def test_command_emits_json_then_fails_when_live_gate_is_blocked(self):
        with tempfile.TemporaryDirectory() as directory:
            approval_path, governance_path = self.write_manifests(directory)
            output = io.StringIO()
            with self.assertRaises(CommandError):
                call_command(
                    "check_org_memory_pilot_readiness",
                    organization_domain=self.organization.domain,
                    approval_manifest=str(approval_path),
                    governance_manifest=str(governance_path),
                    environment="staging",
                    live=True,
                    fail_on_blockers=True,
                    stdout=output,
                )

        payload = json.loads(output.getvalue())
        self.assertFalse(payload["ready"])
        self.assertIn("query_api_not_enabled", payload["blockers"])
