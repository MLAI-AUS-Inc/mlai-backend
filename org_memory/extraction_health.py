from __future__ import annotations

from typing import Optional

from django.db import transaction
from django.utils import timezone

from .extraction import ExtractionTarget, configured_extraction_target
from .extraction import schedule_source_extraction
from .models import (
    MemoryClaimStatus,
    MemoryDeadLetter,
    MemoryExtractionRun,
    MemoryExtractionStatus,
    MemorySourceLifecycle,
    MemorySourceVersion,
    MemoryWorkTaskType,
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


def legacy_extraction_dead_letters(
    *,
    organization,
    provider: str,
    superseded_schema_version: str,
):
    return MemoryDeadLetter.objects.filter(
        organization=organization,
        resolved_at__isnull=True,
        task_type=MemoryWorkTaskType.EXTRACT,
        work_item__provider=provider,
        work_item__action_request__isnull=True,
        work_item__source_version__isnull=False,
        payload_snapshot__schema_version=superseded_schema_version,
    ).select_related("work_item", "work_item__source_version")


@transaction.atomic
def reconcile_legacy_extraction_dead_letters(
    *,
    organization,
    provider: str,
    superseded_schema_version: str,
    apply: bool = False,
    resolved_by=None,
    limit: int = 1000,
    target: Optional[ExtractionTarget] = None,
) -> dict:
    """Supersede bounded legacy extraction failures with the current target."""

    target = target or configured_extraction_target()
    if superseded_schema_version == target.schema_version:
        raise ValueError(
            "The superseded schema version must differ from the current target."
        )
    if apply and resolved_by is None:
        raise ValueError("An operator is required when applying reconciliation.")
    dead_letters = legacy_extraction_dead_letters(
        organization=organization,
        provider=provider,
        superseded_schema_version=superseded_schema_version,
    ).order_by("dead_at", "pk")
    if apply:
        dead_letters = dead_letters.select_for_update()
    rows = list(dead_letters[:limit])
    report = {
        "schema_version": "org-memory-extraction-dead-letter-reconciliation-v1",
        "organization_domain": organization.domain,
        "provider": provider,
        "apply": bool(apply),
        "superseded_schema_version": superseded_schema_version,
        "target_schema_version": target.schema_version,
        "target_fingerprint": target.fingerprint,
        "candidates": len(rows),
        "scheduled": 0,
        "existing": 0,
        "skipped": 0,
        "resolved": 0,
    }
    if not apply:
        return report
    resolved_at = timezone.now()
    for dead_letter in rows:
        scheduled = schedule_source_extraction(
            source_version=dead_letter.work_item.source_version,
            target=target,
        )
        for key in ("scheduled", "existing", "skipped"):
            report[key] += int(scheduled.get(key) or 0)
        if scheduled.get("skipped"):
            continue
        dead_letter.resolved_at = resolved_at
        dead_letter.resolved_by = resolved_by
        dead_letter.requeued_work_item_id = scheduled.get("work_item_id")
        dead_letter.save(
            update_fields=(
                "resolved_at",
                "resolved_by",
                "requeued_work_item",
            )
        )
        report["resolved"] += 1
    return report
