from __future__ import annotations

from typing import Optional

from django.db import connection, transaction
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
    MemoryWorkItem,
    MemoryWorkerLease,
    MemoryWorkStatus,
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


def superseded_extraction_work_items(
    *,
    organization,
    provider: str,
    target: Optional[ExtractionTarget] = None,
):
    """Return queued/in-flight extraction work that is not for the current target."""

    target = target or configured_extraction_target()
    return MemoryWorkItem.objects.filter(
        organization=organization,
        provider=provider,
        task_type=MemoryWorkTaskType.EXTRACT,
        action_request__isnull=True,
        status__in=(
            MemoryWorkStatus.PENDING,
            MemoryWorkStatus.FAILED,
            MemoryWorkStatus.PROCESSING,
        ),
    ).exclude(
        payload__model=target.model,
        payload__extractor_version=target.extractor_version,
        payload__schema_version=target.schema_version,
        payload__prompt_version=target.prompt_version,
        payload__target_fingerprint=target.fingerprint,
    )


@transaction.atomic
def cancel_superseded_extraction_work(
    *,
    organization,
    provider: str,
    apply: bool = False,
    resolved_by=None,
    limit: int = 1000,
    target: Optional[ExtractionTarget] = None,
) -> dict:
    """Cancel bounded stale-target work before scheduling the reviewed target."""

    target = target or configured_extraction_target()
    if apply and resolved_by is None:
        raise ValueError("An operator is required when applying reconciliation.")
    work_items = superseded_extraction_work_items(
        organization=organization,
        provider=provider,
        target=target,
    ).order_by("created_at", "pk")
    if apply:
        work_items = work_items.select_for_update()
    rows = list(work_items[:limit])
    report = {
        "schema_version": "org-memory-superseded-extraction-work-cancellation-v1",
        "organization_domain": organization.domain,
        "provider": provider,
        "apply": bool(apply),
        "candidates": len(rows),
        "cancelled": 0,
        "leases_released": 0,
        "cost_reservations_released": 0,
        "target_fingerprint": target.fingerprint,
    }
    if not apply or not rows:
        return report
    now = timezone.now()
    work_ids = [row.pk for row in rows]
    report["leases_released"] = MemoryWorkerLease.objects.filter(
        work_item_id__in=work_ids,
        released_at__isnull=True,
    ).update(released_at=now)
    from .cost_control import release_cost_reservations

    report["cost_reservations_released"] = release_cost_reservations(
        work_ids,
        now=now,
    )
    report["cancelled"] = MemoryWorkItem.objects.filter(
        pk__in=work_ids,
        status__in=(
            MemoryWorkStatus.PENDING,
            MemoryWorkStatus.FAILED,
            MemoryWorkStatus.PROCESSING,
        ),
    ).update(
        status=MemoryWorkStatus.CANCELLED,
        completed_at=now,
        locked_at=None,
        last_error="superseded_extraction_target",
        updated_at=now,
    )
    return report


def superseded_extraction_dead_letters(
    *,
    organization,
    provider: str,
    superseded_schema_version: Optional[str] = None,
    superseded_extractor_version: Optional[str] = None,
    superseded_prompt_version: Optional[str] = None,
):
    filters = {
        "organization": organization,
        "resolved_at__isnull": True,
        "task_type": MemoryWorkTaskType.EXTRACT,
        "work_item__provider": provider,
        "work_item__action_request__isnull": True,
        "work_item__source_version__isnull": False,
    }
    if superseded_schema_version:
        filters["payload_snapshot__schema_version"] = superseded_schema_version
    if superseded_extractor_version:
        filters["payload_snapshot__extractor_version"] = superseded_extractor_version
    if superseded_prompt_version:
        filters["payload_snapshot__prompt_version"] = superseded_prompt_version
    return MemoryDeadLetter.objects.filter(
        **filters,
    ).select_related("work_item", "work_item__source_version")


# Backwards-compatible name for callers that still reconcile by schema alone.
legacy_extraction_dead_letters = superseded_extraction_dead_letters


@transaction.atomic
def reconcile_legacy_extraction_dead_letters(
    *,
    organization,
    provider: str,
    superseded_schema_version: Optional[str] = None,
    superseded_extractor_version: Optional[str] = None,
    superseded_prompt_version: Optional[str] = None,
    apply: bool = False,
    resolved_by=None,
    limit: int = 1000,
    target: Optional[ExtractionTarget] = None,
) -> dict:
    """Supersede bounded legacy extraction failures with the current target."""

    target = target or configured_extraction_target()
    superseded_target = {
        "schema_version": str(superseded_schema_version or "").strip(),
        "extractor_version": str(superseded_extractor_version or "").strip(),
        "prompt_version": str(superseded_prompt_version or "").strip(),
    }
    superseded_target = {key: value for key, value in superseded_target.items() if value}
    if not superseded_target:
        raise ValueError("At least one superseded extraction target version is required.")
    current_target = {
        "schema_version": target.schema_version,
        "extractor_version": target.extractor_version,
        "prompt_version": target.prompt_version,
    }
    if all(current_target[key] == value for key, value in superseded_target.items()):
        raise ValueError(
            "The superseded extraction target must differ from the current target."
        )
    if apply and resolved_by is None:
        raise ValueError("An operator is required when applying reconciliation.")
    dead_letters = superseded_extraction_dead_letters(
        organization=organization,
        provider=provider,
        superseded_schema_version=superseded_schema_version,
        superseded_extractor_version=superseded_extractor_version,
        superseded_prompt_version=superseded_prompt_version,
    ).order_by("dead_at", "pk")
    if apply:
        lock_options = {}
        if connection.features.has_select_for_update_of:
            lock_options["of"] = ("self",)
        # This queryset also follows nullable relationships. On PostgreSQL,
        # constrain the row lock to the dead-letter evidence table itself.
        dead_letters = dead_letters.select_for_update(**lock_options)
    rows = list(dead_letters[:limit])
    report = {
        "schema_version": "org-memory-extraction-dead-letter-reconciliation-v1",
        "organization_domain": organization.domain,
        "provider": provider,
        "apply": bool(apply),
        "superseded_target": superseded_target,
        "target": current_target,
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
