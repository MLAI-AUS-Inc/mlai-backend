import hashlib
import os
from datetime import date, datetime, timedelta, timezone as datetime_timezone
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from integrations.models import ExternalServiceConnection
from organizations.models import Organization
from org_memory.assertions import actor_identity_headers, build_actor_assertion
from org_memory.consolidation import mark_stale_claims, transition_claim
from org_memory.kernel import (
    capture_source_version,
    open_review_item,
    revoke_source_access,
)
from org_memory.models import (
    MemoryClaim,
    MemoryClaimKind,
    MemoryClaimStatus,
    MemoryConnectionConfiguration,
    MemoryConnectionHealthSnapshot,
    MemoryConnectionHealthStatus,
    MemoryConnectionState,
    MemoryConsolidationOperation,
    MemoryConsolidationRun,
    MemoryConsolidationStatus,
    MemoryCurrentState,
    MemoryDailyReconciliationReport,
    MemoryDailyReconciliationStatus,
    MemoryDerivedArtifactStatus,
    MemoryDigest,
    MemoryDigestType,
    MemoryEntity,
    MemoryEntityType,
    MemoryEvidence,
    MemoryExtractionRun,
    MemoryExtractionStatus,
    MemoryProviderEnablement,
    MemoryPilotDeployment,
    MemoryPilotDeploymentState,
    MemoryReviewType,
    MemoryScopeStatus,
    MemorySourceActionRequest,
    MemorySourceScope,
    MemorySummary,
    MemorySummaryType,
    OrganizationCapability,
    OrganizationCapabilityGrant,
    OrganizationIdentity,
    OrganizationMembership,
    OrganizationRole,
    OrganizationRoleAssignment,
    OrganizationSlackWorkspace,
    ServicePrincipal,
)
from org_memory.pilot_deployment import approval_allowlist_hashes
from org_memory.review_summaries import (
    review_dashboard_snapshot,
    run_post_reconciliation_artifacts,
)
from org_memory.service_principals import issue_service_principal_credential


def digest(value):
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


@override_settings(
    ORG_MEMORY_QUERY_API_ENABLED=True,
    ORG_MEMORY_PILOT_ALLOWLIST_KEY_VERSION="test-v1",
    ORG_MEMORY_PILOT_ALLOWLIST_HMAC_KEY="r" * 32,
    ORG_MEMORY_SUMMARY_MAX_CLAIMS=100,
    ORG_MEMORY_DIGEST_MAX_ITEMS=25,
    ORG_MEMORY_WEEKLY_DIGEST_WEEKDAY=0,
)
class ReviewSummaryDigestTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.organization = Organization.objects.create(
            name="Review Brain",
            domain="review-brain.test",
        )
        self.other_organization = Organization.objects.create(
            name="Other Brain",
            domain="other-review-brain.test",
        )
        self.user = get_user_model().objects.create_user(
            email="reviewer@review-brain.test"
        )
        OrganizationSlackWorkspace.objects.create(
            organization=self.organization,
            slack_team_id="TREVIEW1",
        )
        OrganizationIdentity.objects.create(
            organization=self.organization,
            user=self.user,
            provider="slack",
            external_tenant_id="TREVIEW1",
            external_user_id="UREVIEW1",
            email_at_link_time=self.user.email,
            verified_at=timezone.now(),
        )
        membership = OrganizationMembership.objects.create(
            organization=self.organization,
            user=self.user,
        )
        role = OrganizationRole.objects.create(
            organization=self.organization,
            slug="brain-reviewer",
            name="Brain reviewer",
        )
        OrganizationRoleAssignment.objects.create(
            membership=membership,
            role=role,
        )
        for capability in ("view_general_memory", "review_claims", "manage_sources"):
            OrganizationCapabilityGrant.objects.create(
                role=role,
                capability=OrganizationCapability.objects.get(key=capability),
            )
        self.principal = ServicePrincipal.objects.create(
            name="review-summary-test",
            organization=self.organization,
            scopes=["org_memory.read", "source.manage"],
            allowed_surfaces=["admin_roo"],
        )
        self.credential, self.token = issue_service_principal_credential(
            self.principal
        )
        allowlist = approval_allowlist_hashes(
            self.organization,
            {
                "pilot_admin_refs": ["slack:UREVIEW1"],
                "allowed_slack_contexts": ["channel:GREVIEW1"],
            },
        )
        MemoryPilotDeployment.objects.create(
            organization=self.organization,
            state=MemoryPilotDeploymentState.ACTIVE,
            approval_manifest_hash="b" * 64,
            approval_review_due_at=timezone.now() + timedelta(days=30),
            allowlist_key_version=allowlist["key_version"],
            actor_ref_hashes=allowlist["actor_hashes"],
            context_ref_hashes=allowlist["context_hashes"],
            approved_provider_count=1,
            approved_source_scope_count=1,
            stage_idempotency_key="review-api-test-stage",
            activation_idempotency_key="review-api-test-activate",
            activated_at=timezone.now(),
        )
        self.connection = ExternalServiceConnection.objects.create(
            provider="linear",
            user=self.user,
            organization=self.organization,
            external_account_id="review-linear",
        )
        MemoryProviderEnablement.objects.create(
            organization=self.organization,
            provider="linear",
            is_enabled=True,
            approved_by=self.user,
            approved_at=timezone.now(),
        )
        self.configuration = MemoryConnectionConfiguration.objects.create(
            organization=self.organization,
            provider="linear",
            external_connection=self.connection,
            lifecycle_state=MemoryConnectionState.ACTIVE,
            created_by=self.user,
            last_successful_sync_at=timezone.now(),
        )
        self.scope = MemorySourceScope.objects.create(
            configuration=self.configuration,
            scope_type="project",
            external_id="project-1",
            name="Project One",
            selected=True,
            status=MemoryScopeStatus.SELECTED,
            default_classification="committee",
        )
        self.project = MemoryEntity.objects.create(
            organization=self.organization,
            entity_type=MemoryEntityType.PROJECT,
            canonical_name="Project One",
            normalized_name="project one",
            resolved_key="project:one",
            classification="committee",
        )
        self.report_date = date(2026, 7, 20)
        self.day_start = datetime(
            2026,
            7,
            20,
            tzinfo=datetime_timezone.utc,
        )
        self.request_number = 0

    def _headers(self, *, idempotency_key=None):
        self.request_number += 1
        request_id = f"review-summary-{self.request_number}"
        assertion = build_actor_assertion(
            self.token,
            credential_id=str(self.credential.pk),
            surface="admin_roo",
            slack_team_id="TREVIEW1",
            acting_slack_user_id="UREVIEW1",
            slack_channel_id="GREVIEW1",
            slack_thread_ts="1700000000.123",
            event_id=f"EvREVIEW{self.request_number}",
            request_id=request_id,
        )
        identity = actor_identity_headers(
            assertion=assertion,
            surface="admin_roo",
            slack_team_id="TREVIEW1",
            acting_slack_user_id="UREVIEW1",
            slack_channel_id="GREVIEW1",
            slack_thread_ts="1700000000.123",
            event_id=f"EvREVIEW{self.request_number}",
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

    def _claim(
        self,
        *,
        external_id,
        statement,
        kind,
        observed_at,
        source_type="linear_issue",
        subject_entity=None,
        classification="committee",
        predicate=None,
    ):
        source, version, _created = capture_source_version(
            organization=self.organization,
            provider="linear",
            external_account_id="review-linear",
            source_type=source_type,
            external_id=external_id,
            version_key="v1",
            content_hash=digest(statement),
            classification=classification,
            acl={
                "is_accessible": True,
                "provider_revision": f"acl-{external_id}",
                "principal_refs": ["group:committee"],
            },
            chunks=[
                {
                    "ordinal": 0,
                    "text": statement,
                    "occurred_at": observed_at,
                }
            ],
            title=f"Source {external_id}",
            occurred_at=observed_at,
            configuration=self.configuration,
            source_scope=self.scope,
        )
        run = MemoryExtractionRun.objects.create(
            organization=self.organization,
            source_version=version,
            idempotency_key=digest(f"run:{external_id}"),
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
            candidate_key=digest(f"candidate:{external_id}"),
            kind=kind,
            epistemic_type="observation",
            subject_entity=subject_entity,
            predicate=predicate or f"predicate_{external_id}",
            object_value=statement,
            statement=statement,
            normalized_key=digest(f"normalized:{external_id}"),
            status=MemoryClaimStatus.ACTIVE,
            classification=classification,
            confidence=Decimal("0.900"),
            importance=Decimal("0.800"),
            source_authority=Decimal("0.800"),
            observed_at=observed_at,
            valid_from=observed_at,
            recorded_at=observed_at,
            review_required=False,
            extractor_version="test-v1",
            extractor_model="deterministic-test",
            extractor_prompt_version="test-v1",
            extractor_schema_version="test-v1",
        )
        chunk = version.chunks.get()
        evidence = MemoryEvidence.objects.create(
            claim=claim,
            source=source,
            source_version=version,
            chunk=chunk,
            quote=statement,
            quote_start=0,
            quote_end=len(statement),
            quote_hash=digest(statement),
            source_locator={"external_id": external_id},
            evidence_confidence=Decimal("1.000"),
        )
        return claim, evidence, source

    def _report(self, *, healthy=True):
        report = MemoryDailyReconciliationReport.objects.create(
            organization=self.organization,
            report_date=self.report_date,
            time_zone="UTC",
            window_started_at=self.day_start,
            status=MemoryDailyReconciliationStatus.COMPLETED,
            completed_at=self.day_start + timedelta(hours=1),
        )
        MemoryConnectionHealthSnapshot.objects.create(
            report=report,
            organization=self.organization,
            configuration=self.configuration,
            provider="linear",
            health_status=(
                MemoryConnectionHealthStatus.HEALTHY
                if healthy
                else MemoryConnectionHealthStatus.STALE
            ),
            schedule_status="noop",
            credential_status="active",
            freshness_status="current" if healthy else "stale",
        )
        return report

    def test_successful_reconciliation_builds_hierarchical_lineage_once(self):
        open_claim, open_evidence, _source = self._claim(
            external_id="thread-open",
            statement="Project One needs an owner for the launch checklist.",
            kind=MemoryClaimKind.OPEN_LOOP,
            observed_at=self.day_start + timedelta(hours=2),
            source_type="slack_thread",
            subject_entity=self.project,
        )
        weekly_claim, weekly_evidence, _source = self._claim(
            external_id="weekly-decision",
            statement="The committee selected Adelaide for the pilot.",
            kind=MemoryClaimKind.DECISION,
            observed_at=self.day_start - timedelta(days=3),
            subject_entity=self.project,
        )
        report = self._report()

        first = run_post_reconciliation_artifacts(
            report=report,
            force_weekly=True,
        )
        counts = (MemorySummary.objects.count(), MemoryDigest.objects.count())
        second = run_post_reconciliation_artifacts(
            report=report,
            force_weekly=True,
        )

        self.assertEqual(first["status"], "completed")
        self.assertEqual(second["status"], "completed")
        self.assertEqual(
            set(MemorySummary.objects.values_list("summary_type", flat=True)),
            {
                MemorySummaryType.THREAD,
                MemorySummaryType.DAY,
                MemorySummaryType.WEEK,
                MemorySummaryType.PROJECT,
            },
        )
        self.assertEqual(
            counts,
            (MemorySummary.objects.count(), MemoryDigest.objects.count()),
        )
        day_summary = MemorySummary.objects.get(
            summary_type=MemorySummaryType.DAY
        )
        self.assertEqual(
            set(day_summary.claim_links.values_list("claim_id", flat=True)),
            {open_claim.pk},
        )
        self.assertEqual(
            set(day_summary.evidence_links.values_list("evidence_id", flat=True)),
            {open_evidence.pk},
        )
        daily = MemoryDigest.objects.get(
            digest_type=MemoryDigestType.DAILY_OPEN_LOOPS
        )
        weekly = MemoryDigest.objects.get(
            digest_type=MemoryDigestType.WEEKLY_COMMITTEE
        )
        self.assertEqual(
            set(daily.items.values_list("claim_id", flat=True)),
            {open_claim.pk},
        )
        self.assertEqual(
            set(
                daily.items.get().evidence_links.values_list(
                    "evidence_id",
                    flat=True,
                )
            ),
            {open_evidence.pk},
        )
        self.assertEqual(
            set(weekly.items.values_list("claim_id", flat=True)),
            {weekly_claim.pk},
        )
        self.assertEqual(
            set(
                weekly.items.get().evidence_links.values_list(
                    "evidence_id",
                    flat=True,
                )
            ),
            {weekly_evidence.pk},
        )

    def test_unhealthy_snapshot_blocks_digest_without_source_content(self):
        claim, _evidence, _source = self._claim(
            external_id="restricted-open",
            statement="Restricted source says the secret launch plan is delayed.",
            kind=MemoryClaimKind.OPEN_LOOP,
            observed_at=self.day_start + timedelta(hours=1),
            subject_entity=self.project,
        )
        report = self._report(healthy=False)

        result = run_post_reconciliation_artifacts(
            report=report,
            force_weekly=True,
        )

        self.assertEqual(result["status"], "blocked")
        self.assertFalse(MemorySummary.objects.exists())
        for digest_row in MemoryDigest.objects.all():
            self.assertEqual(
                digest_row.status,
                MemoryDerivedArtifactStatus.BLOCKED,
            )
            self.assertIn("linear", digest_row.body)
            self.assertNotIn(claim.statement, digest_row.body)
            self.assertFalse(digest_row.items.exists())

    def test_source_access_revocation_invalidates_every_derived_surface(self):
        _claim, _evidence, source = self._claim(
            external_id="revoked-open",
            statement="Project One needs committee approval.",
            kind=MemoryClaimKind.OPEN_LOOP,
            observed_at=self.day_start + timedelta(hours=1),
            source_type="slack_thread",
            subject_entity=self.project,
        )
        report = self._report()
        run_post_reconciliation_artifacts(report=report)

        revoke_source_access(source, reason="scope_removed")

        self.assertFalse(
            MemorySummary.objects.filter(is_current=True).exists()
        )
        self.assertFalse(
            MemorySummary.objects.exclude(
                status=MemoryDerivedArtifactStatus.STALE
            ).exists()
        )
        digest_row = MemoryDigest.objects.get(
            digest_type=MemoryDigestType.DAILY_OPEN_LOOPS
        )
        self.assertEqual(
            digest_row.status,
            MemoryDerivedArtifactStatus.BLOCKED,
        )
        self.assertEqual(
            digest_row.warnings[0]["code"],
            "source_access_changed",
        )

    def test_claim_state_change_invalidates_derived_text_immediately(self):
        claim, _evidence, _source = self._claim(
            external_id="retracted-open",
            statement="Project One needs committee approval.",
            kind=MemoryClaimKind.OPEN_LOOP,
            observed_at=self.day_start + timedelta(hours=1),
            source_type="slack_thread",
            subject_entity=self.project,
        )
        run_post_reconciliation_artifacts(report=self._report())

        transition_claim(
            claim=claim,
            to_status=MemoryClaimStatus.RETRACTED,
            reason="reviewed_retraction",
            actor=self.user,
        )

        self.assertFalse(
            MemorySummary.objects.filter(is_current=True).exists()
        )
        digest_row = MemoryDigest.objects.get(
            digest_type=MemoryDigestType.DAILY_OPEN_LOOPS
        )
        self.assertEqual(
            digest_row.status,
            MemoryDerivedArtifactStatus.BLOCKED,
        )
        self.assertNotIn(claim.statement, digest_row.body)
        self.assertEqual(
            digest_row.warnings,
            [{"code": "claim_state_changed"}],
        )

    def test_review_dashboard_api_evidence_and_scoped_reprocess(self):
        claim, evidence, source = self._claim(
            external_id="review-open",
            statement="Project One launch owner is still unresolved.",
            kind=MemoryClaimKind.OPEN_LOOP,
            observed_at=self.day_start + timedelta(hours=1),
            subject_entity=self.project,
        )
        review, _created = open_review_item(
            organization=self.organization,
            target=claim,
            review_type=MemoryReviewType.CONTRADICTION,
            reason="Resolve the conflicting owner.",
            idempotency_key="review-owner-conflict",
        )
        report = self._report()
        run_post_reconciliation_artifacts(report=report)

        dashboard = self.client.get(
            "/api/v1/org-memory/review-dashboard",
            **self._headers(),
        )
        detail = self.client.get(
            f"/api/v1/org-memory/reviews/{review.pk}",
            **self._headers(),
        )
        summaries = self.client.get(
            "/api/v1/org-memory/summaries",
            **self._headers(),
        )
        digest_rows = self.client.get(
            "/api/v1/org-memory/digests",
            **self._headers(),
        )
        with patch.dict(
            os.environ,
            {"ORG_MEMORY_ENABLED_PROVIDERS": "linear"},
        ):
            reprocess = self.client.post(
                f"/api/v1/org-memory/reviews/{review.pk}/reprocess",
                {"confirm": True, "source_id": str(source.pk)},
                format="json",
                **self._headers(idempotency_key="review-reprocess-1"),
            )
            replay = self.client.post(
                f"/api/v1/org-memory/reviews/{review.pk}/reprocess",
                {"confirm": True, "source_id": str(source.pk)},
                format="json",
                **self._headers(idempotency_key="review-reprocess-1"),
            )

        self.assertEqual(dashboard.status_code, 200, dashboard.data)
        self.assertEqual(
            dashboard.data["queues"]["contradiction"]["count"],
            1,
        )
        self.assertEqual(detail.status_code, 200, detail.data)
        self.assertEqual(
            detail.data["evidence"][0]["evidence_id"],
            str(evidence.pk),
        )
        self.assertEqual(summaries.status_code, 200, summaries.data)
        self.assertTrue(summaries.data["summaries"])
        self.assertEqual(digest_rows.status_code, 200, digest_rows.data)
        self.assertEqual(reprocess.status_code, 202, reprocess.data)
        self.assertTrue(reprocess.data["created"])
        self.assertFalse(replay.data["created"])
        self.assertEqual(
            MemorySourceActionRequest.objects.filter(action="reprocess").count(),
            1,
        )

        snapshot = review_dashboard_snapshot(organization=self.organization)
        self.assertEqual(snapshot["total_open"], 1)

    def test_review_and_derived_apis_hide_ungranted_finance_content(self):
        claim, _evidence, _source = self._claim(
            external_id="finance-open",
            statement="Confidential MRR exception requires review.",
            kind=MemoryClaimKind.OPEN_LOOP,
            observed_at=self.day_start + timedelta(hours=1),
            subject_entity=self.project,
            classification="finance",
        )
        review, _created = open_review_item(
            organization=self.organization,
            target=claim,
            review_type=MemoryReviewType.SENSITIVITY,
            reason="Review the confidential MRR exception.",
            idempotency_key="review-finance-classification",
        )
        run_post_reconciliation_artifacts(report=self._report())

        detail = self.client.get(
            f"/api/v1/org-memory/reviews/{review.pk}",
            **self._headers(),
        )
        summaries = self.client.get(
            "/api/v1/org-memory/summaries",
            **self._headers(),
        )
        digests = self.client.get(
            "/api/v1/org-memory/digests",
            **self._headers(),
        )

        self.assertEqual(detail.status_code, 200, detail.data)
        self.assertEqual(detail.data["reason"], "Restricted review context.")
        self.assertNotIn("statement", detail.data["target"])
        self.assertEqual(detail.data["evidence"], [])
        self.assertEqual(summaries.data["summaries"], [])
        self.assertFalse(
            any(
                row["digest_type"] == MemoryDigestType.DAILY_OPEN_LOOPS
                for row in digests.data["digests"]
            )
        )

    def test_scheduler_backfills_stale_review_queue_idempotently(self):
        claim, _evidence, _source = self._claim(
            external_id="already-stale",
            statement="Project One status needs fresh confirmation.",
            kind=MemoryClaimKind.PROJECT_STATUS,
            observed_at=self.day_start - timedelta(days=60),
            subject_entity=self.project,
        )
        claim.status = MemoryClaimStatus.STALE
        claim.stale_after = self.day_start - timedelta(days=30)
        claim.save(update_fields=("status", "stale_after", "updated_at"))

        first = mark_stale_claims(
            organization=self.organization,
            at=self.day_start,
        )
        second = mark_stale_claims(
            organization=self.organization,
            at=self.day_start,
        )

        self.assertEqual(first["marked_stale"], 0)
        self.assertEqual(first["stale_reviews_ensured"], 1)
        self.assertEqual(second["stale_reviews_ensured"], 1)
        self.assertEqual(
            review_dashboard_snapshot(organization=self.organization)["queues"][
                "stale"
            ]["count"],
            1,
        )

    def test_resolve_api_applies_contradiction_and_replays_idempotently(self):
        established, _evidence, _source = self._claim(
            external_id="owner-established",
            statement="The committee selected Alex as launch owner.",
            kind=MemoryClaimKind.DECISION,
            observed_at=self.day_start,
            subject_entity=self.project,
            predicate="launch_owner",
        )
        candidate, _evidence, _source = self._claim(
            external_id="owner-conflict",
            statement="The committee selected Bailey as launch owner.",
            kind=MemoryClaimKind.DECISION,
            observed_at=self.day_start + timedelta(hours=1),
            subject_entity=self.project,
            predicate="launch_owner",
        )
        candidate.status = MemoryClaimStatus.CANDIDATE
        candidate.save(update_fields=("status", "updated_at"))
        run = MemoryConsolidationRun.objects.create(
            organization=self.organization,
            candidate_claim=candidate,
            matched_claim=established,
            idempotency_key=digest("owner-contradiction-run"),
            operation=MemoryConsolidationOperation.CONTRADICTS,
            status=MemoryConsolidationStatus.REVIEW_REQUIRED,
            confidence=Decimal("0.900"),
            reason="Conflicting launch owners.",
            deterministic=True,
            consolidator_version="test-v1",
            schema_version="test-v1",
            prompt_version="test-v1",
            model="deterministic-test",
            prompt_input_hash=digest("owner-input"),
            output_hash=digest("owner-output"),
        )
        review, _created = open_review_item(
            organization=self.organization,
            target=run,
            review_type=MemoryReviewType.CONTRADICTION,
            reason="Choose the evidenced launch owner.",
            idempotency_key="review-owner-resolution",
        )
        run.review_item = review
        run.save(update_fields=("review_item",))

        resolved = self.client.post(
            f"/api/v1/org-memory/review-items/{review.pk}/resolve",
            {
                "confirm": True,
                "decision": "approve",
                "winner_claim_id": str(established.pk),
                "reason": "The earlier committee decision remains authoritative.",
            },
            format="json",
            **self._headers(idempotency_key="resolve-owner-1"),
        )
        replay = self.client.post(
            f"/api/v1/org-memory/review-items/{review.pk}/resolve",
            {
                "confirm": True,
                "decision": "approve",
                "winner_claim_id": str(established.pk),
            },
            format="json",
            **self._headers(idempotency_key="resolve-owner-1"),
        )

        established.refresh_from_db()
        candidate.refresh_from_db()
        review.refresh_from_db()
        self.assertEqual(resolved.status_code, 200, resolved.data)
        self.assertTrue(resolved.data["created"])
        self.assertFalse(replay.data["created"])
        self.assertEqual(review.status, "approved")
        self.assertEqual(established.status, MemoryClaimStatus.ACTIVE)
        self.assertEqual(candidate.status, MemoryClaimStatus.CONTRADICTED)
        self.assertEqual(
            MemoryCurrentState.objects.get(
                organization=self.organization,
                state_key="decision:launch_owner",
            ).claim_id,
            established.pk,
        )
