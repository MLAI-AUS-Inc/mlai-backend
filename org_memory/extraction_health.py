from __future__ import annotations

from typing import Optional

from .extraction import ExtractionTarget, configured_extraction_target
from .models import (
    MemoryClaimStatus,
    MemoryExtractionRun,
    MemoryExtractionStatus,
    MemorySourceLifecycle,
    MemorySourceVersion,
)


def eligible_source_versions(*, organization, provider: Optional[str] = None):
    versions = MemorySourceVersion.objects.filter(
        source__organization=organization,
        source__lifecycle_state=MemorySourceLifecycle.ACTIVE,
        source__access_revoked_at__isnull=True,
        is_current=True,
        tombstoned_at__isnull=True,
        acl_snapshot__is_accessible=True,
        acl_snapshot__revoked_at__isnull=True,
        chunks__active_for_retrieval=True,
    ).exclude(classification="no_agent")
    if provider:
        versions = versions.filter(source__provider=provider)
    return versions.distinct()


def extraction_health_report(
    *,
    organization,
    provider: Optional[str] = None,
    target: Optional[ExtractionTarget] = None,
) -> dict:
    """Return a content-free gate for the currently configured extraction target."""

    target = target or configured_extraction_target()
    versions = eligible_source_versions(
        organization=organization,
        provider=provider,
    )
    version_ids = versions.values_list("pk", flat=True)
    runs = MemoryExtractionRun.objects.filter(
        organization=organization,
        source_version_id__in=version_ids,
        model=target.model,
        extractor_version=target.extractor_version,
        schema_version=target.schema_version,
        prompt_version=target.prompt_version,
    )
    processed = runs.values("source_version_id").distinct().count()
    quarantined = runs.filter(status=MemoryExtractionStatus.QUARANTINED).count()
    rejected = runs.filter(status=MemoryExtractionStatus.REJECTED).count()
    extracted_claims = (
        runs.filter(claims__isnull=False)
        .values("claims__id")
        .distinct()
        .count()
    )
    queryable_claims = (
        runs.filter(
            claims__status__in=(MemoryClaimStatus.ACTIVE, MemoryClaimStatus.STALE)
        )
        .values("claims__id")
        .distinct()
        .count()
    )
    eligible = versions.count()
    blockers = []
    if not eligible:
        blockers.append("eligible_source_versions_missing")
    if processed != eligible:
        blockers.append("extraction_coverage_incomplete")
    if quarantined:
        blockers.append("extraction_quarantine_present")
    if rejected:
        blockers.append("extraction_rejection_present")
    if eligible and not extracted_claims:
        blockers.append("extracted_claims_missing")
    if eligible and not queryable_claims:
        blockers.append("queryable_claims_missing")
    return {
        "schema_version": "org-memory-extraction-health-v1",
        "organization_domain": organization.domain,
        "provider": provider or "all",
        "ready": not blockers,
        "blockers": sorted(set(blockers)),
        "target": {
            "model": target.model,
            "extractor_version": target.extractor_version,
            "schema_version": target.schema_version,
            "prompt_version": target.prompt_version,
            "fingerprint": target.fingerprint,
        },
        "metrics": {
            "eligible_source_versions": eligible,
            "processed_source_versions": processed,
            "quarantined_runs": quarantined,
            "rejected_runs": rejected,
            "extracted_claims": extracted_claims,
            "queryable_claims": queryable_claims,
        },
    }
