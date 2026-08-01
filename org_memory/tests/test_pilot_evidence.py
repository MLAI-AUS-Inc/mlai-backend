import copy
import hashlib
import io
import json
import tempfile
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.utils import timezone

from integrations.models import ExternalServiceConnection
from organizations.models import Organization
from org_memory.models import (
    CapabilityGrantEffect,
    MemoryConnectionConfiguration,
    MemoryConnectionHealthSnapshot,
    MemoryConnectionHealthStatus,
    MemoryDailyCostLedger,
    MemoryDailyReconciliationReport,
    MemoryDailyReconciliationStatus,
    MemoryEvidenceSufficiency,
    MemoryPilotQueryAudit,
    MemoryQueryLog,
    MemoryQueryMode,
    MemoryQueryStatus,
    MemorySourcePolicy,
    OrganizationCapability,
    OrganizationCapabilityGrant,
    OrganizationMembership,
)
from org_memory.pilot_evidence import (
    PilotEvidenceError,
    build_pilot_evidence_report,
    import_pilot_audit_batch,
    validate_pilot_audit_batch,
    validate_pilot_exit_policy,
)


def canonical_hash(value):
    encoded = json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class PilotEvidenceTests(TestCase):
    def setUp(self):
        self.now = timezone.now()
        self.start_at = self.now - timedelta(days=8)
        self.end_at = self.now - timedelta(days=1)
        self.organization = Organization.objects.create(
            name="Evidence Pilot",
            domain="evidence-pilot.test",
        )
        User = get_user_model()
        self.pilot_user = User.objects.create_user(email="pilot@evidence.test")
        self.reviewer = User.objects.create_user(email="reviewer@evidence.test")
        OrganizationMembership.objects.create(
            organization=self.organization,
            user=self.pilot_user,
            joined_at=self.start_at - timedelta(days=2),
        )
        reviewer_membership = OrganizationMembership.objects.create(
            organization=self.organization,
            user=self.reviewer,
            joined_at=self.start_at - timedelta(days=2),
        )
        review_capability = OrganizationCapability.objects.get(
            key="review_claims"
        )
        OrganizationCapabilityGrant.objects.create(
            membership=reviewer_membership,
            capability=review_capability,
            effect=CapabilityGrantEffect.ALLOW,
            valid_from=self.start_at - timedelta(days=2),
        )

        external_connection = ExternalServiceConnection.objects.create(
            provider="linear",
            user=self.pilot_user,
            organization=self.organization,
            external_account_id="pilot-evidence-linear",
            account_label="Pilot evidence",
        )
        policy = MemorySourcePolicy.objects.create(
            organization=self.organization,
            provider="linear",
            policy_key="pilot-evidence-linear",
            name="Pilot evidence Linear",
            scope_type="project",
            classification="committee",
        )
        self.configuration = MemoryConnectionConfiguration.objects.create(
            organization=self.organization,
            provider="linear",
            external_connection=external_connection,
            default_policy=policy,
            created_by=self.pilot_user,
        )

        for day in range(7):
            report_time = self.start_at + timedelta(days=day, hours=1)
            report = MemoryDailyReconciliationReport.objects.create(
                organization=self.organization,
                report_date=report_time.date(),
                time_zone="UTC",
                window_started_at=report_time - timedelta(hours=24),
                status=MemoryDailyReconciliationStatus.COMPLETED,
                summary={"content_free": True},
                alerts=[],
                started_at=report_time - timedelta(minutes=10),
                completed_at=report_time,
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
                freshness_slo_seconds=86400,
                last_successful_sync_at=report_time,
            )
            MemoryDailyCostLedger.objects.create(
                organization=self.organization,
                budget_date=report_time.date(),
                ceiling_aud=Decimal("10"),
                reserved_aud=Decimal("0"),
                consumed_aud=Decimal("1"),
            )

        self.queries = []
        for index in range(20):
            if index < 10:
                mode = MemoryQueryMode.CURRENT_STATE
                status = MemoryQueryStatus.ANSWERED
            elif index < 15:
                mode = MemoryQueryMode.TIMELINE
                status = MemoryQueryStatus.ANSWERED
            else:
                mode = MemoryQueryMode.CURRENT_STATE
                status = MemoryQueryStatus.ABSTAINED
            answered = status == MemoryQueryStatus.ANSWERED
            query = MemoryQueryLog.objects.create(
                organization=self.organization,
                requester_user=self.pilot_user,
                requester_slack_id="UPILOT123",
                channel_id=(
                    "CPUBLIC123" if index == 0 else "GPRIVATE123"
                ),
                request_id=f"pilot-request-{index}",
                query="Private pilot question that must never appear in a report.",
                query_hash=hashlib.sha256(str(index).encode()).hexdigest(),
                query_plan={"mode": str(mode).upper()},
                candidate_trace=[],
                selected_claim_ids=[],
                selected_chunk_ids=[],
                answer="Private answer." if answered else "Insufficient evidence.",
                citation_data=(
                    [
                        {
                            "source_id": "sensitive-source-id",
                            "source_version_id": "sensitive-version-id",
                        }
                    ]
                    if answered
                    else []
                ),
                warnings=[],
                status=status,
                evidence_sufficiency=(
                    MemoryEvidenceSufficiency.SUFFICIENT
                    if answered
                    else MemoryEvidenceSufficiency.INSUFFICIENT
                ),
                confidence=Decimal("0.900") if answered else Decimal("0"),
                selector_version="rules-v1",
                model_name="pilot-model" if answered else "",
                answerer_version="answer-v1" if answered else "",
                prompt_version="prompt-v1" if answered else "",
                schema_version="schema-v1" if answered else "",
                latency_ms=100 + index,
                input_tokens=100 if answered else 0,
                output_tokens=20 if answered else 0,
            )
            created_at = self.start_at + timedelta(hours=2, minutes=index)
            MemoryQueryLog.objects.filter(pk=query.pk).update(
                created_at=created_at
            )
            query.refresh_from_db()
            self.queries.append(query)

        self.approval = {
            "schema_version": 1,
            "organization_domain": self.organization.domain,
            "approval_status": "approved",
            "approved_at": (self.start_at - timedelta(days=1)).isoformat(),
            "review_due_at": (self.now + timedelta(days=30)).isoformat(),
            "approvers": {
                "data": "Data Owner",
                "security": "Security Owner",
                "review": "Review Owner",
                "operations": "Operations Owner",
            },
            "pilot_admin_refs": ["slack:UPILOT123"],
            "allowed_slack_contexts": [
                "channel:GPRIVATE123",
                "public_channels:pilot_admins",
            ],
            "approved_providers": ["linear"],
            "approved_source_scopes": {"linear": ["project:pilot-project"]},
            "controls": {
                "data_processing_terms_approved": True,
                "retention_and_deletion_approved": True,
                "backup_restore_tested": True,
                "incident_response_runbook_approved": True,
                "freshness_latency_cost_slos_approved": True,
                "public_roo_isolation_verified": True,
            },
        }
        self.exit_policy = {
            "schema_version": 1,
            "organization_domain": self.organization.domain,
            "approval_status": "approved",
            "approved_at": (self.start_at - timedelta(days=1)).isoformat(),
            "review_due_at": (self.now + timedelta(days=10)).isoformat(),
            "pilot_approval_sha256": canonical_hash(self.approval),
            "approvers": {
                "review": "Review Owner",
                "security": "Security Owner",
                "operations": "Operations Owner",
            },
            "window": {
                "start_at": self.start_at.isoformat(),
                "end_at": self.end_at.isoformat(),
            },
            "rubric_version": "admin-roo-pilot-v1",
            "minimum_samples": {
                "pilot_days": 7,
                "audited_queries": 20,
                "answered_queries": 10,
                "abstained_queries": 5,
                "high_risk_citations": 10,
                "current_state_queries": 5,
                "temporal_queries": 5,
            },
            "thresholds": {
                "high_risk_citation_precision": 0.95,
                "current_state_accuracy": 0.85,
                "temporal_accuracy": 0.8,
                "abstention_accuracy": 0.85,
                "answer_faithfulness": 0.9,
                "max_query_failure_rate": 0.05,
                "max_p95_latency_ms": 500,
                "max_p95_total_tokens": 500,
                "max_daily_total_tokens": 5000,
            },
            "controls": {
                "manual_audit_double_checked": True,
                "incident_log_reconciled": True,
                "cost_measurement_approved": True,
            },
        }

    def audit_batch(self):
        audits = []
        for index, query in enumerate(self.queries):
            answered = query.status == MemoryQueryStatus.ANSWERED
            mode = query.query_plan["mode"]
            audits.append(
                {
                    "query_id": str(query.pk),
                    "idempotency_key": f"pilot-audit-{index:03d}",
                    "reviewed_at": (
                        self.end_at + timedelta(hours=1)
                    ).isoformat(),
                    "risk": "high" if index < 10 else "standard",
                    "answer_correct": True if answered else None,
                    "faithfulness_correct": True if answered else None,
                    "abstention_correct": True,
                    "current_state_correct": (
                        True
                        if mode == MemoryQueryMode.CURRENT_STATE.upper()
                        else None
                    ),
                    "temporal_correct": (
                        True
                        if mode
                        in {
                            MemoryQueryMode.HISTORICAL_AS_OF.upper(),
                            MemoryQueryMode.TIMELINE.upper(),
                        }
                        else None
                    ),
                    "correct_citation_count": 1 if answered else 0,
                    "permission_leak": False,
                    "public_admin_leak": False,
                }
            )
        return {
            "schema_version": 1,
            "organization_domain": self.organization.domain,
            "reviewer_email": self.reviewer.email,
            "rubric_version": "admin-roo-pilot-v1",
            "audits": audits,
        }

    def import_audits(self):
        return import_pilot_audit_batch(
            organization=self.organization,
            batch=self.audit_batch(),
            now=self.now,
        )

    def test_strict_policy_and_batch_validation_rejects_unsafe_inputs(self):
        batch = self.audit_batch()
        batch["audits"][0]["permission_leak"] = "no"
        self.assertIn(
            "audit_item_boolean_invalid",
            validate_pilot_audit_batch(
                batch,
                organization_domain=self.organization.domain,
                now=self.now,
            ),
        )
        policy = copy.deepcopy(self.exit_policy)
        policy["minimum_samples"]["audited_queries"] = 1
        policy["thresholds"]["high_risk_citation_precision"] = 0.5
        errors = validate_pilot_exit_policy(
            policy,
            organization_domain=self.organization.domain,
            now=self.now,
        )
        self.assertIn("exit_policy_minimum_samples_below_floor", errors)
        self.assertIn("exit_policy_quality_threshold_unsafe", errors)

    def test_import_is_atomic_idempotent_independent_and_immutable(self):
        result = self.import_audits()
        self.assertEqual(result["created"], 20)
        self.assertEqual(MemoryPilotQueryAudit.objects.count(), 20)
        repeated = self.import_audits()
        self.assertEqual(repeated["created"], 0)
        self.assertEqual(repeated["existing"], 20)

        audit = MemoryPilotQueryAudit.objects.first()
        audit.permission_leak = True
        with self.assertRaises(ValidationError):
            audit.save()

        conflict = self.audit_batch()
        conflict["audits"][0]["answer_correct"] = False
        with self.assertRaises(PilotEvidenceError):
            import_pilot_audit_batch(
                organization=self.organization,
                batch=conflict,
                now=self.now,
            )
        self.assertEqual(MemoryPilotQueryAudit.objects.count(), 20)

    def test_import_rejects_cross_org_query_and_self_review(self):
        other = Organization.objects.create(
            name="Other Evidence",
            domain="other-evidence.test",
        )
        batch = self.audit_batch()
        batch["audits"][0]["query_id"] = str(
            MemoryQueryLog.objects.create(
                organization=other,
                query="Other",
                query_hash="a" * 64,
                query_plan={"mode": "CURRENT_STATE"},
                status=MemoryQueryStatus.ABSTAINED,
                evidence_sufficiency=MemoryEvidenceSufficiency.INSUFFICIENT,
                selector_version="rules-v1",
            ).pk
        )
        with self.assertRaises(PilotEvidenceError):
            import_pilot_audit_batch(
                organization=self.organization,
                batch=batch,
                now=self.now,
            )
        self.assertEqual(MemoryPilotQueryAudit.objects.count(), 0)

        pilot_membership = OrganizationMembership.objects.get(
            organization=self.organization,
            user=self.pilot_user,
        )
        OrganizationCapabilityGrant.objects.create(
            membership=pilot_membership,
            capability=OrganizationCapability.objects.get(key="review_claims"),
            valid_from=self.start_at - timedelta(days=2),
        )
        self_batch = self.audit_batch()
        self_batch["reviewer_email"] = self.pilot_user.email
        with self.assertRaises(PilotEvidenceError):
            import_pilot_audit_batch(
                organization=self.organization,
                batch=self_batch,
                now=self.now,
            )
        self.assertEqual(MemoryPilotQueryAudit.objects.count(), 0)

    def test_green_report_measures_all_exit_gates_without_content(self):
        self.import_audits()
        report = build_pilot_evidence_report(
            organization=self.organization,
            approval_manifest=self.approval,
            exit_policy=self.exit_policy,
            now=self.now,
        )
        self.assertTrue(report["ready_to_exit"], report["blockers"])
        rendered = json.dumps(report, sort_keys=True)
        self.assertNotIn("Private pilot question", rendered)
        self.assertNotIn("sensitive-source-id", rendered)
        self.assertNotIn("UPILOT123", rendered)
        self.assertNotIn(self.reviewer.email, rendered)
        quality = next(
            item for item in report["checks"] if item["name"] == "quality_gates"
        )
        self.assertEqual(
            quality["metrics"]["high_risk_citation_precision"],
            1.0,
        )
        self.assertEqual(quality["metrics"]["temporal_accuracy"], 1.0)

    def test_report_blocks_leakage_scope_quality_and_operational_failures(self):
        self.import_audits()
        first_audit = MemoryPilotQueryAudit.objects.first()
        MemoryPilotQueryAudit.objects.filter(pk=first_audit.pk).update(
            permission_leak=True,
        )
        first_query = self.queries[0]
        MemoryQueryLog.objects.filter(pk=first_query.pk).update(
            channel_id="GUNAPPROVED",
        )
        report = build_pilot_evidence_report(
            organization=self.organization,
            approval_manifest=self.approval,
            exit_policy=self.exit_policy,
            now=self.now,
        )
        self.assertFalse(report["ready_to_exit"])
        self.assertIn("pilot_traffic_scope_violation", report["blockers"])
        self.assertIn("pilot_isolation_failure", report["blockers"])

        policy = copy.deepcopy(self.exit_policy)
        policy["pilot_approval_sha256"] = "f" * 64
        mismatched = build_pilot_evidence_report(
            organization=self.organization,
            approval_manifest=self.approval,
            exit_policy=policy,
            now=self.now,
        )
        self.assertIn("pilot_approval_binding_invalid", mismatched["blockers"])

    def test_commands_dry_run_apply_report_and_fail_after_json(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            audit_path = root / "audits.json"
            approval_path = root / "approval.json"
            policy_path = root / "policy.json"
            audit_path.write_text(json.dumps(self.audit_batch()), encoding="utf-8")
            approval_path.write_text(json.dumps(self.approval), encoding="utf-8")
            policy_path.write_text(json.dumps(self.exit_policy), encoding="utf-8")

            output = io.StringIO()
            call_command(
                "import_org_memory_pilot_audits",
                organization_domain=self.organization.domain,
                audit_batch=str(audit_path),
                stdout=output,
            )
            dry_run = json.loads(output.getvalue())
            self.assertFalse(dry_run["applied"])
            self.assertEqual(MemoryPilotQueryAudit.objects.count(), 0)
            self.assertNotIn(self.reviewer.email, output.getvalue())
            self.assertNotIn(str(self.queries[0].pk), output.getvalue())

            output = io.StringIO()
            call_command(
                "import_org_memory_pilot_audits",
                organization_domain=self.organization.domain,
                audit_batch=str(audit_path),
                apply=True,
                stdout=output,
            )
            self.assertTrue(json.loads(output.getvalue())["applied"])
            self.assertEqual(MemoryPilotQueryAudit.objects.count(), 20)

            output = io.StringIO()
            call_command(
                "evaluate_org_memory_pilot",
                organization_domain=self.organization.domain,
                approval_manifest=str(approval_path),
                exit_policy=str(policy_path),
                fail_on_blockers=True,
                stdout=output,
            )
            self.assertTrue(json.loads(output.getvalue())["ready_to_exit"])

            policy = copy.deepcopy(self.exit_policy)
            policy["approval_status"] = "draft"
            policy_path.write_text(json.dumps(policy), encoding="utf-8")
            output = io.StringIO()
            with self.assertRaises(CommandError):
                call_command(
                    "evaluate_org_memory_pilot",
                    organization_domain=self.organization.domain,
                    approval_manifest=str(approval_path),
                    exit_policy=str(policy_path),
                    fail_on_blockers=True,
                    stdout=output,
                )
            self.assertFalse(json.loads(output.getvalue())["ready_to_exit"])
