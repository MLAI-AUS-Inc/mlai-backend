from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .models import (
    MemoryClaim,
    MemoryClaimKind,
    MemoryClaimStatus,
    MemoryClassification,
    MemoryConnectionHealthStatus,
    MemoryDerivedArtifactStatus,
    MemoryDigest,
    MemoryDigestItem,
    MemoryDigestItemEvidence,
    MemoryDigestType,
    MemoryDailyReconciliationReport,
    MemoryDailyReconciliationStatus,
    MemoryEntityType,
    MemoryEvidence,
    MemoryReviewItem,
    MemoryReviewStatus,
    MemoryReviewType,
    MemorySourceLifecycle,
    MemorySummary,
    MemorySummaryClaim,
    MemorySummaryEvidence,
    MemorySummaryType,
)


OPEN_REVIEW_STATUSES = (
    MemoryReviewStatus.OPEN,
    MemoryReviewStatus.IN_REVIEW,
)
REVIEW_QUEUE_TYPES = {
    "contradiction": MemoryReviewType.CONTRADICTION,
    "correction": MemoryReviewType.CORRECTION,
    "entity": MemoryReviewType.ENTITY_MERGE,
    "sensitivity": MemoryReviewType.SENSITIVITY,
    "stale": MemoryReviewType.STALE,
    "publication": MemoryReviewType.PUBLICATION,
}
DAILY_OPEN_LOOP_KINDS = (
    MemoryClaimKind.COMMITMENT,
    MemoryClaimKind.TASK,
    MemoryClaimKind.OPEN_LOOP,
    MemoryClaimKind.QUESTION,
    MemoryClaimKind.RISK,
)
WEEKLY_COMMITTEE_KINDS = (
    MemoryClaimKind.DECISION,
    MemoryClaimKind.COMMITMENT,
    MemoryClaimKind.PROJECT_STATUS,
    MemoryClaimKind.RISK,
    MemoryClaimKind.OPPORTUNITY,
    MemoryClaimKind.METRIC,
    MemoryClaimKind.EVENT,
)


def _bounded_positive_setting(name: str, default: int, maximum: int) -> int:
    try:
        value = int(getattr(settings, name, default))
    except (TypeError, ValueError):
        value = default
    return min(max(value, 1), maximum)


def review_dashboard_snapshot(*, organization, now=None) -> dict:
    now = now or timezone.now()
    reviews = MemoryReviewItem.objects.filter(
        organization=organization,
        status__in=OPEN_REVIEW_STATUSES,
    )
    queues = {}
    for queue_name, review_type in REVIEW_QUEUE_TYPES.items():
        rows = reviews.filter(review_type=review_type)
        oldest = rows.order_by("created_at").first()
        queues[queue_name] = {
            "review_type": review_type,
            "count": rows.count(),
            "high_priority": rows.filter(severity__in=("high", "critical")).count(),
            "overdue": rows.filter(due_at__lt=now).count(),
            "oldest_at": oldest.created_at if oldest else None,
        }

    latest_by_configuration = {}
    snapshots = (
        organization.memory_connection_health_snapshots.select_related(
            "configuration",
            "report",
        )
        .order_by("configuration_id", "-report__report_date", "-updated_at")
    )
    for snapshot in snapshots:
        latest_by_configuration.setdefault(snapshot.configuration_id, snapshot)
    connectors = [
        {
            "configuration_id": str(snapshot.configuration_id),
            "provider": snapshot.provider,
            "health_status": snapshot.health_status,
            "freshness_status": snapshot.freshness_status,
            "schedule_status": snapshot.schedule_status,
            "operator_action": snapshot.operator_action,
            "report_date": snapshot.report.report_date,
        }
        for snapshot in latest_by_configuration.values()
        if snapshot.health_status != MemoryConnectionHealthStatus.HEALTHY
    ]
    return {
        "queues": queues,
        "total_open": reviews.count(),
        "unhealthy_connectors": sorted(
            connectors,
            key=lambda row: (row["provider"], row["configuration_id"]),
        ),
    }


def open_stale_review(claim) -> MemoryReviewItem:
    review, _created = MemoryReviewItem.objects.get_or_create(
        organization=claim.organization,
        idempotency_key=f"stale-claim:{claim.pk}:{claim.stale_after.isoformat()}",
        defaults={
            "review_type": MemoryReviewType.STALE,
            "target_content_type": _content_type(claim),
            "target_object_id": str(claim.pk),
            "severity": "high"
            if claim.kind
            in {
                MemoryClaimKind.DECISION,
                MemoryClaimKind.COMMITMENT,
                MemoryClaimKind.POLICY,
                MemoryClaimKind.METRIC,
            }
            else "normal",
            "reason": "Review this stale claim and refresh, supersede, or retract it.",
            "due_at": claim.stale_after,
        },
    )
    return review


def _content_type(instance):
    from django.contrib.contenttypes.models import ContentType

    return ContentType.objects.get_for_model(instance, for_concrete_model=False)


def eligible_evidence_queryset(*, organization):
    return MemoryEvidence.objects.filter(
        claim__organization=organization,
        claim__classification__in=tuple(
            value
            for value in MemoryClassification.values
            if value != MemoryClassification.NO_AGENT
        ),
        source__lifecycle_state=MemorySourceLifecycle.ACTIVE,
        source__access_revoked_at__isnull=True,
        source_version__tombstoned_at__isnull=True,
        source_version__acl_snapshot__is_accessible=True,
        source_version__acl_snapshot__revoked_at__isnull=True,
    ).exclude(
        source_version__classification=MemoryClassification.NO_AGENT,
    ).exclude(
        chunk__classification=MemoryClassification.NO_AGENT,
    )


def eligible_claims_queryset(*, organization):
    return (
        MemoryClaim.objects.filter(
            organization=organization,
            status=MemoryClaimStatus.ACTIVE,
            evidence__in=eligible_evidence_queryset(organization=organization),
        )
        .exclude(classification="no_agent")
        .distinct()
    )


def _claim_time_filter(window_start, window_end):
    return (
        Q(observed_at__gte=window_start, observed_at__lt=window_end)
        | Q(
            observed_at__isnull=True,
            recorded_at__gte=window_start,
            recorded_at__lt=window_end,
        )
    )


def _window(report):
    target_timezone = ZoneInfo(report.time_zone)
    local_start = datetime.combine(report.report_date, time.min, target_timezone)
    local_end = local_start + timedelta(days=1)
    return (
        local_start.astimezone(timezone.utc),
        local_end.astimezone(timezone.utc),
    )


def _week_to_date_window(report):
    target_timezone = ZoneInfo(report.time_zone)
    week_start_date = report.report_date - timedelta(days=report.report_date.weekday())
    local_start = datetime.combine(week_start_date, time.min, target_timezone)
    _day_start, day_end = _window(report)
    return local_start.astimezone(timezone.utc), day_end


def _previous_week_window(report):
    target_timezone = ZoneInfo(report.time_zone)
    this_week_start = report.report_date - timedelta(days=report.report_date.weekday())
    local_end = datetime.combine(this_week_start, time.min, target_timezone)
    local_start = local_end - timedelta(days=7)
    return (
        local_start.astimezone(timezone.utc),
        local_end.astimezone(timezone.utc),
    )


def _fingerprint(values) -> str:
    encoded = json.dumps(values, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _classifications(claims, evidence_by_claim=None, extra=None) -> list[str]:
    values = {str(claim.classification) for claim in claims}
    for evidence in (
        item
        for items in (evidence_by_claim or {}).values()
        for item in items
    ):
        values.add(str(evidence.source_version.classification))
    values.update(str(value) for value in (extra or ()) if value)
    values.discard(MemoryClassification.NO_AGENT)
    return sorted(values)


def _evidence_for_claims(claims):
    claim_ids = [claim.pk for claim in claims]
    evidence = (
        eligible_evidence_queryset(organization=claims[0].organization)
        .filter(claim_id__in=claim_ids)
        .select_related("claim", "source", "source_version", "chunk")
        .order_by("claim_id", "source_id", "created_at")
    )
    grouped = defaultdict(list)
    for item in evidence:
        grouped[item.claim_id].append(item)
    return grouped


@transaction.atomic
def _upsert_summary(
    *,
    report,
    summary_type,
    subject_key,
    title,
    claims,
    window_start,
    window_end,
    parent=None,
    extra_classifications=None,
):
    claims = list(claims)[
        : _bounded_positive_setting("ORG_MEMORY_SUMMARY_MAX_CLAIMS", 100, 500)
    ]
    evidence_by_claim = _evidence_for_claims(claims) if claims else {}
    lineage = [
        {
            "claim_id": str(claim.pk),
            "evidence_ids": [
                str(item.pk) for item in evidence_by_claim.get(claim.pk, ())
            ],
        }
        for claim in claims
    ]
    generation_key = _fingerprint(
        [
            summary_type,
            subject_key,
            window_start.isoformat(),
            window_end.isoformat(),
            str(report.pk),
        ]
    )
    fingerprint = _fingerprint(lineage)
    status = (
        MemoryDerivedArtifactStatus.READY
        if claims
        else MemoryDerivedArtifactStatus.EMPTY
    )
    body = "\n".join(f"- {claim.statement}" for claim in claims)
    source_ids = {
        str(item.source_id)
        for items in evidence_by_claim.values()
        for item in items
    }
    summary, _created = MemorySummary.objects.update_or_create(
        organization=report.organization,
        generation_key=generation_key,
        defaults={
            "summary_type": summary_type,
            "subject_key": subject_key,
            "title": title[:512],
            "body": body,
            "structured_data": {
                "claim_count": len(claims),
                "evidence_count": sum(len(items) for items in evidence_by_claim.values()),
                "source_count": len(source_ids),
            },
            "required_classifications": _classifications(
                claims,
                evidence_by_claim,
                extra_classifications,
            ),
            "window_start": window_start,
            "window_end": window_end,
            "fingerprint": fingerprint,
            "status": status,
            "source_report": report,
            "parent": parent,
            "is_current": True,
            "invalidated_at": None,
        },
    )
    MemorySummary.objects.filter(
        organization=report.organization,
        summary_type=summary_type,
        subject_key=subject_key,
        is_current=True,
    ).exclude(pk=summary.pk).update(
        is_current=False,
        status=MemoryDerivedArtifactStatus.STALE,
        invalidated_at=timezone.now(),
        updated_at=timezone.now(),
    )
    summary.claim_links.all().delete()
    summary.evidence_links.all().delete()
    MemorySummaryClaim.objects.bulk_create(
        [
            MemorySummaryClaim(summary=summary, claim=claim, ordinal=ordinal)
            for ordinal, claim in enumerate(claims)
        ]
    )
    MemorySummaryEvidence.objects.bulk_create(
        [
            MemorySummaryEvidence(summary=summary, evidence=evidence)
            for claim in claims
            for evidence in evidence_by_claim.get(claim.pk, ())
        ]
    )
    return summary


def generate_summaries(*, report) -> list[MemorySummary]:
    if not reconciliation_succeeded(report):
        return []
    organization = report.organization
    day_start, day_end = _window(report)
    week_start, week_end = _week_to_date_window(report)
    base = eligible_claims_queryset(organization=organization).select_related(
        "organization",
        "subject_entity",
    )
    ordered = base.order_by("-importance", "-recorded_at", "pk")
    day_claims = list(ordered.filter(_claim_time_filter(day_start, day_end)))
    week_claims = list(ordered.filter(_claim_time_filter(week_start, week_end)))
    day_summary = _upsert_summary(
        report=report,
        summary_type=MemorySummaryType.DAY,
        subject_key=report.report_date.isoformat(),
        title=f"Daily memory summary — {report.report_date.isoformat()}",
        claims=day_claims,
        window_start=day_start,
        window_end=day_end,
    )
    summaries = [day_summary]
    summaries.append(
        _upsert_summary(
            report=report,
            summary_type=MemorySummaryType.WEEK,
            subject_key=week_start.date().isoformat(),
            title=(
                f"Week-to-date memory summary — {week_start.date().isoformat()}"
            ),
            claims=week_claims,
            window_start=week_start,
            window_end=week_end,
        )
    )

    project_entities = (
        organization.memory_entities.filter(
            entity_type=MemoryEntityType.PROJECT,
            merged_into__isnull=True,
            subject_claims__in=base,
        )
        .distinct()
        .order_by("canonical_name", "pk")
    )
    for entity in project_entities:
        project_claims = list(ordered.filter(subject_entity=entity))
        project_start = min(
            (
                claim.observed_at
                or claim.valid_from
                or claim.recorded_at
                for claim in project_claims
            ),
            default=week_start,
        )
        project_start = min(project_start, day_start)
        summaries.append(
            _upsert_summary(
                report=report,
                summary_type=MemorySummaryType.PROJECT,
                subject_key=str(entity.pk),
                title=f"Project summary — {entity.canonical_name}",
                claims=project_claims,
                window_start=project_start,
                window_end=day_end,
                extra_classifications=(entity.classification,),
            )
        )

    thread_claims = (
        base.filter(
            _claim_time_filter(day_start, day_end),
            evidence__source__source_type__in=(
                "slack_thread",
                "gmail_thread",
                "thread",
                "email_thread",
            ),
        )
        .distinct()
        .order_by("-importance", "-recorded_at", "pk")
    )
    thread_sources = {}
    for claim in thread_claims:
        evidence = (
            eligible_evidence_queryset(organization=organization)
            .filter(
                claim=claim,
                source__source_type__in=(
                    "slack_thread",
                    "gmail_thread",
                    "thread",
                    "email_thread",
                ),
            )
            .select_related("source", "source__current_version")
        )
        for item in evidence:
            thread_sources.setdefault(item.source_id, item.source)
    for source_id, source in sorted(
        thread_sources.items(),
        key=lambda item: (item[1].title, str(item[0])),
    ):
        claims = thread_claims.filter(evidence__source_id=source_id).distinct()
        summaries.append(
            _upsert_summary(
                report=report,
                summary_type=MemorySummaryType.THREAD,
                subject_key=str(source_id),
                title=f"Thread summary — {source.title or source.external_id}",
                claims=claims,
                window_start=day_start,
                window_end=day_end,
                parent=day_summary,
                extra_classifications=(
                    source.current_version.classification
                    if source.current_version_id
                    else None,
                ),
            )
        )
    return summaries


def _blocked_warnings(report) -> list[dict]:
    warnings = [
        {
            "code": "connector_not_reconciled",
            "provider": snapshot.provider,
            "configuration_id": str(snapshot.configuration_id),
            "health_status": snapshot.health_status,
            "freshness_status": snapshot.freshness_status,
            "schedule_status": snapshot.schedule_status,
        }
        for snapshot in report.connection_snapshots.order_by(
            "provider",
            "configuration_id",
        )
        if snapshot.health_status != MemoryConnectionHealthStatus.HEALTHY
        or snapshot.schedule_status not in {"completed", "noop"}
    ]
    known = {
        (
            row.get("code"),
            row.get("provider"),
            row.get("configuration_id"),
        )
        for row in warnings
    }
    for alert in report.alerts or ():
        if not isinstance(alert, dict):
            continue
        safe = {
            key: alert[key]
            for key in (
                "code",
                "severity",
                "provider",
                "configuration_id",
            )
            if alert.get(key) not in (None, "")
        }
        key = (
            safe.get("code"),
            safe.get("provider"),
            safe.get("configuration_id"),
        )
        if safe.get("code") and key not in known:
            warnings.append(safe)
            known.add(key)
    return warnings


def reconciliation_succeeded(report) -> bool:
    snapshots = report.connection_snapshots.all()
    return bool(
        report.status == MemoryDailyReconciliationStatus.COMPLETED
        and not report.alerts
        and snapshots.exists()
        and not snapshots.exclude(
            health_status=MemoryConnectionHealthStatus.HEALTHY,
            schedule_status__in=("completed", "noop"),
        ).exists()
    )


@transaction.atomic
def _upsert_digest(
    *,
    report,
    digest_type,
    window_start,
    window_end,
    claims,
    blocked_warnings=None,
):
    title = (
        f"Daily open-loop digest — {report.report_date.isoformat()}"
        if digest_type == MemoryDigestType.DAILY_OPEN_LOOPS
        else f"Weekly committee digest — {window_start.date().isoformat()}"
    )
    idempotency_key = f"{digest_type}:{report.report_date.isoformat()}"
    blocked_warnings = list(blocked_warnings or ())
    claims = list(claims)[
        : _bounded_positive_setting("ORG_MEMORY_DIGEST_MAX_ITEMS", 25, 200)
    ]
    if blocked_warnings:
        claims = []
        status = MemoryDerivedArtifactStatus.BLOCKED
        providers = ", ".join(
            sorted(
                {
                    str(row.get("provider"))
                    for row in blocked_warnings
                    if row.get("provider")
                    and row.get("provider") != "unknown"
                }
            )
        )
        body = (
            "Digest not generated because daily reconciliation is incomplete for: "
            f"{providers or 'the daily reconciliation report'}."
        )
    elif claims:
        status = MemoryDerivedArtifactStatus.READY
        body = "\n".join(f"- {claim.statement}" for claim in claims)
    else:
        status = MemoryDerivedArtifactStatus.EMPTY
        body = "No matching open items were found in reconciled evidence."
    evidence_by_claim = _evidence_for_claims(claims) if claims else {}
    digest, _created = MemoryDigest.objects.update_or_create(
        organization=report.organization,
        digest_type=digest_type,
        digest_date=report.report_date,
        defaults={
            "time_zone": report.time_zone,
            "window_start": window_start,
            "window_end": window_end,
            "title": title,
            "body": body,
            "status": status,
            "warnings": blocked_warnings,
            "required_classifications": _classifications(
                claims,
                evidence_by_claim,
            ),
            "source_report": report,
            "idempotency_key": idempotency_key,
        },
    )
    digest.items.all().delete()
    if not claims:
        return digest
    current_project_summaries = {
        row.subject_key: row
        for row in MemorySummary.objects.filter(
            organization=report.organization,
            summary_type=MemorySummaryType.PROJECT,
            is_current=True,
            status=MemoryDerivedArtifactStatus.READY,
        )
    }
    for ordinal, claim in enumerate(claims):
        summary = (
            current_project_summaries.get(str(claim.subject_entity_id))
            if claim.subject_entity_id
            else None
        )
        item = MemoryDigestItem.objects.create(
            digest=digest,
            claim=claim,
            summary=summary,
            ordinal=ordinal,
            text=claim.statement,
        )
        MemoryDigestItemEvidence.objects.bulk_create(
            [
                MemoryDigestItemEvidence(item=item, evidence=evidence)
                for evidence in evidence_by_claim.get(claim.pk, ())
            ]
        )
    return digest


def generate_digests(*, report, force_weekly=False) -> list[MemoryDigest]:
    if report.status == MemoryDailyReconciliationStatus.RUNNING:
        return []
    day_start, day_end = _window(report)
    warnings = (
        []
        if reconciliation_succeeded(report)
        else _blocked_warnings(report)
        or [{"code": "daily_reconciliation_not_successful", "provider": "unknown"}]
    )
    claims = eligible_claims_queryset(organization=report.organization)
    daily_claims = claims.filter(kind__in=DAILY_OPEN_LOOP_KINDS).order_by(
        "-importance",
        "stale_after",
        "-recorded_at",
        "pk",
    )
    digests = [
        _upsert_digest(
            report=report,
            digest_type=MemoryDigestType.DAILY_OPEN_LOOPS,
            window_start=day_start,
            window_end=day_end,
            claims=daily_claims,
            blocked_warnings=warnings,
        )
    ]
    weekly_day = int(getattr(settings, "ORG_MEMORY_WEEKLY_DIGEST_WEEKDAY", 0))
    if weekly_day not in range(7):
        weekly_day = 0
    if force_weekly or report.report_date.weekday() == weekly_day:
        week_start, week_end = _previous_week_window(report)
        weekly_claims = (
            claims.filter(
                kind__in=WEEKLY_COMMITTEE_KINDS,
            )
            .filter(_claim_time_filter(week_start, week_end))
            .order_by("-importance", "-recorded_at", "pk")
        )
        digests.append(
            _upsert_digest(
                report=report,
                digest_type=MemoryDigestType.WEEKLY_COMMITTEE,
                window_start=week_start,
                window_end=week_end,
                claims=weekly_claims,
                blocked_warnings=warnings,
            )
        )
    return digests


@transaction.atomic
def run_post_reconciliation_artifacts(*, report, force_weekly=False) -> dict:
    report = (
        MemoryDailyReconciliationReport.objects.select_for_update()
        .select_related("organization")
        .get(pk=report.pk)
    )
    if report.status == MemoryDailyReconciliationStatus.RUNNING:
        return {"status": "waiting", "summaries": 0, "digests": 0}
    succeeded = reconciliation_succeeded(report)
    if not succeeded:
        report.summaries.filter(is_current=True).update(
            is_current=False,
            status=MemoryDerivedArtifactStatus.STALE,
            invalidated_at=timezone.now(),
            updated_at=timezone.now(),
        )
    summaries = generate_summaries(report=report)
    digests = generate_digests(report=report, force_weekly=force_weekly)
    return {
        "status": (
            "completed"
            if succeeded
            else "blocked"
        ),
        "summaries": len(summaries),
        "digests": len(digests),
    }


def reconcile_derived_visibility_for_source(source) -> dict:
    now = timezone.now()
    summary_ids = MemorySummaryEvidence.objects.filter(
        evidence__source=source,
    ).values_list("summary_id", flat=True)
    summaries = MemorySummary.objects.filter(
        pk__in=summary_ids,
        is_current=True,
    ).update(
        is_current=False,
        status=MemoryDerivedArtifactStatus.STALE,
        invalidated_at=now,
        updated_at=now,
    )
    digest_ids = MemoryDigestItemEvidence.objects.filter(
        evidence__source=source,
    ).values_list("item__digest_id", flat=True)
    digests = 0
    for digest in MemoryDigest.objects.filter(pk__in=digest_ids).exclude(
        status=MemoryDerivedArtifactStatus.BLOCKED
    ):
        digest.status = MemoryDerivedArtifactStatus.BLOCKED
        digest.body = "Digest unavailable because source access changed."
        digest.warnings = [
            {
                "code": "source_access_changed",
                "provider": source.provider,
            }
        ]
        digest.save(update_fields=("status", "body", "warnings", "updated_at"))
        digests += 1
    from .publication import retire_publications_for_source

    publication_result = retire_publications_for_source(
        source,
        reason="private_source_access_or_lifecycle_changed",
    )
    return {
        "summaries_invalidated": summaries,
        "digests_blocked": digests,
        **publication_result,
    }


def reconcile_derived_visibility_for_claim(claim) -> dict:
    now = timezone.now()
    summary_ids = MemorySummaryClaim.objects.filter(
        claim=claim,
    ).values_list("summary_id", flat=True)
    summaries = MemorySummary.objects.filter(
        pk__in=summary_ids,
        is_current=True,
    ).update(
        is_current=False,
        status=MemoryDerivedArtifactStatus.STALE,
        invalidated_at=now,
        updated_at=now,
    )
    digests = 0
    for digest in MemoryDigest.objects.filter(items__claim=claim).distinct().exclude(
        status=MemoryDerivedArtifactStatus.BLOCKED
    ):
        digest.status = MemoryDerivedArtifactStatus.BLOCKED
        digest.body = "Digest unavailable because a linked claim changed state."
        digest.warnings = [
            {
                "code": "claim_state_changed",
            }
        ]
        digest.save(update_fields=("status", "body", "warnings", "updated_at"))
        digests += 1
    from .publication import retire_publications_for_claim

    publication_result = retire_publications_for_claim(
        claim,
        reason="private_claim_state_changed",
    )
    return {
        "summaries_invalidated": summaries,
        "digests_blocked": digests,
        **publication_result,
    }
