from datetime import datetime, timedelta, timezone as dt_timezone
from typing import Any, Optional, Tuple

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import OperationalError, transaction
from django.shortcuts import get_object_or_404
from django.utils.dateparse import parse_datetime
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from integrations import http_client as requests
from organizations.models import Organization
from workflow_runs.models import ContentFactoryRun, ContentFactoryRunStatus, ContentFactoryStepStatus
from core.permissions import HasRooApiKey
from startup_updates.serializers import (
    ClassificationResultsSerializer,
    CurationResultsSerializer,
    DraftResultsSerializer,
    ExtractionResultsSerializer,
    LinearClassificationResultsSerializer,
    LinearExtractionResultsSerializer,
    GoogleAnalyticsClassificationResultsSerializer,
    GoogleAnalyticsExtractionResultsSerializer,
    NotionClassificationResultsSerializer,
    NotionExtractionResultsSerializer,
    SlackClassificationResultsSerializer,
    SlackExtractionResultsSerializer,
    StartupProfileUpsertSerializer,
    StartupUpdateBatchQuerySerializer,
    StartupUpdateIngestSerializer,
    StartupUpdateRunCreateSerializer,
    StartupUpdateThreadHydrationSerializer,
)
from integrations.models import ExternalServiceProvider, GoogleConnection
from startup_updates.models import (
    ArtifactProcessingStatus,
    GmailAttachmentArtifact,
    GmailMessageArtifact,
    GmailRelevanceLabel,
    GmailSyncCursor,
    GmailThreadArtifact,
    LinearProjectArtifact,
    LinearProjectSelection,
    SlackChannelSelection,
    SlackThreadArtifact,
    MonthlyUpdateDraft,
    StartupMetricObservation,
    StartupEvent,
    UserStartupBinding,
)
from startup_updates.metric_catalog import (
    startup_update_metric_key,
    startup_update_metric_label,
)
from integrations.services.external_connectors import (
    ConnectorConfigurationError,
    ConnectorOAuthError,
    ConnectorRateLimitError,
    google_connection_for_org,
    sync_linear_connection_page,
    sync_slack_connection_page,
)
from integrations.services.gmail import (
    default_backfill_window,
    ensure_thread_attachments_hydrated,
    hydrate_thread_artifact,
    is_gmail_insufficient_permissions_error,
    StaleHistoryCursorError,
    six_month_backfill_window,
    sync_history_metadata_page,
    sync_message_metadata_page,
)
from integrations.services.gmail_scopes import (
    GMAIL_INSUFFICIENT_SCOPE_CODE,
    GMAIL_RECONNECT_WARNING,
    gmail_scope_status_payload,
    has_gmail_read_scope,
)
from startup_updates.services import (
    DEFAULT_BACKFILL_MONTHS,
    OPEN_RUN_STATUSES,
    RUN_STEP_ORDER,
    STARTUP_UPDATE_WORKFLOW,
    HIGH_SIGNAL_TERMS,
    bind_user_to_startup,
    build_cancel_backup_for_draft,
    build_cancel_backup_for_event,
    build_cancel_backup_for_metric,
    build_timeline_payload,
    cancel_startup_update_run,
    apply_slack_profile_scoring,
    compact_gmail_thread_bundle,
    compact_linear_project_bundle,
    compact_slack_thread_bundle,
    coerce_startup_update_sources_for_gmail_scope,
    create_startup_update_run,
    DEFAULT_MAX_SOURCE_THREADS,
    get_default_binding_for_domain,
    get_open_startup_update_run,
    get_startup_update_run_cancel_backups,
    get_startup_update_run_google_connection_id,
    get_startup_update_run_target_month,
    gmail_required_for_sources,
    latest_external_connection_for_startup,
    merge_luma_metrics_into_structured_memo,
    merge_xero_metrics_into_structured_memo,
    normalize_startup_update_input_sources,
    parse_startup_update_target_month,
    pin_startup_update_run_connection,
    record_valley_dispatch_result,
    resolve_or_create_profile,
    seed_startup_profile,
    set_startup_update_run_cancel_backups,
    startup_update_run_matches_target_month,
    upsert_monthly_update_draft,
)
from integrations.services.valley_harness import notify_valley_run_created
from integrations.services.google_analytics import (
    apply_classification_results as ga_apply_classification_results,
    build_classification_batch as ga_build_classification_batch,
    build_extraction_batch as ga_build_extraction_batch,
    get_ga_run_store,
    resolve_google_analytics_connection_for_run,
    run_google_analytics_backfill,
    save_ga_run_store,
)
from integrations.utils import normalize_domain

User = get_user_model()

EMAIL_DRAFT_DISPLAY_STAGES = {
    "profile_resolution": "Preparing company context",
    "gmail_backfill": "Scanning recent Gmail messages",
    "relevance_classification": "Finding investor-relevant updates",
    "thread_hydration": "Pulling full thread context",
    "event_extraction": "Extracting metrics and highlights",
    "slack_backfill": "Scanning selected Slack channels",
    "slack_relevance_classification": "Filtering Slack highlights",
    "slack_event_extraction": "Extracting Slack highlights",
    "linear_backfill": "Scanning selected Linear projects",
    "linear_relevance_classification": "Filtering Linear project context",
    "linear_event_extraction": "Extracting Linear project highlights",
    "notion_backfill": "Scanning selected Notion workspace",
    "notion_relevance_classification": "Filtering Notion workspace context",
    "notion_event_extraction": "Extracting Notion highlights",
    "google_analytics_backfill": "Pulling Google Analytics reports",
    "google_analytics_relevance_classification": "Filtering Google Analytics signal",
    "google_analytics_event_extraction": "Extracting Google Analytics metrics",
    "timeline_merge": "Building timeline",
    "candidate_curation": "Choosing update-worthy candidates",
    "founder_review": "Preparing founder review",
    "draft_generation": "Drafting monthly updates",
    "groundedness_review": "Final review",
}
UPDATE_WORTHY_RELEVANCE_LABELS = [
    GmailRelevanceLabel.UPDATE_WORTHY,
    GmailRelevanceLabel.RELEVANT,
]
EXTRACTABLE_RELEVANCE_LABELS = [
    GmailRelevanceLabel.UPDATE_WORTHY,
    GmailRelevanceLabel.RELEVANT,
    GmailRelevanceLabel.AMBIGUOUS,
]
VALLEY_META_KEY = "_valley_meta"
TRANSIENT_SQLITE_LOCK_MARKERS = (
    "database is locked",
    "database table is locked",
    "database schema is locked",
)
QUEUED_REDISPATCH_AFTER = timedelta(seconds=30)


def _is_transient_sqlite_lock(exc: Exception) -> bool:
    message = str(exc).lower()
    return any(marker in message for marker in TRANSIENT_SQLITE_LOCK_MARKERS)


def _should_dispatch_existing_run(run) -> bool:
    if not bool(run) or run.status != ContentFactoryRunStatus.QUEUED:
        return False
    result_payload = run.result if isinstance(run.result, dict) else {}
    meta = result_payload.get(VALLEY_META_KEY) if isinstance(result_payload, dict) else {}
    if isinstance(meta, dict) and meta.get("dispatch_status") == "failed":
        return True
    return bool(run.updated_at) and run.updated_at <= timezone.now() - QUEUED_REDISPATCH_AFTER


def _valley_dispatch_failure_payload(run, dispatch_result) -> dict:
    return {
        "run_id": run.run_id,
        "error": "valley_dispatch_failed",
        "retryable": True,
        "message": "The startup update run was saved, but Valley could not be reached. Check Valley connectivity and retry.",
        "valley_dispatch": {
            "status": "failed",
            "failure_kind": str(getattr(dispatch_result, "failure_kind", "") or "unknown"),
            "status_code": getattr(dispatch_result, "status_code", None),
            "detail": str(getattr(dispatch_result, "detail", "") or "")[:300],
        },
    }


def _dispatch_run_to_valley(run):
    dispatch_result = notify_valley_run_created(run.run_id)
    record_valley_dispatch_result(run, dispatch_result)
    return dispatch_result


def _serialize_profile(profile) -> dict:
    return {
        "organization_id": profile.organization_id,
        "domain": profile.organization.domain,
        "company_aliases": profile.company_aliases,
        "domain_aliases": profile.domain_aliases,
        "product_names": profile.product_names,
        "founder_names": profile.founder_names,
        "team_names": profile.team_names,
        "investor_names": profile.investor_names,
        "investor_domains": profile.investor_domains,
        "competitor_names": profile.competitor_names,
        "competitor_domains": profile.competitor_domains,
        "customer_names": profile.customer_names,
        "customer_domains": profile.customer_domains,
        "prospect_names": profile.prospect_names,
        "prospect_domains": profile.prospect_domains,
        "positive_keywords": profile.positive_keywords,
        "negative_keywords": profile.negative_keywords,
        "kpi_definitions": profile.kpi_definitions,
        "default_currency": profile.default_currency,
        "stage": profile.stage,
        "organization_kind": profile.organization_kind,
        "organizationKind": profile.organization_kind,
        "notes": profile.notes,
        "created_at": profile.created_at.isoformat(),
        "updated_at": profile.updated_at.isoformat(),
    }


def _serialize_binding(binding) -> dict:
    return {
        "id": binding.id,
        "user_id": binding.user_id,
        "organization_id": binding.organization_id,
        "google_connection_id": binding.google_connection_id,
        "role": binding.role,
        "is_default_for_gmail": binding.is_default_for_gmail,
    }


def _serialize_run(run, request) -> dict:
    return {
        "run_id": run.run_id,
        "workflow": run.workflow,
        "domain": run.domain,
        "status": run.status,
        "current_step": run.current_step,
        "run_request": run.run_request or {},
        "step_order": run.step_order or [],
        "status_url": request.build_absolute_uri(
            reverse("content_factory_run", kwargs={"run_id": run.run_id})
        ),
    }


def _serialize_open_run(run) -> dict:
    return {
        "run_id": run.run_id,
        "workflow": run.workflow,
        "domain": run.domain,
        "status": run.status,
        "current_step": run.current_step,
        "run_request": run.run_request or {},
        "result": run.result or {},
        "step_order": run.step_order or [],
        "step_states": {},
        "created_at": run.created_at.isoformat(),
        "updated_at": run.updated_at.isoformat(),
    }


def _serialize_attachment(attachment) -> dict:
    return {
        "id": attachment.id,
        "filename": attachment.filename,
        "mime_type": attachment.mime_type,
        "part_id": attachment.part_id,
        "gmail_attachment_id": attachment.gmail_attachment_id,
        "size_bytes": attachment.size_bytes,
        "is_inline": attachment.is_inline,
        "raw_content_base64": attachment.raw_content_base64,
        "extracted_text": attachment.extracted_text,
        "extraction_status": attachment.extraction_status,
        "parse_notes": attachment.parse_notes,
        "last_error": attachment.last_error,
        "metadata": attachment.metadata or {},
    }


def _serialize_draft(draft) -> dict:
    return {
        "id": draft.id,
        "organization_id": draft.organization_id,
        "month": draft.month.isoformat(),
        "status": draft.status,
        "title": draft.title,
        "model_name": draft.model_name,
        "groundedness_status": draft.groundedness_status,
        "structured_memo": draft.structured_memo or {},
        "rendered_markdown": draft.rendered_markdown,
        "evidence_event_ids": draft.evidence_event_ids or [],
        "evidence_metric_ids": draft.evidence_metric_ids or [],
        "carry_forward_event_ids": draft.carry_forward_event_ids or [],
        "groundedness_notes": draft.groundedness_notes or "",
        "created_at": draft.created_at.isoformat(),
        "updated_at": draft.updated_at.isoformat(),
    }


def _serialize_event(event) -> dict:
    return {
        "id": event.id,
        "canonical_key": event.canonical_key,
        "event_type": event.event_type,
        "title": event.title,
        "summary": event.summary,
        "event_date": event.event_date.isoformat() if event.event_date else None,
        "month_bucket": event.month_bucket.isoformat(),
        "date_precision": event.date_precision,
        "sentiment": event.sentiment,
        "investor_importance": event.investor_importance,
        "quantitative_facts": event.quantitative_facts or [],
        "evidence_message_ids": event.evidence_message_ids or [],
        "evidence_attachment_ids": event.evidence_attachment_ids or [],
        "source_thread_ids": event.source_thread_ids or [],
        "confidence": event.confidence,
        "status": event.status,
        "needs_review": event.needs_review,
        "merge_notes": event.merge_notes or "",
    }


def _serialize_metric(metric) -> dict:
    return {
        "id": metric.id,
        "metric_key": metric.metric_key,
        "metric_name": metric.metric_name,
        "value_text": metric.value_text,
        "value_number": str(metric.value_number) if metric.value_number is not None else None,
        "unit": metric.unit,
        "observed_at": metric.observed_at.isoformat() if metric.observed_at else None,
        "period_month": metric.period_month.isoformat(),
        "confidence": metric.confidence,
        "evidence_message_ids": metric.evidence_message_ids or [],
        "evidence_attachment_ids": metric.evidence_attachment_ids or [],
        "source_provider": metric.source_provider or "",
        "source_record_ids": metric.source_record_ids or [],
        "source_metadata": metric.source_metadata or {},
        "summary": metric.summary or "",
    }


def _parse_optional_int(value) -> Optional[int]:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _input_sources_from_query_params(query_params) -> Optional[list[str]]:
    raw_values = [
        *query_params.getlist("inputSources"),
        *query_params.getlist("input_sources"),
    ]
    if not raw_values:
        return None
    values = []
    for raw_value in raw_values:
        values.extend(str(raw_value or "").replace(",", " ").split())
    return normalize_startup_update_input_sources(values)


def _get_run_result_payload(run: ContentFactoryRun) -> dict:
    payload = run.result or {}
    return payload if isinstance(payload, dict) else {}


def _get_run_meta(run: ContentFactoryRun) -> dict:
    meta = _get_run_result_payload(run).get(VALLEY_META_KEY) or {}
    return meta if isinstance(meta, dict) else {}


def _cancelled_run_response(run: ContentFactoryRun) -> Response:
    return Response(
        {
            "error": "run_cancelled",
            "detail": "This startup update run was cancelled and cannot accept more workflow writes.",
            "run_id": run.run_id,
            "status": run.status,
        },
        status=status.HTTP_409_CONFLICT,
    )


def _reject_if_run_cancelled(run: ContentFactoryRun) -> Optional[Response]:
    if run.status == ContentFactoryRunStatus.CANCELLED:
        return _cancelled_run_response(run)
    return None


def _backup_draft_if_needed(run: ContentFactoryRun, draft: MonthlyUpdateDraft, backups: dict) -> bool:
    if draft.run_id == run.run_id:
        return False

    draft_backups = dict((backups or {}).get("drafts") or {})
    draft_key = draft.month.isoformat()
    if draft_key in draft_backups:
        return False

    draft_backups[draft_key] = build_cancel_backup_for_draft(draft)
    backups["drafts"] = draft_backups
    return True


def _backup_event_if_needed(run: ContentFactoryRun, event: StartupEvent, backups: dict) -> bool:
    if event.run_id == run.run_id:
        return False

    event_backups = dict((backups or {}).get("events") or {})
    event_key = event.canonical_key
    if event_key in event_backups:
        return False

    event_backups[event_key] = build_cancel_backup_for_event(event)
    backups["events"] = event_backups
    return True


def _backup_metric_if_needed(run: ContentFactoryRun, metric: StartupMetricObservation, backups: dict) -> bool:
    if metric.run_id == run.run_id:
        return False

    metric_backups = dict((backups or {}).get("metrics") or {})
    metric_key = "|".join(
        [
            str(metric.source_thread_id or ""),
            metric.metric_key,
            metric.period_month.isoformat(),
            metric.value_text,
        ]
    )
    if metric_key in metric_backups:
        return False

    metric_backups[metric_key] = build_cancel_backup_for_metric(metric)
    backups["metrics"] = metric_backups
    return True


def _get_run_generated_draft_months(run: ContentFactoryRun) -> list[str]:
    raw_months = (
        _get_run_result_payload(run).get("generated_draft_months")
        or (run.run_request or {}).get("draft_months")
        or []
    )
    if not isinstance(raw_months, (list, tuple)):
        return []

    months: list[str] = []
    for item in raw_months:
        text = str(item or "").strip()
        if text:
            months.append(text)
    return months


def _get_email_draft_display_stage(current_step: Optional[str]) -> str:
    step_key = str(current_step or "").strip() or RUN_STEP_ORDER[0]
    return EMAIL_DRAFT_DISPLAY_STAGES.get(step_key, "Preparing company context")


def _count_completed_run_steps(run: ContentFactoryRun) -> Tuple[int, int]:
    ordered_steps = list(run.step_order or RUN_STEP_ORDER)
    if not ordered_steps:
        return 0, 0

    step_statuses = {
        step.step_key: step.status
        for step in run.steps.all()
    }
    completed_count = 0
    for step_key in ordered_steps:
        if step_statuses.get(step_key) in {
            ContentFactoryStepStatus.COMPLETED,
            ContentFactoryStepStatus.SKIPPED,
        }:
            completed_count += 1

    if run.status == ContentFactoryRunStatus.COMPLETED:
        completed_count = len(ordered_steps)

    return completed_count, len(ordered_steps)


def _serialize_run_progress(run: ContentFactoryRun) -> dict:
    completed_steps, total_steps = _count_completed_run_steps(run)
    target_month = get_startup_update_run_target_month(run)
    return {
        "run_id": run.run_id,
        "status": run.status,
        "current_step": run.current_step,
        "completed_steps": completed_steps,
        "total_steps": total_steps,
        "display_stage": _get_email_draft_display_stage(run.current_step),
        "last_heartbeat_at": _get_run_meta(run).get("last_heartbeat_at"),
        "can_retry": run.status in {
            ContentFactoryRunStatus.FAILED,
            ContentFactoryRunStatus.DENIED,
        },
        "terminal_state": (
            run.status
            if run.status in {
                ContentFactoryRunStatus.COMPLETED,
                ContentFactoryRunStatus.FAILED,
                ContentFactoryRunStatus.DENIED,
                ContentFactoryRunStatus.CANCELLED,
            }
            else None
        ),
        "generated_draft_months": _get_run_generated_draft_months(run),
        "target_month": target_month.isoformat() if target_month else None,
    }


def _normalize_text_list(value) -> list[str]:
    if value is None:
        return []

    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []

    if isinstance(value, dict):
        candidate = (
            value.get("text")
            or value.get("value")
            or value.get("title")
            or value.get("label")
            or ""
        )
        text = str(candidate).strip()
        return [text] if text else []

    if isinstance(value, (list, tuple)):
        items: list[str] = []
        for item in value:
            items.extend(_normalize_text_list(item))
        return items

    text = str(value).strip()
    return [text] if text else []


def _join_text_items(value) -> str:
    items = _normalize_text_list(value)
    if not items:
        return ""

    text = ". ".join(item.rstrip(". ") for item in items if item.strip())
    text = text.strip()
    if text and text[-1].isalnum():
        text += "."
    return text


def _join_text_items_with_newlines(value) -> str:
    items = _normalize_text_list(value)
    return "\n".join(item.strip() for item in items if item.strip())


def _join_named_sections(structured_memo, sections) -> str:
    lines = []
    for label, key in sections:
        for item in _normalize_text_list((structured_memo or {}).get(key)):
            prefix = f"{label}: " if label else ""
            lines.append(f"{prefix}{item}")
    return "\n".join(lines)


# Sections merged into the single "What moved the business forward?" field. Labels are
# intentionally blank so points render without a category prefix and read as one
# continuous, founder-voiced list rather than a categorized report.
_HIGHLIGHT_SECTIONS = [
    ("", "financial_performance"),
    ("", "highlights"),
    ("", "operations"),
]


def _metric_key_from_label(label) -> Optional[str]:
    return startup_update_metric_key(label)


def _normalize_metric_value(value) -> Optional[str]:
    if value is None:
        return None

    text = str(value).strip()
    return text or None


def _extract_form_metrics(structured_memo) -> dict:
    metrics = {}
    snapshot = (structured_memo or {}).get("kpi_snapshot") or []
    for item in snapshot:
        if not isinstance(item, dict):
            continue

        metric_key = startup_update_metric_key(item.get("metric_key")) or _metric_key_from_label(
            item.get("label") or item.get("name") or item.get("metric_name")
        )
        if not metric_key:
            continue

        metric_value = _normalize_metric_value(
            item.get("value")
            or item.get("value_text")
            or item.get("value_number")
        )
        if metric_value:
            metrics[metric_key] = metric_value

    return metrics


def _extract_metric_suggestions(structured_memo) -> list[dict]:
    suggestions = []
    raw_suggestions = (
        (structured_memo or {}).get("metric_suggestions")
        or (structured_memo or {}).get("metricSuggestions")
        or []
    )
    for item in raw_suggestions:
        if not isinstance(item, dict):
            continue
        metric_key = startup_update_metric_key(item.get("metric_key") or item.get("metricKey")) or _metric_key_from_label(
            item.get("label") or item.get("name") or item.get("metric_name")
        )
        if not metric_key:
            continue
        suggestions.append(
            {
                "metricKey": metric_key,
                "label": str(item.get("label") or startup_update_metric_label(metric_key)).strip()
                or startup_update_metric_label(metric_key),
                "reason": str(item.get("reason") or "").strip(),
            }
        )
    return suggestions


def _structured_memo_section_text(structured_memo, key: str) -> str:
    return _join_text_items_with_newlines((structured_memo or {}).get(key))


def _structured_memo_with_xero_metrics(draft) -> dict:
    # Merges connector-backed metrics (Xero + Luma) into the draft's kpi_snapshot.
    structured_memo = draft.structured_memo or {}
    if not getattr(draft, "organization_id", None):
        return structured_memo

    evidence_metric_ids = getattr(draft, "evidence_metric_ids", []) or []
    merged_memo, evidence_metric_ids = merge_xero_metrics_into_structured_memo(
        organization=draft.organization,
        month=draft.month,
        structured_memo=structured_memo,
        evidence_metric_ids=evidence_metric_ids,
    )
    merged_memo, _evidence_metric_ids = merge_luma_metrics_into_structured_memo(
        organization=draft.organization,
        month=draft.month,
        structured_memo=merged_memo,
        evidence_metric_ids=evidence_metric_ids,
    )
    return merged_memo


def _serialize_draft_for_editor(draft) -> dict:
    structured_memo = _structured_memo_with_xero_metrics(draft)
    month_value = draft.month
    return {
        "month": month_value.strftime("%B"),
        "year": month_value.year,
        "highlights": _join_named_sections(structured_memo, _HIGHLIGHT_SECTIONS),
        "challenges": _join_text_items_with_newlines(structured_memo.get("lowlights")),
        "asks": _join_named_sections(structured_memo, [
            ("", "asks"),
        ]),
        "learnings": _structured_memo_section_text(structured_memo, "learnings"),
        "next30Days": _structured_memo_section_text(structured_memo, "next_30_days"),
        "metrics": _extract_form_metrics(structured_memo),
        "metricSuggestions": _extract_metric_suggestions(structured_memo),
    }


def _serialize_email_draft_month(draft) -> dict:
    structured_memo = _structured_memo_with_xero_metrics(draft)
    month_value = draft.month
    return {
        "draft_id": draft.id,
        "iso_month": month_value.isoformat(),
        "month": month_value.strftime("%B"),
        "year": month_value.year,
        "metrics": _extract_form_metrics(structured_memo),
        "metricSuggestions": _extract_metric_suggestions(structured_memo),
        "highlights": _join_named_sections(structured_memo, _HIGHLIGHT_SECTIONS),
        "challenges": _join_text_items_with_newlines(structured_memo.get("lowlights")),
        "asks": _join_named_sections(structured_memo, [
            ("", "asks"),
        ]),
        "learnings": _structured_memo_section_text(structured_memo, "learnings"),
        "next30Days": _structured_memo_section_text(structured_memo, "next_30_days"),
    }


def _serialize_draft_results_bundle(drafts) -> Optional[dict]:
    if not drafts:
        return None

    current_month = _serialize_email_draft_month(drafts[0])
    past_months = [_serialize_email_draft_month(draft) for draft in reversed(drafts[1:])]

    editor_past_months = []
    for draft in drafts[1:3]:
        payload = _serialize_draft_for_editor(draft)
        editor_past_months.append(
            {
                "month": f"{payload['month']} {payload['year']}",
                "highlights": payload["highlights"],
                "challenges": payload["challenges"],
                "asks": payload["asks"],
                "learnings": payload["learnings"],
                "next30Days": payload["next30Days"],
                "metrics": payload["metrics"],
                "metricSuggestions": payload["metricSuggestions"],
            }
        )

    return {
        "draft": {
            **_serialize_draft_for_editor(drafts[0]),
            "pastMonths": editor_past_months,
        },
        "current_month": current_month,
        "past_months": past_months,
        "months": [*past_months, current_month],
    }


def _get_run_or_404(run_id: str) -> ContentFactoryRun:
    return get_object_or_404(
        ContentFactoryRun,
        run_id=run_id,
        workflow=STARTUP_UPDATE_WORKFLOW,
    )


def _get_run_window_bounds(run: ContentFactoryRun) -> Tuple[Optional[datetime], Optional[datetime]]:
    request_payload = run.run_request or {}
    start = parse_datetime(str(request_payload.get("backfill_window_start") or "").strip() or "")
    end = parse_datetime(str(request_payload.get("backfill_window_end") or "").strip() or "")
    return start, end


def _apply_run_window(queryset, run: ContentFactoryRun, field_name: str):
    start, end = _get_run_window_bounds(run)
    filters = {}
    if start is not None:
        filters[f"{field_name}__gte"] = start
    if end is not None:
        filters[f"{field_name}__lte"] = end
    if filters:
        queryset = queryset.filter(**filters)
    return queryset


def _get_run_max_source_threads(run: ContentFactoryRun) -> int:
    raw_value = (run.run_request or {}).get("max_source_threads", DEFAULT_MAX_SOURCE_THREADS)
    try:
        value = int(raw_value)
    except (TypeError, ValueError):
        return DEFAULT_MAX_SOURCE_THREADS
    return max(value, 1)


def _get_prioritized_run_thread_ids(
    *,
    run: ContentFactoryRun,
    organization: Organization,
    google_connection: GoogleConnection,
) -> list[str]:
    thread_limit = _get_run_max_source_threads(run)
    queryset = _apply_run_window(
        GmailMessageArtifact.objects.filter(
            organization=organization,
            google_connection=google_connection,
            relevance_label__in=EXTRACTABLE_RELEVANCE_LABELS,
            needs_thread_context=True,
        )
        .exclude(gmail_thread_id="")
        .order_by("-internal_date", "-updated_at")
        .only("gmail_thread_id"),
        run,
        "internal_date",
    )

    thread_ids: list[str] = []
    seen_thread_ids: set[str] = set()
    for artifact in queryset:
        if artifact.gmail_thread_id in seen_thread_ids:
            continue
        seen_thread_ids.add(artifact.gmail_thread_id)
        thread_ids.append(artifact.gmail_thread_id)
        if len(thread_ids) >= thread_limit:
            break
    return thread_ids


def _get_org_and_binding_for_run(run: ContentFactoryRun):
    organization = get_object_or_404(Organization, domain=normalize_domain(run.domain))
    binding_id = (run.run_request or {}).get("binding_id")
    binding = get_object_or_404(
        organization.user_startup_bindings.select_related("user", "google_connection"),
        id=binding_id,
    )
    google_connection_id = get_startup_update_run_google_connection_id(run)
    if google_connection_id is None:
        google_connection = binding.google_connection or google_connection_for_org(
            binding.user, binding.organization
        )
        if google_connection is not None:
            pin_startup_update_run_connection(run, google_connection.id)
    else:
        google_connection = get_object_or_404(GoogleConnection, id=google_connection_id)
    profile = getattr(organization, "startup_profile", None)
    if profile is None:
        _, profile = resolve_or_create_profile(domain=organization.domain)
    return organization, binding, google_connection, profile


def _gmail_connection_required_response() -> Response:
    return Response(
        {"error": "This run does not have a Gmail connection."},
        status=status.HTTP_400_BAD_REQUEST,
    )


def _gmail_source_unavailable_payload(
    run: ContentFactoryRun,
    request,
    *,
    connection=None,
    warning: str = GMAIL_RECONNECT_WARNING,
) -> dict:
    scope_status = gmail_scope_status_payload(connection)
    scope_status.update(
        {
            "hasGmailScope": False,
            "has_gmail_scope": False,
            "needsGmailReconnect": True,
            "needs_gmail_reconnect": True,
        }
    )
    return {
        "run": _serialize_run(run, request),
        "sourceUnavailable": True,
        "source_unavailable": True,
        "source": "gmail",
        "code": GMAIL_INSUFFICIENT_SCOPE_CODE,
        "retryable": False,
        "warning": warning,
        "mode": "backfill",
        "ingested_count": 0,
        "result_size_estimate": 0,
        "next_page_token": None,
        "history_id": "",
        "cursor_reset": False,
        "reused_existing_count": 0,
        "relevance_counts": {
            GmailRelevanceLabel.RELEVANT: 0,
            GmailRelevanceLabel.UPDATE_WORTHY: 0,
            GmailRelevanceLabel.BACKGROUND: 0,
            GmailRelevanceLabel.IRRELEVANT: 0,
            GmailRelevanceLabel.AMBIGUOUS: 0,
            GmailRelevanceLabel.PENDING: 0,
        },
        "message_ids": [],
        **scope_status,
    }


def _gmail_source_unavailable_response(run: ContentFactoryRun, request, *, connection=None) -> Response:
    return Response(
        _gmail_source_unavailable_payload(run, request, connection=connection),
        status=status.HTTP_200_OK,
    )


def _update_run_step(run: ContentFactoryRun, *, step_key: str):
    if run.status == ContentFactoryRunStatus.CANCELLED:
        return
    if run.status == ContentFactoryRunStatus.QUEUED:
        run.status = ContentFactoryRunStatus.RUNNING
    run.current_step = step_key
    run.save(update_fields=["status", "current_step", "updated_at"])


class StartupProfileView(APIView):
    authentication_classes = []
    permission_classes = [HasRooApiKey]

    def post(self, request):
        serializer = StartupProfileUpsertSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        user = get_object_or_404(User, pk=data["user_id"])
        organization, profile = resolve_or_create_profile(domain=data["domain"])
        profile = seed_startup_profile(profile)

        update_fields = []
        for field_name in [
            "company_aliases",
            "domain_aliases",
            "product_names",
            "founder_names",
            "team_names",
            "investor_names",
            "investor_domains",
            "competitor_names",
            "competitor_domains",
            "customer_names",
            "customer_domains",
            "prospect_names",
            "prospect_domains",
            "positive_keywords",
            "negative_keywords",
            "kpi_definitions",
            "default_currency",
            "stage",
            "organization_kind",
            "notes",
        ]:
            if field_name in data and getattr(profile, field_name) != data[field_name]:
                setattr(profile, field_name, data[field_name])
                update_fields.append(field_name)

        if update_fields:
            update_fields.append("updated_at")
            profile.save(update_fields=update_fields)

        binding = bind_user_to_startup(
            user=user,
            organization=organization,
            google_connection=google_connection_for_org(user, organization, adopt_unassigned=True),
            role=data.get("role", ""),
            is_default_for_gmail=bool(data.get("is_default_for_gmail", True)),
        )

        return Response(
            {
                "profile": _serialize_profile(profile),
                "binding": _serialize_binding(binding),
                "google_connected": bool(binding.google_connection),
            },
            status=status.HTTP_200_OK,
        )


class StartupUpdateRunView(APIView):
    authentication_classes = []
    permission_classes = [HasRooApiKey]

    def post(self, request):
        serializer = StartupUpdateRunCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        user = get_object_or_404(User, pk=data["user_id"])
        binding = get_default_binding_for_domain(user=user, domain=data["domain"])
        if binding is None:
            return Response(
                {"error": "No startup binding found for this user and domain."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        input_sources = normalize_startup_update_input_sources(
            data.get("input_sources") or data.get("inputSources") or []
        )
        raw_target_month = data.get("target_month") or data.get("targetMonth")
        try:
            target_month = parse_startup_update_target_month(raw_target_month)
        except ValueError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        google_connection = binding.google_connection or google_connection_for_org(
            binding.user, binding.organization
        )
        input_sources, google_connection, gmail_scope_warnings = coerce_startup_update_sources_for_gmail_scope(
            input_sources,
            google_connection,
        )
        if gmail_required_for_sources(input_sources) and (
            google_connection is None or not has_gmail_read_scope(google_connection)
        ):
            return Response(
                {
                    "error": "The bound user does not have a usable Gmail connection.",
                    "code": GMAIL_INSUFFICIENT_SCOPE_CODE if google_connection else "gmail_connection_required",
                    "sourceUnavailable": bool(google_connection),
                    "source_unavailable": bool(google_connection),
                    "source": "gmail",
                    "retryable": False,
                    "warning": GMAIL_RECONNECT_WARNING if google_connection else "Connect Gmail before selecting Gmail.",
                    **gmail_scope_status_payload(google_connection),
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        if google_connection is not None and binding.google_connection_id != google_connection.id:
            binding.google_connection = google_connection
            binding.save(update_fields=["google_connection", "updated_at"])

        organization = binding.organization
        _, profile = resolve_or_create_profile(domain=organization.domain)
        existing_run = get_open_startup_update_run(
            organization=organization,
            google_connection_id=google_connection.id if google_connection else None,
            target_month=target_month,
            input_sources=input_sources,
        )
        conflicting_run = get_open_startup_update_run(
            organization=organization,
            google_connection_id=google_connection.id if google_connection else None,
            input_sources=input_sources,
        )
        if (
            existing_run is None
            and conflicting_run is not None
            and not startup_update_run_matches_target_month(conflicting_run, target_month)
        ):
            active_target_month = get_startup_update_run_target_month(conflicting_run)
            return Response(
                {
                    "run": _serialize_run(conflicting_run, request),
                    "run_id": conflicting_run.run_id,
                    "status": conflicting_run.status,
                    "current_step": conflicting_run.current_step,
                    "reused_existing_run": True,
                    "target_month_conflict": True,
                    "requested_target_month": target_month.isoformat(),
                    "active_target_month": active_target_month.isoformat() if active_target_month else None,
                    "error": "Another monthly update generation is already active for this startup.",
                    "profile": _serialize_profile(profile),
                    "binding": _serialize_binding(binding),
                },
                status=status.HTTP_200_OK,
            )
        dispatch_required = existing_run is None or _should_dispatch_existing_run(existing_run)
        run = create_startup_update_run(
            organization=organization,
            binding=binding,
            window_months=data.get("window_months", DEFAULT_BACKFILL_MONTHS),
            input_sources=input_sources,
            source_warnings=gmail_scope_warnings,
            target_month=target_month,
        )
        if google_connection is not None and gmail_required_for_sources(input_sources):
            GmailSyncCursor.objects.get_or_create(
                organization=organization,
                google_connection=google_connection,
            )

        payload = {
            "run": _serialize_run(run, request),
            "run_id": run.run_id,
            "status": run.status,
            "current_step": run.current_step,
            "reused_existing_run": existing_run is not None,
            "profile": _serialize_profile(profile),
            "binding": _serialize_binding(binding),
        }
        if dispatch_required:
            dispatch_result = _dispatch_run_to_valley(run)
            if not dispatch_result:
                payload.update(_valley_dispatch_failure_payload(run, dispatch_result))
                return Response(payload, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        return Response(payload, status=status.HTTP_200_OK if existing_run else status.HTTP_201_CREATED)


class StartupUpdateActiveRunView(APIView):
    authentication_classes = []
    permission_classes = [HasRooApiKey]

    def get(self, request):
        domain = normalize_domain(request.query_params.get("domain") or "")
        binding_id = _parse_optional_int(request.query_params.get("binding_id"))
        google_connection_id = _parse_optional_int(request.query_params.get("google_connection_id"))
        input_sources = _input_sources_from_query_params(request.query_params)

        if not domain or binding_id is None:
            return Response(
                {"error": "domain and binding_id query parameters are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        organization = get_object_or_404(Organization, domain=domain)
        binding = get_object_or_404(
            organization.user_startup_bindings.select_related("user", "google_connection"),
            id=binding_id,
        )
        if google_connection_id is None:
            google_connection_id = binding.google_connection_id or getattr(binding.user, "google_connection_id", None)

        run = get_open_startup_update_run(
            organization=organization,
            google_connection_id=google_connection_id,
            input_sources=input_sources,
        )
        if run is None:
            return Response(None, status=status.HTTP_200_OK)

        return Response(_serialize_run_progress(run), status=status.HTTP_200_OK)


class StartupUpdateRunStatusView(APIView):
    authentication_classes = []
    permission_classes = [HasRooApiKey]

    def get(self, request, run_id: str):
        run = _get_run_or_404(run_id)
        return Response(_serialize_run_progress(run), status=status.HTTP_200_OK)


class StartupUpdateIngestNextPageView(APIView):
    authentication_classes = []
    permission_classes = [HasRooApiKey]

    def post(self, request, run_id: str):
        serializer = StartupUpdateIngestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        run = _get_run_or_404(run_id)
        cancelled_response = _reject_if_run_cancelled(run)
        if cancelled_response is not None:
            return cancelled_response
        organization, binding, google_connection, profile = _get_org_and_binding_for_run(run)
        if google_connection is None:
            return _gmail_connection_required_response()
        if not has_gmail_read_scope(google_connection):
            return _gmail_source_unavailable_response(run, request, connection=google_connection)
        _update_run_step(run, step_key="gmail_backfill")
        cursor, _ = GmailSyncCursor.objects.get_or_create(
            organization=organization,
            google_connection=google_connection,
        )

        mode = data.get("mode", "backfill")

        run_request = run.run_request or {}
        start_raw = run_request.get("backfill_window_start")
        end_raw = run_request.get("backfill_window_end")
        if start_raw and end_raw:
            after_dt = datetime.fromisoformat(start_raw)
            before_dt = datetime.fromisoformat(end_raw)
            if timezone.is_naive(after_dt):
                after_dt = timezone.make_aware(after_dt, timezone=dt_timezone.utc)
            if timezone.is_naive(before_dt):
                before_dt = timezone.make_aware(before_dt, timezone=dt_timezone.utc)
        else:
            after_dt, before_dt = default_backfill_window(
                window_months=int(run_request.get("window_months") or DEFAULT_BACKFILL_MONTHS)
            )

        sync_result = None
        try:
            if mode == "incremental":
                start_history_id = data.get("start_history_id") or cursor.last_history_id
                if start_history_id:
                    try:
                        sync_result = sync_history_metadata_page(
                            organization=organization,
                            connection=google_connection,
                            profile=profile,
                            start_history_id=start_history_id,
                            page_token=data.get("page_token"),
                            max_results=data.get("max_results", 250),
                        )
                    except StaleHistoryCursorError:
                        cursor.last_history_id = ""
                        cursor.backfill_completed_at = None
                        cursor.save(update_fields=["last_history_id", "backfill_completed_at", "updated_at"])
                        after_dt, before_dt = default_backfill_window(
                            window_months=int(run_request.get("window_months") or DEFAULT_BACKFILL_MONTHS)
                        )
                        sync_result = sync_message_metadata_page(
                            organization=organization,
                            connection=google_connection,
                            profile=profile,
                            after_dt=after_dt,
                            before_dt=before_dt,
                            page_token=data.get("page_token"),
                            max_results=data.get("max_results", 250),
                        )
                        sync_result["cursor_reset"] = True
                else:
                    after_dt, before_dt = default_backfill_window(
                        window_months=int(run_request.get("window_months") or DEFAULT_BACKFILL_MONTHS)
                    )
                    sync_result = sync_message_metadata_page(
                        organization=organization,
                        connection=google_connection,
                        profile=profile,
                        after_dt=after_dt,
                        before_dt=before_dt,
                        page_token=data.get("page_token"),
                        max_results=data.get("max_results", 250),
                    )
                    sync_result["mode"] = "backfill"
            else:
                sync_result = sync_message_metadata_page(
                    organization=organization,
                    connection=google_connection,
                    profile=profile,
                    after_dt=after_dt,
                    before_dt=before_dt,
                    page_token=data.get("page_token"),
                    max_results=data.get("max_results", 250),
                )
        except Exception as exc:
            if is_gmail_insufficient_permissions_error(exc):
                return _gmail_source_unavailable_response(run, request, connection=google_connection)
            raise

        cursor.backfill_window_start = after_dt
        cursor.backfill_window_end = before_dt
        if sync_result["artifacts"]:
            cursor.last_message_internal_date = max(item.internal_date for item in sync_result["artifacts"])
            cursor.last_synced_internal_date = timezone.now()
            artifact_history_ids = [int(item.history_id) for item in sync_result["artifacts"] if str(item.history_id or "").isdigit()]
            if artifact_history_ids:
                cursor.last_history_id = str(max(artifact_history_ids))
        if sync_result.get("history_id"):
            cursor.last_history_id = sync_result["history_id"]
        if not sync_result.get("next_page_token"):
            cursor.backfill_completed_at = timezone.now()
        cursor.save()

        counts = {
            GmailRelevanceLabel.RELEVANT: 0,
            GmailRelevanceLabel.UPDATE_WORTHY: 0,
            GmailRelevanceLabel.BACKGROUND: 0,
            GmailRelevanceLabel.IRRELEVANT: 0,
            GmailRelevanceLabel.AMBIGUOUS: 0,
            GmailRelevanceLabel.PENDING: 0,
        }
        for artifact in sync_result["artifacts"]:
            counts[artifact.relevance_label] = counts.get(artifact.relevance_label, 0) + 1

        return Response(
            {
                "run": _serialize_run(run, request),
                "mode": sync_result.get("mode", mode),
                "ingested_count": len(sync_result["artifacts"]),
                "result_size_estimate": sync_result["result_size_estimate"],
                "next_page_token": sync_result["next_page_token"],
                "history_id": sync_result.get("history_id", ""),
                "cursor_reset": bool(sync_result.get("cursor_reset", False)),
                "reused_existing_count": int(sync_result.get("reused_existing_count") or 0),
                "relevance_counts": counts,
                "message_ids": [artifact.gmail_message_id for artifact in sync_result["artifacts"]],
            },
            status=status.HTTP_200_OK,
        )


class StartupUpdateHydrateThreadsView(APIView):
    authentication_classes = []
    permission_classes = [HasRooApiKey]

    def post(self, request, run_id: str):
        serializer = StartupUpdateThreadHydrationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        run = _get_run_or_404(run_id)
        cancelled_response = _reject_if_run_cancelled(run)
        if cancelled_response is not None:
            return cancelled_response
        organization, binding, google_connection, profile = _get_org_and_binding_for_run(run)
        if google_connection is None:
            return _gmail_connection_required_response()
        if not has_gmail_read_scope(google_connection):
            payload = _gmail_source_unavailable_payload(run, request, connection=google_connection)
            payload.update({"hydrated_thread_ids": [], "hydrated_count": 0, "attachment_count": 0})
            return Response(payload, status=status.HTTP_200_OK)
        _update_run_step(run, step_key="thread_hydration")

        thread_ids = set(data.get("thread_ids") or [])
        if data.get("message_ids"):
            for artifact in GmailMessageArtifact.objects.filter(
                organization=organization,
                google_connection=google_connection,
                gmail_message_id__in=data["message_ids"],
            ):
                thread_ids.add(artifact.gmail_thread_id)

        if not thread_ids:
            return Response(
                {"error": "At least one thread_id or message_id is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        hydrated = []
        attachment_count = 0
        fetch_attachments = bool(data.get("fetch_attachments", False))
        try:
            for thread_id in sorted(thread_ids):
                thread_artifact = hydrate_thread_artifact(
                    organization=organization,
                    connection=google_connection,
                    thread_id=thread_id,
                    profile=profile,
                    fetch_attachments=fetch_attachments,
                )
                attachment_count += len(thread_artifact.attachment_ids or [])
                hydrated.append(thread_artifact.gmail_thread_id)
        except Exception as exc:
            if is_gmail_insufficient_permissions_error(exc):
                payload = _gmail_source_unavailable_payload(run, request, connection=google_connection)
                payload.update({"hydrated_thread_ids": hydrated, "hydrated_count": len(hydrated), "attachment_count": attachment_count})
                return Response(payload, status=status.HTTP_200_OK)
            raise

        return Response(
            {
                "run": _serialize_run(run, request),
                "hydrated_thread_ids": hydrated,
                "hydrated_count": len(hydrated),
                "attachment_count": attachment_count,
            },
            status=status.HTTP_200_OK,
        )


class StartupUpdateHydrationCandidatesView(APIView):
    authentication_classes = []
    permission_classes = [HasRooApiKey]

    def get(self, request, run_id: str):
        serializer = StartupUpdateBatchQuerySerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        limit = serializer.validated_data["limit"]

        run = _get_run_or_404(run_id)
        cancelled_response = _reject_if_run_cancelled(run)
        if cancelled_response is not None:
            return cancelled_response
        organization, binding, google_connection, profile = _get_org_and_binding_for_run(run)
        if google_connection is None:
            return _gmail_connection_required_response()
        _update_run_step(run, step_key="thread_hydration")
        eligible_thread_ids = set(
            _get_prioritized_run_thread_ids(
                run=run,
                organization=organization,
                google_connection=google_connection,
            )
        )
        if not eligible_thread_ids:
            return Response(
                {
                    "run": _serialize_run(run, request),
                    "count": 0,
                    "threads": [],
                },
                status=status.HTTP_200_OK,
            )

        hydrated_by_thread = {
            item["gmail_thread_id"]: item["hydration_status"]
            for item in GmailThreadArtifact.objects.filter(
                organization=organization,
                google_connection=google_connection,
            ).values("gmail_thread_id", "hydration_status")
        }

        seen_thread_ids = set()
        candidates = []
        queryset = _apply_run_window(
            GmailMessageArtifact.objects.filter(
                organization=organization,
                google_connection=google_connection,
                relevance_label__in=EXTRACTABLE_RELEVANCE_LABELS,
                needs_thread_context=True,
                gmail_thread_id__in=eligible_thread_ids,
            ).order_by("-internal_date"),
            run,
            "internal_date",
        )

        for artifact in queryset:
            if artifact.gmail_thread_id in seen_thread_ids:
                continue
            thread_status = hydrated_by_thread.get(artifact.gmail_thread_id)
            if thread_status == ArtifactProcessingStatus.HYDRATED:
                continue
            seen_thread_ids.add(artifact.gmail_thread_id)
            candidates.append(
                {
                    "gmail_thread_id": artifact.gmail_thread_id,
                    "source_message_ids": [artifact.gmail_message_id],
                    "latest_message_internal_date": artifact.internal_date.isoformat(),
                }
            )
            if len(candidates) >= limit:
                break

        return Response(
            {
                "run": _serialize_run(run, request),
                "count": len(candidates),
                "threads": candidates,
            },
            status=status.HTTP_200_OK,
        )


class StartupUpdateClassificationBatchView(APIView):
    authentication_classes = []
    permission_classes = [HasRooApiKey]

    def get(self, request, run_id: str):
        serializer = StartupUpdateBatchQuerySerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        limit = serializer.validated_data["limit"]

        run = _get_run_or_404(run_id)
        cancelled_response = _reject_if_run_cancelled(run)
        if cancelled_response is not None:
            return cancelled_response
        organization, binding, google_connection, profile = _get_org_and_binding_for_run(run)
        if google_connection is None:
            return _gmail_connection_required_response()
        _update_run_step(run, step_key="relevance_classification")

        queryset = _apply_run_window(
            GmailMessageArtifact.objects.filter(
                organization=organization,
                google_connection=google_connection,
                relevance_label__in=[GmailRelevanceLabel.AMBIGUOUS, GmailRelevanceLabel.PENDING],
                classified_at__isnull=True,
            ).order_by("-heuristic_score", "-internal_date"),
            run,
            "internal_date",
        )[:limit]

        payload = []
        for artifact in queryset:
            payload.append(
                {
                    "gmail_message_id": artifact.gmail_message_id,
                    "gmail_thread_id": artifact.gmail_thread_id,
                    "internal_date": artifact.internal_date.isoformat(),
                    "subject": artifact.subject,
                    "from_address": artifact.from_address,
                    "to_addresses": artifact.to_addresses or [],
                    "cc_addresses": artifact.cc_addresses or [],
                    "snippet": artifact.snippet,
                    "cleaned_text": artifact.cleaned_text,
                    "attachment_manifest": artifact.attachment_manifest or [],
                    "heuristic_score": artifact.heuristic_score,
                    "heuristic_reasons": artifact.heuristic_reasons or [],
                }
            )

        return Response(
            {
                "run": _serialize_run(run, request),
                "count": len(payload),
                "messages": payload,
            },
            status=status.HTTP_200_OK,
        )


class StartupUpdateClassificationResultsView(APIView):
    authentication_classes = []
    permission_classes = [HasRooApiKey]

    def post(self, request, run_id: str):
        serializer = ClassificationResultsSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        run = _get_run_or_404(run_id)
        cancelled_response = _reject_if_run_cancelled(run)
        if cancelled_response is not None:
            return cancelled_response
        organization, binding, google_connection, profile = _get_org_and_binding_for_run(run)
        if google_connection is None:
            return _gmail_connection_required_response()
        _update_run_step(run, step_key="relevance_classification")

        updated = 0
        for item in serializer.validated_data["results"]:
            artifact = get_object_or_404(
                GmailMessageArtifact,
                organization=organization,
                google_connection=google_connection,
                gmail_message_id=item["gmail_message_id"],
            )
            artifact.relevance_label = item["relevance_label"]
            artifact.relevance_score = item.get("relevance_score", 0.0)
            artifact.relevance_reason = item.get("relevance_reason", "")
            artifact.needs_thread_context = bool(item.get("needs_thread_context", False))
            artifact.classified_at = timezone.now()
            artifact.save(
                update_fields=[
                    "relevance_label",
                    "relevance_score",
                    "relevance_reason",
                    "needs_thread_context",
                    "classified_at",
                    "updated_at",
                ]
            )
            updated += 1

        return Response(
            {
                "run": _serialize_run(run, request),
                "updated_count": updated,
            },
            status=status.HTTP_200_OK,
        )


class StartupUpdateExtractionBatchView(APIView):
    authentication_classes = []
    permission_classes = [HasRooApiKey]

    def get(self, request, run_id: str):
        serializer = StartupUpdateBatchQuerySerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        limit = serializer.validated_data["limit"]

        run = _get_run_or_404(run_id)
        cancelled_response = _reject_if_run_cancelled(run)
        if cancelled_response is not None:
            return cancelled_response
        organization, binding, google_connection, profile = _get_org_and_binding_for_run(run)
        if google_connection is None:
            return _gmail_connection_required_response()
        if not has_gmail_read_scope(google_connection):
            payload = _gmail_source_unavailable_payload(run, request, connection=google_connection)
            payload.update({"count": 0, "threads": []})
            return Response(payload, status=status.HTTP_200_OK)
        _update_run_step(run, step_key="event_extraction")
        eligible_thread_ids = _get_prioritized_run_thread_ids(
            run=run,
            organization=organization,
            google_connection=google_connection,
        )

        if not eligible_thread_ids:
            return Response(
                {
                    "run": _serialize_run(run, request),
                    "count": 0,
                    "threads": [],
                },
                status=status.HTTP_200_OK,
            )

        queryset = _apply_run_window(
            GmailThreadArtifact.objects.filter(
                organization=organization,
                google_connection=google_connection,
                gmail_thread_id__in=eligible_thread_ids,
                hydration_status=ArtifactProcessingStatus.HYDRATED,
                extraction_status__in=[ArtifactProcessingStatus.PENDING, ArtifactProcessingStatus.HYDRATED],
            ).order_by("-latest_message_internal_date", "-updated_at"),
            run,
            "latest_message_internal_date",
        )[:limit]

        bundles = []
        try:
            for thread_artifact in queryset:
                attachments = ensure_thread_attachments_hydrated(
                    organization=organization,
                    connection=google_connection,
                    thread_artifact=thread_artifact,
                )
                bundles.append(
                    compact_gmail_thread_bundle(
                        thread_artifact,
                        profile=profile,
                        attachments=[_serialize_attachment(attachment) for attachment in attachments],
                    )
                )
        except Exception as exc:
            if is_gmail_insufficient_permissions_error(exc):
                payload = _gmail_source_unavailable_payload(run, request, connection=google_connection)
                payload.update({"count": 0, "threads": []})
                return Response(payload, status=status.HTTP_200_OK)
            raise

        return Response(
            {
                "run": _serialize_run(run, request),
                "count": len(bundles),
                "threads": bundles,
            },
            status=status.HTTP_200_OK,
        )


class StartupUpdateExtractionResultsView(APIView):
    authentication_classes = []
    permission_classes = [HasRooApiKey]

    @transaction.atomic
    def post(self, request, run_id: str):
        serializer = ExtractionResultsSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        run = _get_run_or_404(run_id)
        cancelled_response = _reject_if_run_cancelled(run)
        if cancelled_response is not None:
            return cancelled_response
        organization, binding, google_connection, profile = _get_org_and_binding_for_run(run)
        if google_connection is None:
            return _gmail_connection_required_response()
        _update_run_step(run, step_key="event_extraction")

        backups = get_startup_update_run_cancel_backups(run)
        backups_changed = False
        event_count = 0
        metric_count = 0
        attachment_count = 0
        for item in serializer.validated_data["results"]:
            thread_artifact = get_object_or_404(
                GmailThreadArtifact,
                organization=organization,
                google_connection=google_connection,
                gmail_thread_id=item["gmail_thread_id"],
            )
            thread_artifact.extraction_status = item.get(
                "extraction_status",
                ArtifactProcessingStatus.PROCESSED,
            )
            thread_artifact.extracted_at = timezone.now()
            thread_artifact.save(update_fields=["extraction_status", "extracted_at", "updated_at"])

            for attachment_update in item.get("attachment_updates", []):
                attachment = get_object_or_404(
                    GmailAttachmentArtifact,
                    organization=organization,
                    id=attachment_update["id"],
                )
                attachment.extracted_text = attachment_update.get("extracted_text", "")
                attachment.extraction_status = attachment_update.get(
                    "extraction_status",
                    ArtifactProcessingStatus.PROCESSED,
                )
                attachment.parse_notes = attachment_update.get("parse_notes", "")
                attachment.extracted_at = timezone.now()
                attachment.save(
                    update_fields=[
                        "extracted_text",
                        "extraction_status",
                        "parse_notes",
                        "extracted_at",
                        "updated_at",
                    ]
                )
                attachment_count += 1

            for event_data in item.get("events", []):
                existing_event = StartupEvent.objects.filter(
                    organization=organization,
                    canonical_key=event_data["canonical_key"],
                ).first()
                if existing_event is not None:
                    backups_changed = _backup_event_if_needed(run, existing_event, backups) or backups_changed
                StartupEvent.objects.update_or_create(
                    organization=organization,
                    canonical_key=event_data["canonical_key"],
                    defaults={
                        "run": run,
                        "event_type": event_data["event_type"],
                        "title": event_data["title"],
                        "summary": event_data.get("summary", ""),
                        "event_date": event_data.get("event_date"),
                        "month_bucket": event_data["month_bucket"],
                        "date_precision": event_data.get("date_precision"),
                        "sentiment": event_data.get("sentiment", ""),
                        "investor_importance": event_data.get("investor_importance", 3),
                        "quantitative_facts": event_data.get("quantitative_facts", []),
                        "evidence_message_ids": event_data.get("evidence_message_ids", []),
                        "evidence_attachment_ids": event_data.get("evidence_attachment_ids", []),
                        "source_thread_ids": event_data.get("source_thread_ids", []),
                        "confidence": event_data.get("confidence", 0.0),
                        "status": event_data.get("status", "open"),
                        "needs_review": bool(event_data.get("needs_review", False)),
                        "merge_notes": event_data.get("merge_notes", ""),
                    },
                )
                event_count += 1

            for metric_data in item.get("metrics", []):
                existing_metric = StartupMetricObservation.objects.filter(
                    organization=organization,
                    source_thread=thread_artifact,
                    metric_key=metric_data["metric_key"],
                    period_month=metric_data["period_month"],
                    value_text=metric_data["value_text"],
                ).first()
                if existing_metric is not None:
                    backups_changed = _backup_metric_if_needed(run, existing_metric, backups) or backups_changed
                StartupMetricObservation.objects.update_or_create(
                    organization=organization,
                    source_thread=thread_artifact,
                    metric_key=metric_data["metric_key"],
                    period_month=metric_data["period_month"],
                    value_text=metric_data["value_text"],
                    defaults={
                        "run": run,
                        "metric_name": metric_data["metric_name"],
                        "value_number": metric_data.get("value_number"),
                        "unit": metric_data.get("unit", ""),
                        "observed_at": metric_data.get("observed_at"),
                        "confidence": metric_data.get("confidence", 0.0),
                        "evidence_message_ids": metric_data.get("evidence_message_ids", []),
                        "evidence_attachment_ids": metric_data.get("evidence_attachment_ids", []),
                        "source_provider": "gmail",
                        "source_record_ids": [],
                        "source_metadata": {"source": "gmail_thread_extraction"},
                        "summary": metric_data.get("summary", ""),
                    },
                )
                metric_count += 1

        if backups_changed:
            set_startup_update_run_cancel_backups(run, backups)
            run.save(update_fields=["result", "updated_at"])

        return Response(
            {
                "run": _serialize_run(run, request),
                "event_count": event_count,
                "metric_count": metric_count,
                "attachment_count": attachment_count,
            },
            status=status.HTTP_200_OK,
        )


def _slack_thread_public_id(thread: SlackThreadArtifact) -> str:
    return f"slack:{thread.channel_id}:{thread.thread_ts}"


def _run_slack_channel_ids(run: ContentFactoryRun) -> list[str]:
    raw_channel_ids = (run.run_request or {}).get("slack_channel_ids") or []
    if not isinstance(raw_channel_ids, (list, tuple)):
        return []
    return [str(item or "").strip() for item in raw_channel_ids if str(item or "").strip()]


class StartupUpdateSlackBackfillView(APIView):
    authentication_classes = []
    permission_classes = [HasRooApiKey]

    def post(self, request, run_id: str):
        run = _get_run_or_404(run_id)
        cancelled_response = _reject_if_run_cancelled(run)
        if cancelled_response is not None:
            return cancelled_response
        organization, binding, google_connection, profile = _get_org_and_binding_for_run(run)
        _update_run_step(run, step_key="slack_backfill")

        channel_ids = _run_slack_channel_ids(run)
        connection = (
            binding.user.external_service_connections.filter(
                provider="slack",
                organization=organization,
            )
            .exclude(status="disconnected")
            .order_by("-updated_at", "-id")
            .first()
        )
        if connection is None:
            return Response({"error": "Slack is not connected."}, status=status.HTTP_400_BAD_REQUEST)

        if not channel_ids:
            channel_ids = [
                selection.channel_id
                for selection in SlackChannelSelection.objects.filter(
                    connection=connection,
                    selected=True,
                )
            ]
        try:
            sync_result = sync_slack_connection_page(
                connection,
                run_id=run.run_id,
                channel_ids=channel_ids,
            )
        except ConnectorRateLimitError as exc:
            sync_result = {
                "connectionId": connection.id,
                "connection_id": connection.id,
                "provider": "slack",
                "status": "rate_limited",
                "messagesSynced": 0,
                "messages_synced": 0,
                "threadsTouched": 0,
                "threads_touched": 0,
                "channels": [],
                "has_more": True,
                "retry_after_seconds": exc.retry_after_seconds,
            }
        except ConnectorConfigurationError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except ConnectorOAuthError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except requests.RequestException as exc:
            return Response(
                {
                    "error": "Slack backfill timed out or failed while contacting Slack.",
                    "detail": str(exc),
                },
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        run_request = dict(run.run_request or {})
        run_request["slack_channel_ids"] = channel_ids
        external_context = dict(run_request.get("external_context") or {})
        slack_context = dict(external_context.get("slack") or {})
        slack_context["selected_channel_ids"] = channel_ids
        slack_context["last_sync"] = sync_result
        external_context["slack"] = slack_context
        run_request["external_context"] = external_context
        run.run_request = run_request
        run.save(update_fields=["run_request", "updated_at"])

        return Response(
            {
                "run": _serialize_run(run, request),
                **sync_result,
                "has_more": bool(sync_result.get("has_more")),
            },
            status=status.HTTP_200_OK,
        )


def _slack_thread_from_public_id(slack_thread_id: str) -> Optional[Tuple[str, str]]:
    parts = str(slack_thread_id or "").split(":", 2)
    if len(parts) != 3 or parts[0] != "slack":
        return None
    return parts[1], parts[2]


def _update_slack_filtering_summary(
    *,
    run: ContentFactoryRun,
    organization: Organization,
    channel_ids: list[str],
    batch_context: Optional[dict] = None,
) -> None:
    queryset = SlackThreadArtifact.objects.filter(organization=organization)
    if channel_ids:
        queryset = queryset.filter(channel_id__in=channel_ids)
    queryset = _apply_run_window(queryset, run, "latest_message_at")
    summary = {
        "threads_scanned": queryset.count(),
        "classified": queryset.exclude(classified_at__isnull=True).count(),
        "relevant": queryset.filter(relevance_label__in=UPDATE_WORTHY_RELEVANCE_LABELS).count(),
        "ambiguous": queryset.filter(relevance_label=GmailRelevanceLabel.AMBIGUOUS).count(),
        "irrelevant": queryset.filter(relevance_label=GmailRelevanceLabel.IRRELEVANT).count(),
        "needs_extraction": queryset.filter(needs_extraction=True).count(),
        "extracted": queryset.filter(extraction_status=ArtifactProcessingStatus.PROCESSED).count(),
    }
    if batch_context:
        summary["last_batch"] = batch_context

    run_request = dict(run.run_request or {})
    external_context = dict(run_request.get("external_context") or {})
    slack_context = dict(external_context.get("slack") or {})
    slack_context["filtering_summary"] = summary
    external_context["slack"] = slack_context
    run_request["external_context"] = external_context
    run.run_request = run_request
    run.save(update_fields=["run_request", "updated_at"])


class StartupUpdateSlackClassificationBatchView(APIView):
    authentication_classes = []
    permission_classes = [HasRooApiKey]

    def get(self, request, run_id: str):
        serializer = StartupUpdateBatchQuerySerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        limit = serializer.validated_data["limit"]

        run = _get_run_or_404(run_id)
        cancelled_response = _reject_if_run_cancelled(run)
        if cancelled_response is not None:
            return cancelled_response
        organization, binding, google_connection, profile = _get_org_and_binding_for_run(run)
        _update_run_step(run, step_key="slack_relevance_classification")

        channel_ids = _run_slack_channel_ids(run)
        queryset = SlackThreadArtifact.objects.filter(
            organization=organization,
            extraction_status__in=[ArtifactProcessingStatus.PENDING, ArtifactProcessingStatus.HYDRATED],
            relevance_label__in=[GmailRelevanceLabel.PENDING, GmailRelevanceLabel.AMBIGUOUS],
            classified_at__isnull=True,
        )
        if channel_ids:
            queryset = queryset.filter(channel_id__in=channel_ids)
        queryset = _apply_run_window(
            queryset.order_by("-heuristic_score", "-latest_message_at", "-updated_at"),
            run,
            "latest_message_at",
        )

        bundles = []
        hard_filtered_count = 0
        for thread in queryset.iterator(chunk_size=200):
            _score, _reasons, _label, hard_filtered = apply_slack_profile_scoring(profile, thread)
            if hard_filtered:
                hard_filtered_count += 1
                continue
            bundles.append(compact_slack_thread_bundle(thread))
            if len(bundles) >= limit:
                break

        _update_slack_filtering_summary(
            run=run,
            organization=organization,
            channel_ids=channel_ids,
            batch_context={
                "stage": "classification_batch",
                "returned": len(bundles),
                "hard_filtered": hard_filtered_count,
            },
        )
        return Response(
            {
                "run": _serialize_run(run, request),
                "count": len(bundles),
                "threads": bundles,
                "hard_filtered_count": hard_filtered_count,
            },
            status=status.HTTP_200_OK,
        )


class StartupUpdateSlackClassificationResultsView(APIView):
    authentication_classes = []
    permission_classes = [HasRooApiKey]

    def post(self, request, run_id: str):
        serializer = SlackClassificationResultsSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        run = _get_run_or_404(run_id)
        cancelled_response = _reject_if_run_cancelled(run)
        if cancelled_response is not None:
            return cancelled_response
        organization, binding, google_connection, profile = _get_org_and_binding_for_run(run)
        _update_run_step(run, step_key="slack_relevance_classification")

        updated = 0
        for item in serializer.validated_data["results"]:
            parsed = _slack_thread_from_public_id(item["slack_thread_id"])
            if parsed is None:
                return Response(
                    {"error": f"Invalid Slack thread id: {item['slack_thread_id']}"},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            channel_id, thread_ts = parsed
            thread = get_object_or_404(
                SlackThreadArtifact,
                organization=organization,
                channel_id=channel_id,
                thread_ts=thread_ts,
            )
            label = item["relevance_label"]
            thread.relevance_label = label
            thread.relevance_score = item.get("relevance_score", 0.0)
            thread.relevance_reason = item.get("relevance_reason", "")
            requested_extraction = item.get("needs_extraction")
            if requested_extraction is None:
                requested_extraction = label in {
                    *UPDATE_WORTHY_RELEVANCE_LABELS,
                    GmailRelevanceLabel.AMBIGUOUS,
                }
            thread.needs_extraction = bool(requested_extraction) and label in {
                *UPDATE_WORTHY_RELEVANCE_LABELS,
                GmailRelevanceLabel.AMBIGUOUS,
            }
            thread.extraction_hints = item.get("extraction_hints") or {}
            thread.classified_at = timezone.now()
            if label == GmailRelevanceLabel.IRRELEVANT:
                thread.needs_extraction = False
                thread.extraction_status = ArtifactProcessingStatus.UNSUPPORTED
            elif thread.extraction_status == ArtifactProcessingStatus.UNSUPPORTED:
                thread.extraction_status = ArtifactProcessingStatus.HYDRATED
            thread.save(
                update_fields=[
                    "relevance_label",
                    "relevance_score",
                    "relevance_reason",
                    "needs_extraction",
                    "extraction_hints",
                    "classified_at",
                    "extraction_status",
                    "updated_at",
                ]
            )
            updated += 1

        _update_slack_filtering_summary(
            run=run,
            organization=organization,
            channel_ids=_run_slack_channel_ids(run),
            batch_context={"stage": "classification_results", "updated": updated},
        )
        return Response(
            {
                "run": _serialize_run(run, request),
                "updated_count": updated,
            },
            status=status.HTTP_200_OK,
        )


class StartupUpdateSlackExtractionBatchView(APIView):
    authentication_classes = []
    permission_classes = [HasRooApiKey]

    def get(self, request, run_id: str):
        serializer = StartupUpdateBatchQuerySerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        limit = serializer.validated_data["limit"]

        run = _get_run_or_404(run_id)
        cancelled_response = _reject_if_run_cancelled(run)
        if cancelled_response is not None:
            return cancelled_response
        organization, binding, google_connection, profile = _get_org_and_binding_for_run(run)
        _update_run_step(run, step_key="slack_event_extraction")

        channel_ids = _run_slack_channel_ids(run)
        queryset = SlackThreadArtifact.objects.filter(
            organization=organization,
            extraction_status__in=[ArtifactProcessingStatus.PENDING, ArtifactProcessingStatus.HYDRATED],
            relevance_label__in=EXTRACTABLE_RELEVANCE_LABELS,
            needs_extraction=True,
        )
        if channel_ids:
            queryset = queryset.filter(channel_id__in=channel_ids)
        queryset = _apply_run_window(
            queryset.order_by("-relevance_score", "-heuristic_score", "-latest_message_at", "-updated_at"),
            run,
            "latest_message_at",
        )[:limit]

        bundles = [compact_slack_thread_bundle(thread) for thread in queryset]
        _update_slack_filtering_summary(
            run=run,
            organization=organization,
            channel_ids=channel_ids,
            batch_context={"stage": "extraction_batch", "returned": len(bundles)},
        )
        return Response(
            {
                "run": _serialize_run(run, request),
                "count": len(bundles),
                "threads": bundles,
            },
            status=status.HTTP_200_OK,
        )


class StartupUpdateSlackExtractionResultsView(APIView):
    authentication_classes = []
    permission_classes = [HasRooApiKey]

    @transaction.atomic
    def post(self, request, run_id: str):
        serializer = SlackExtractionResultsSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        run = _get_run_or_404(run_id)
        cancelled_response = _reject_if_run_cancelled(run)
        if cancelled_response is not None:
            return cancelled_response
        organization, binding, google_connection, profile = _get_org_and_binding_for_run(run)
        _update_run_step(run, step_key="slack_event_extraction")

        backups = get_startup_update_run_cancel_backups(run)
        backups_changed = False
        event_count = 0
        metric_count = 0
        for item in serializer.validated_data["results"]:
            slack_thread_id = item["slack_thread_id"]
            parts = slack_thread_id.split(":", 2)
            if len(parts) != 3 or parts[0] != "slack":
                return Response({"error": f"Invalid Slack thread id: {slack_thread_id}"}, status=status.HTTP_400_BAD_REQUEST)
            _prefix, channel_id, thread_ts = parts
            thread_artifact = get_object_or_404(
                SlackThreadArtifact,
                organization=organization,
                channel_id=channel_id,
                thread_ts=thread_ts,
            )
            thread_artifact.extraction_status = item.get(
                "extraction_status",
                ArtifactProcessingStatus.PROCESSED,
            )
            thread_artifact.extracted_at = timezone.now()
            thread_artifact.last_error = ""
            thread_artifact.save(update_fields=["extraction_status", "extracted_at", "last_error", "updated_at"])

            source_record_ids = list(thread_artifact.source_message_ids or [])
            source_metadata = {
                "source": "slack_thread_extraction",
                "slack_thread_id": slack_thread_id,
                "channel_id": thread_artifact.channel_id,
                "channel_name": thread_artifact.channel_name,
                "thread_ts": thread_artifact.thread_ts,
            }

            for event_data in item.get("events", []):
                existing_event = StartupEvent.objects.filter(
                    organization=organization,
                    canonical_key=event_data["canonical_key"],
                ).first()
                if existing_event is not None:
                    backups_changed = _backup_event_if_needed(run, existing_event, backups) or backups_changed
                StartupEvent.objects.update_or_create(
                    organization=organization,
                    canonical_key=event_data["canonical_key"],
                    defaults={
                        "run": run,
                        "event_type": event_data["event_type"],
                        "title": event_data["title"],
                        "summary": event_data.get("summary", ""),
                        "event_date": event_data.get("event_date"),
                        "month_bucket": event_data["month_bucket"],
                        "date_precision": event_data.get("date_precision"),
                        "sentiment": event_data.get("sentiment", ""),
                        "investor_importance": event_data.get("investor_importance", 3),
                        "quantitative_facts": event_data.get("quantitative_facts", []),
                        "evidence_message_ids": event_data.get("evidence_message_ids", []),
                        "evidence_attachment_ids": event_data.get("evidence_attachment_ids", []),
                        "source_thread_ids": event_data.get("source_thread_ids", []) or [slack_thread_id],
                        "confidence": event_data.get("confidence", 0.0),
                        "status": event_data.get("status", "open"),
                        "needs_review": bool(event_data.get("needs_review", False)),
                        "merge_notes": event_data.get("merge_notes", ""),
                    },
                )
                event_count += 1

            for metric_data in item.get("metrics", []):
                existing_metric = StartupMetricObservation.objects.filter(
                    organization=organization,
                    source_provider="slack",
                    metric_key=metric_data["metric_key"],
                    period_month=metric_data["period_month"],
                    value_text=metric_data["value_text"],
                ).first()
                if existing_metric is not None:
                    backups_changed = _backup_metric_if_needed(run, existing_metric, backups) or backups_changed
                StartupMetricObservation.objects.update_or_create(
                    organization=organization,
                    source_thread=None,
                    source_provider="slack",
                    metric_key=metric_data["metric_key"],
                    period_month=metric_data["period_month"],
                    value_text=metric_data["value_text"],
                    defaults={
                        "run": run,
                        "metric_name": metric_data["metric_name"],
                        "value_number": metric_data.get("value_number"),
                        "unit": metric_data.get("unit", ""),
                        "observed_at": metric_data.get("observed_at"),
                        "confidence": metric_data.get("confidence", 0.0),
                        "evidence_message_ids": metric_data.get("evidence_message_ids", []),
                        "evidence_attachment_ids": metric_data.get("evidence_attachment_ids", []),
                        "source_record_ids": source_record_ids,
                        "source_metadata": source_metadata,
                        "summary": metric_data.get("summary", ""),
                    },
                )
                metric_count += 1

        if backups_changed:
            set_startup_update_run_cancel_backups(run, backups)
            run.save(update_fields=["result", "updated_at"])

        return Response(
            {
                "run": _serialize_run(run, request),
                "event_count": event_count,
                "metric_count": metric_count,
            },
            status=status.HTTP_200_OK,
        )


def _linear_project_public_id(project: LinearProjectArtifact) -> str:
    return f"linear:project:{project.linear_project_id}"


def _linear_project_from_public_id(linear_project_id: str) -> str:
    raw = str(linear_project_id or "").strip()
    prefix = "linear:project:"
    if raw.startswith(prefix):
        return raw[len(prefix):]
    return raw


def _run_linear_project_ids(run: ContentFactoryRun) -> list[str]:
    raw_project_ids = (run.run_request or {}).get("linear_project_ids") or []
    if not isinstance(raw_project_ids, (list, tuple)):
        return []
    return [str(item or "").strip() for item in raw_project_ids if str(item or "").strip()]


def _update_linear_filtering_summary(
    *,
    run: ContentFactoryRun,
    organization: Organization,
    project_ids: list[str],
    batch_context: Optional[dict] = None,
) -> None:
    queryset = LinearProjectArtifact.objects.filter(organization=organization)
    if project_ids:
        queryset = queryset.filter(linear_project_id__in=project_ids)
    summary = {
        "projects_scanned": queryset.count(),
        "classified": queryset.exclude(classified_at__isnull=True).count(),
        "relevant": queryset.filter(relevance_label__in=UPDATE_WORTHY_RELEVANCE_LABELS).count(),
        "ambiguous": queryset.filter(relevance_label=GmailRelevanceLabel.AMBIGUOUS).count(),
        "irrelevant": queryset.filter(relevance_label=GmailRelevanceLabel.IRRELEVANT).count(),
        "needs_extraction": queryset.filter(needs_extraction=True).count(),
        "extracted": queryset.filter(extraction_status=ArtifactProcessingStatus.PROCESSED).count(),
    }
    if batch_context:
        summary["last_batch"] = batch_context

    run_request = dict(run.run_request or {})
    external_context = dict(run_request.get("external_context") or {})
    linear_context = dict(external_context.get("linear") or {})
    linear_context["filtering_summary"] = summary
    external_context["linear"] = linear_context
    run_request["external_context"] = external_context
    run.run_request = run_request
    run.save(update_fields=["run_request", "updated_at"])


class StartupUpdateLinearBackfillView(APIView):
    authentication_classes = []
    permission_classes = [HasRooApiKey]

    def post(self, request, run_id: str):
        run = _get_run_or_404(run_id)
        cancelled_response = _reject_if_run_cancelled(run)
        if cancelled_response is not None:
            return cancelled_response
        organization, binding, google_connection, profile = _get_org_and_binding_for_run(run)
        _update_run_step(run, step_key="linear_backfill")

        project_ids = _run_linear_project_ids(run)
        connection = (
            binding.user.external_service_connections.filter(
                provider=ExternalServiceProvider.LINEAR,
                organization=organization,
            )
            .exclude(status="disconnected")
            .order_by("-updated_at", "-id")
            .first()
        )
        if connection is None:
            return Response({"error": "Linear is not connected."}, status=status.HTTP_400_BAD_REQUEST)

        if not project_ids:
            project_ids = [
                selection.linear_project_id
                for selection in LinearProjectSelection.objects.filter(
                    connection=connection,
                    selected=True,
                )
            ]
        try:
            sync_result = sync_linear_connection_page(
                connection,
                run_id=run.run_id,
                project_ids=project_ids,
            )
        except ConnectorRateLimitError as exc:
            sync_result = {
                "connectionId": connection.id,
                "connection_id": connection.id,
                "provider": "linear",
                "status": "rate_limited",
                "projectsSynced": 0,
                "projects_synced": 0,
                "issuesSynced": 0,
                "issues_synced": 0,
                "updatesSynced": 0,
                "updates_synced": 0,
                "projects": [],
                "has_more": True,
                "retry_after_seconds": exc.retry_after_seconds,
            }
        except ConnectorConfigurationError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except ConnectorOAuthError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except requests.RequestException as exc:
            return Response(
                {
                    "error": "Linear backfill timed out or failed while contacting Linear.",
                    "detail": str(exc),
                },
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        run_request = dict(run.run_request or {})
        run_request["linear_project_ids"] = project_ids
        external_context = dict(run_request.get("external_context") or {})
        linear_context = dict(external_context.get("linear") or {})
        linear_context["selected_project_ids"] = project_ids
        linear_context["last_sync"] = sync_result
        external_context["linear"] = linear_context
        run_request["external_context"] = external_context
        run.run_request = run_request
        run.save(update_fields=["run_request", "updated_at"])

        return Response(
            {
                "run": _serialize_run(run, request),
                **sync_result,
                "has_more": bool(sync_result.get("has_more")),
            },
            status=status.HTTP_200_OK,
        )


class StartupUpdateLinearClassificationBatchView(APIView):
    authentication_classes = []
    permission_classes = [HasRooApiKey]

    def get(self, request, run_id: str):
        serializer = StartupUpdateBatchQuerySerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        limit = serializer.validated_data["limit"]

        run = _get_run_or_404(run_id)
        cancelled_response = _reject_if_run_cancelled(run)
        if cancelled_response is not None:
            return cancelled_response
        organization, binding, google_connection, profile = _get_org_and_binding_for_run(run)
        _update_run_step(run, step_key="linear_relevance_classification")

        project_ids = _run_linear_project_ids(run)
        queryset = LinearProjectArtifact.objects.filter(
            organization=organization,
            extraction_status__in=[ArtifactProcessingStatus.PENDING, ArtifactProcessingStatus.HYDRATED],
            relevance_label__in=[GmailRelevanceLabel.PENDING, GmailRelevanceLabel.AMBIGUOUS],
            classified_at__isnull=True,
        )
        if project_ids:
            queryset = queryset.filter(linear_project_id__in=project_ids)
        queryset = queryset.order_by("-updated_at", "name")

        bundles = [compact_linear_project_bundle(project) for project in queryset[:limit]]
        _update_linear_filtering_summary(
            run=run,
            organization=organization,
            project_ids=project_ids,
            batch_context={"stage": "classification_batch", "returned": len(bundles)},
        )
        return Response(
            {
                "run": _serialize_run(run, request),
                "count": len(bundles),
                "projects": bundles,
            },
            status=status.HTTP_200_OK,
        )


class StartupUpdateLinearClassificationResultsView(APIView):
    authentication_classes = []
    permission_classes = [HasRooApiKey]

    def post(self, request, run_id: str):
        serializer = LinearClassificationResultsSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        run = _get_run_or_404(run_id)
        cancelled_response = _reject_if_run_cancelled(run)
        if cancelled_response is not None:
            return cancelled_response
        organization, binding, google_connection, profile = _get_org_and_binding_for_run(run)
        _update_run_step(run, step_key="linear_relevance_classification")

        updated = 0
        for item in serializer.validated_data["results"]:
            project_id = _linear_project_from_public_id(item["linear_project_id"])
            project = get_object_or_404(
                LinearProjectArtifact,
                organization=organization,
                linear_project_id=project_id,
            )
            label = item["relevance_label"]
            project.relevance_label = label
            project.relevance_score = item.get("relevance_score", 0.0)
            project.relevance_reason = item.get("relevance_reason", "")
            requested_extraction = item.get("needs_extraction")
            if requested_extraction is None:
                requested_extraction = label in {
                    *UPDATE_WORTHY_RELEVANCE_LABELS,
                    GmailRelevanceLabel.AMBIGUOUS,
                }
            project.needs_extraction = bool(requested_extraction) and label in {
                *UPDATE_WORTHY_RELEVANCE_LABELS,
                GmailRelevanceLabel.AMBIGUOUS,
            }
            project.extraction_hints = item.get("extraction_hints") or {}
            project.classified_at = timezone.now()
            if label == GmailRelevanceLabel.IRRELEVANT:
                project.needs_extraction = False
                project.extraction_status = ArtifactProcessingStatus.UNSUPPORTED
            elif project.extraction_status == ArtifactProcessingStatus.UNSUPPORTED:
                project.extraction_status = ArtifactProcessingStatus.HYDRATED
            project.save(
                update_fields=[
                    "relevance_label",
                    "relevance_score",
                    "relevance_reason",
                    "needs_extraction",
                    "extraction_hints",
                    "classified_at",
                    "extraction_status",
                    "updated_at",
                ]
            )
            updated += 1

        _update_linear_filtering_summary(
            run=run,
            organization=organization,
            project_ids=_run_linear_project_ids(run),
            batch_context={"stage": "classification_results", "updated": updated},
        )
        return Response(
            {
                "run": _serialize_run(run, request),
                "updated_count": updated,
            },
            status=status.HTTP_200_OK,
        )


class StartupUpdateLinearExtractionBatchView(APIView):
    authentication_classes = []
    permission_classes = [HasRooApiKey]

    def get(self, request, run_id: str):
        serializer = StartupUpdateBatchQuerySerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        limit = serializer.validated_data["limit"]

        run = _get_run_or_404(run_id)
        cancelled_response = _reject_if_run_cancelled(run)
        if cancelled_response is not None:
            return cancelled_response
        organization, binding, google_connection, profile = _get_org_and_binding_for_run(run)
        _update_run_step(run, step_key="linear_event_extraction")

        project_ids = _run_linear_project_ids(run)
        queryset = LinearProjectArtifact.objects.filter(
            organization=organization,
            extraction_status__in=[ArtifactProcessingStatus.PENDING, ArtifactProcessingStatus.HYDRATED],
            relevance_label__in=EXTRACTABLE_RELEVANCE_LABELS,
            needs_extraction=True,
        )
        if project_ids:
            queryset = queryset.filter(linear_project_id__in=project_ids)
        queryset = queryset.order_by("-relevance_score", "-updated_at", "name")[:limit]

        bundles = [compact_linear_project_bundle(project) for project in queryset]
        _update_linear_filtering_summary(
            run=run,
            organization=organization,
            project_ids=project_ids,
            batch_context={"stage": "extraction_batch", "returned": len(bundles)},
        )
        return Response(
            {
                "run": _serialize_run(run, request),
                "count": len(bundles),
                "projects": bundles,
            },
            status=status.HTTP_200_OK,
        )


class StartupUpdateLinearExtractionResultsView(APIView):
    authentication_classes = []
    permission_classes = [HasRooApiKey]

    @transaction.atomic
    def post(self, request, run_id: str):
        serializer = LinearExtractionResultsSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        run = _get_run_or_404(run_id)
        cancelled_response = _reject_if_run_cancelled(run)
        if cancelled_response is not None:
            return cancelled_response
        organization, binding, google_connection, profile = _get_org_and_binding_for_run(run)
        _update_run_step(run, step_key="linear_event_extraction")

        backups = get_startup_update_run_cancel_backups(run)
        backups_changed = False
        event_count = 0
        metric_count = 0
        for item in serializer.validated_data["results"]:
            project_id = _linear_project_from_public_id(item["linear_project_id"])
            project_artifact = get_object_or_404(
                LinearProjectArtifact,
                organization=organization,
                linear_project_id=project_id,
            )
            project_public_id = _linear_project_public_id(project_artifact)
            project_artifact.extraction_status = item.get(
                "extraction_status",
                ArtifactProcessingStatus.PROCESSED,
            )
            project_artifact.extracted_at = timezone.now()
            project_artifact.last_error = ""
            project_artifact.save(update_fields=["extraction_status", "extracted_at", "last_error", "updated_at"])

            source_record_ids = list(project_artifact.source_record_ids or []) or [project_public_id]
            source_metadata = {
                "source": "linear_project_extraction",
                "linear_project_id": project_public_id,
                "project_id": project_artifact.linear_project_id,
                "project_name": project_artifact.name,
            }

            for event_data in item.get("events", []):
                existing_event = StartupEvent.objects.filter(
                    organization=organization,
                    canonical_key=event_data["canonical_key"],
                ).first()
                if existing_event is not None:
                    backups_changed = _backup_event_if_needed(run, existing_event, backups) or backups_changed
                evidence_ids = event_data.get("evidence_message_ids", []) or source_record_ids
                StartupEvent.objects.update_or_create(
                    organization=organization,
                    canonical_key=event_data["canonical_key"],
                    defaults={
                        "run": run,
                        "event_type": event_data["event_type"],
                        "title": event_data["title"],
                        "summary": event_data.get("summary", ""),
                        "event_date": event_data.get("event_date"),
                        "month_bucket": event_data["month_bucket"],
                        "date_precision": event_data.get("date_precision"),
                        "sentiment": event_data.get("sentiment", ""),
                        "investor_importance": event_data.get("investor_importance", 3),
                        "quantitative_facts": event_data.get("quantitative_facts", []),
                        "evidence_message_ids": evidence_ids,
                        "evidence_attachment_ids": event_data.get("evidence_attachment_ids", []),
                        "source_thread_ids": event_data.get("source_thread_ids", []) or [project_public_id],
                        "confidence": event_data.get("confidence", 0.0),
                        "status": event_data.get("status", "open"),
                        "needs_review": bool(event_data.get("needs_review", False)),
                        "merge_notes": event_data.get("merge_notes", ""),
                    },
                )
                event_count += 1

            for metric_data in item.get("metrics", []):
                existing_metric = StartupMetricObservation.objects.filter(
                    organization=organization,
                    source_provider=ExternalServiceProvider.LINEAR,
                    metric_key=metric_data["metric_key"],
                    period_month=metric_data["period_month"],
                    value_text=metric_data["value_text"],
                ).first()
                if existing_metric is not None:
                    backups_changed = _backup_metric_if_needed(run, existing_metric, backups) or backups_changed
                evidence_ids = metric_data.get("evidence_message_ids", []) or source_record_ids
                StartupMetricObservation.objects.update_or_create(
                    organization=organization,
                    source_thread=None,
                    source_provider=ExternalServiceProvider.LINEAR,
                    metric_key=metric_data["metric_key"],
                    period_month=metric_data["period_month"],
                    value_text=metric_data["value_text"],
                    defaults={
                        "run": run,
                        "metric_name": metric_data["metric_name"],
                        "value_number": metric_data.get("value_number"),
                        "unit": metric_data.get("unit", ""),
                        "observed_at": metric_data.get("observed_at"),
                        "confidence": metric_data.get("confidence", 0.0),
                        "evidence_message_ids": evidence_ids,
                        "evidence_attachment_ids": metric_data.get("evidence_attachment_ids", []),
                        "source_record_ids": source_record_ids,
                        "source_metadata": source_metadata,
                        "summary": metric_data.get("summary", ""),
                    },
                )
                metric_count += 1

        if backups_changed:
            set_startup_update_run_cancel_backups(run, backups)
            run.save(update_fields=["result", "updated_at"])

        return Response(
            {
                "run": _serialize_run(run, request),
                "event_count": event_count,
                "metric_count": metric_count,
            },
            status=status.HTTP_200_OK,
        )


def _latest_notion_connection(user, organization: Organization):
    connection = latest_external_connection_for_startup(
        user=user,
        organization=organization,
        provider=ExternalServiceProvider.NOTION,
    )
    if connection is not None and connection.organization_id != organization.id:
        connection.organization = organization
        connection.save(update_fields=["organization", "updated_at"])
    return connection


def _notion_headers(connection) -> dict[str, str]:
    token = str(connection.access_token or "").strip()
    return {
        "Authorization": f"Bearer {token}",
        "Notion-Version": str(getattr(settings, "NOTION_API_VERSION", "2026-03-11")),
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def _notion_plain_text(rich_text: Any) -> str:
    if not isinstance(rich_text, list):
        return ""
    parts = []
    for item in rich_text:
        if not isinstance(item, dict):
            continue
        text_payload = item.get("text") if isinstance(item.get("text"), dict) else {}
        parts.append(str(item.get("plain_text") or text_payload.get("content") or ""))
    return "".join(parts).strip()


def _notion_page_title(page: dict[str, Any]) -> str:
    properties = page.get("properties") if isinstance(page.get("properties"), dict) else {}
    for prop in properties.values():
        if isinstance(prop, dict) and prop.get("type") == "title":
            title = _notion_plain_text(prop.get("title"))
            if title:
                return title
    return str(page.get("title") or page.get("id") or "Untitled Notion page").strip()


def _notion_block_text(block: dict[str, Any]) -> Tuple[str, Optional[str]]:
    block_type = str(block.get("type") or "")
    payload = block.get(block_type) if isinstance(block.get(block_type), dict) else {}
    text = _notion_plain_text(payload.get("rich_text"))
    if not text and block_type == "child_page":
        text = str(payload.get("title") or "").strip()
    heading = text if block_type in {"heading_1", "heading_2", "heading_3"} and text else None
    return text, heading


def _fetch_notion_children(connection, block_id: str, *, max_blocks: int = 80) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    cursor = None
    while len(blocks) < max_blocks:
        params = {"page_size": min(100, max_blocks - len(blocks))}
        if cursor:
            params["start_cursor"] = cursor
        response = requests.get(
            f"https://api.notion.com/v1/blocks/{block_id}/children",
            headers=_notion_headers(connection),
            params=params,
            timeout=(3, 20),
        )
        response.raise_for_status()
        payload = response.json()
        results = payload.get("results") if isinstance(payload.get("results"), list) else []
        blocks.extend(item for item in results if isinstance(item, dict))
        if not payload.get("has_more") or not payload.get("next_cursor"):
            break
        cursor = payload.get("next_cursor")
    return blocks


def _build_notion_page_bundle(connection, page: dict[str, Any]) -> dict[str, Any]:
    page_id = str(page.get("id") or "").strip()
    title = _notion_page_title(page)
    blocks = _fetch_notion_children(connection, page_id) if page_id else []
    text_lines = [title]
    source_block_ids: list[str] = []
    heading_path: list[str] = []
    for block in blocks:
        text, heading = _notion_block_text(block)
        block_id = str(block.get("id") or "").strip()
        if block_id:
            source_block_ids.append(f"notion:block:{block_id}")
        if heading:
            heading_path = [heading]
        if text:
            text_lines.append(text)
    cleaned_text = "\n".join(line for line in text_lines if line).strip()
    haystack = f"{title}\n{cleaned_text}".lower()
    reasons = []
    score = 20
    if any(term in haystack for term in HIGH_SIGNAL_TERMS):
        score += 40
        reasons.append("matched_high_signal_term")
    if any(term in haystack for term in ("ask", "risk", "blocked", "launched", "shipped", "customer", "investor")):
        score += 25
        reasons.append("matched_update_signal")
    parent = page.get("parent") if isinstance(page.get("parent"), dict) else {}
    return {
        "notion_page_id": page_id,
        "notion_chunk_id": f"{page_id}:main" if page_id else "",
        "url": str(page.get("url") or ""),
        "title": title,
        "ancestor_path": [],
        "parent_data_source": parent,
        "properties": page.get("properties") if isinstance(page.get("properties"), dict) else {},
        "created_time": page.get("created_time"),
        "last_edited_time": page.get("last_edited_time"),
        "section_heading_path": heading_path,
        "source_block_ids": source_block_ids,
        "cleaned_text": cleaned_text,
        "heuristic_score": min(score, 100),
        "heuristic_reasons": reasons,
        "relevance_label": GmailRelevanceLabel.PENDING,
        "relevance_score": 0.0,
        "relevance_reason": "",
        "extraction_hints": {},
        "omitted_block_count": max(len(blocks) - len(source_block_ids), 0),
        "compression_notes": ["notion_page_children_compacted"],
    }


def _get_notion_run_store(connection, run_id: str) -> dict[str, Any]:
    cursor = connection.sync_cursor if isinstance(connection.sync_cursor, dict) else {}
    run_stores = cursor.get("startup_update_runs") if isinstance(cursor.get("startup_update_runs"), dict) else {}
    store = run_stores.get(run_id) if isinstance(run_stores.get(run_id), dict) else {}
    store.setdefault("pages", [])
    store.setdefault("classifications", {})
    store.setdefault("extracted_chunk_ids", [])
    return store


def _save_notion_run_store(connection, run_id: str, store: dict[str, Any]) -> None:
    cursor = dict(connection.sync_cursor or {})
    run_stores = dict(cursor.get("startup_update_runs") or {})
    run_stores[run_id] = store
    cursor["startup_update_runs"] = run_stores
    cursor["startup_update_index_partial"] = bool(store.get("index_partial", False))
    connection.sync_cursor = cursor
    connection.last_synced_at = timezone.now()
    connection.last_error = ""
    connection.save(update_fields=["sync_cursor", "last_synced_at", "last_error", "updated_at"])


class StartupUpdateNotionBackfillView(APIView):
    authentication_classes = []
    permission_classes = [HasRooApiKey]

    def post(self, request, run_id: str):
        run = _get_run_or_404(run_id)
        cancelled_response = _reject_if_run_cancelled(run)
        if cancelled_response is not None:
            return cancelled_response
        organization, binding, google_connection, profile = _get_org_and_binding_for_run(run)
        _update_run_step(run, step_key="notion_backfill")

        connection = _latest_notion_connection(binding.user, organization)
        if connection is None:
            return Response({"error": "Notion is not connected."}, status=status.HTTP_400_BAD_REQUEST)
        if not str(connection.access_token or "").strip():
            return Response({"error": "Notion connection is missing an access token."}, status=status.HTTP_400_BAD_REQUEST)

        store = _get_notion_run_store(connection, run.run_id)
        cursor = store.get("next_cursor")
        body: dict[str, Any] = {
            "filter": {"property": "object", "value": "page"},
            "sort": {"direction": "descending", "timestamp": "last_edited_time"},
            "page_size": int(getattr(settings, "NOTION_SYNC_PAGE_LIMIT", 20) or 20),
        }
        if cursor:
            body["start_cursor"] = cursor
        try:
            response = requests.post(
                "https://api.notion.com/v1/search",
                headers=_notion_headers(connection),
                json=body,
                timeout=(3, 20),
            )
            if response.status_code == 429:
                retry_after = int(response.headers.get("Retry-After") or 1)
                return Response(
                    {
                        "run": _serialize_run(run, request),
                        "provider": "notion",
                        "status": "rate_limited",
                        "has_more": True,
                        "retry_after_seconds": retry_after,
                        "pages_synced": 0,
                        "chunks_indexed": len(store.get("pages") or []),
                        "index_partial": True,
                        "warnings": ["notion_rate_limited"],
                    },
                    status=status.HTTP_200_OK,
                )
            response.raise_for_status()
            payload = response.json()
        except requests.RequestException as exc:
            connection.last_error = str(exc) or "Notion sync failed."
            connection.save(update_fields=["last_error", "updated_at"])
            return Response(
                {"error": "Notion backfill failed while contacting Notion.", "detail": str(exc)},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        existing_by_chunk = {
            str(page.get("notion_chunk_id") or ""): page
            for page in store.get("pages", [])
            if isinstance(page, dict)
        }
        pages_synced = 0
        warnings: list[str] = []
        for page in payload.get("results") or []:
            if not isinstance(page, dict):
                continue
            try:
                bundle = _build_notion_page_bundle(connection, page)
            except requests.RequestException as exc:
                warnings.append(f"notion_page_children_failed:{page.get('id')}")
                bundle = {
                    "notion_page_id": str(page.get("id") or ""),
                    "notion_chunk_id": f"{page.get('id')}:main",
                    "url": str(page.get("url") or ""),
                    "title": _notion_page_title(page),
                    "ancestor_path": [],
                    "parent_data_source": page.get("parent") if isinstance(page.get("parent"), dict) else {},
                    "properties": page.get("properties") if isinstance(page.get("properties"), dict) else {},
                    "created_time": page.get("created_time"),
                    "last_edited_time": page.get("last_edited_time"),
                    "section_heading_path": [],
                    "source_block_ids": [],
                    "cleaned_text": _notion_page_title(page),
                    "heuristic_score": 10,
                    "heuristic_reasons": ["notion_children_fetch_failed"],
                    "relevance_label": GmailRelevanceLabel.PENDING,
                    "relevance_score": 0.0,
                    "relevance_reason": "",
                    "extraction_hints": {},
                    "omitted_block_count": 0,
                    "compression_notes": [str(exc)[:200]],
                }
            existing_by_chunk[bundle["notion_chunk_id"]] = bundle
            pages_synced += 1

        store["pages"] = list(existing_by_chunk.values())
        store["next_cursor"] = payload.get("next_cursor")
        store["index_partial"] = bool(payload.get("has_more"))
        _save_notion_run_store(connection, run.run_id, store)

        run_request = dict(run.run_request or {})
        run_request["notion_connection_id"] = connection.id
        external_context = dict(run_request.get("external_context") or {})
        notion_context = dict(external_context.get("notion") or {})
        notion_context.update(
            {
                "connection_id": connection.id,
                "workspace": connection.account_label,
                "scope": "whole_accessible_workspace",
                "index_partial": bool(store.get("index_partial")),
                "pages_indexed": len(store.get("pages") or []),
                "warnings": warnings,
            }
        )
        external_context["notion"] = notion_context
        run_request["external_context"] = external_context
        run.run_request = run_request
        run.save(update_fields=["run_request", "updated_at"])

        return Response(
            {
                "run": _serialize_run(run, request),
                "provider": "notion",
                "status": "ok",
                "pages_synced": pages_synced,
                "pagesSynced": pages_synced,
                "chunks_indexed": len(store.get("pages") or []),
                "chunksIndexed": len(store.get("pages") or []),
                "has_more": bool(payload.get("has_more")),
                "index_partial": bool(store.get("index_partial")),
                "indexPartial": bool(store.get("index_partial")),
                "warnings": warnings,
            },
            status=status.HTTP_200_OK,
        )


class StartupUpdateNotionClassificationBatchView(APIView):
    authentication_classes = []
    permission_classes = [HasRooApiKey]

    def get(self, request, run_id: str):
        serializer = StartupUpdateBatchQuerySerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        limit = serializer.validated_data["limit"]
        run = _get_run_or_404(run_id)
        cancelled_response = _reject_if_run_cancelled(run)
        if cancelled_response is not None:
            return cancelled_response
        organization, binding, google_connection, profile = _get_org_and_binding_for_run(run)
        _update_run_step(run, step_key="notion_relevance_classification")
        connection = _latest_notion_connection(binding.user, organization)
        if connection is None:
            return Response({"error": "Notion is not connected."}, status=status.HTTP_400_BAD_REQUEST)
        store = _get_notion_run_store(connection, run.run_id)
        classifications = store.get("classifications") or {}
        pages = [
            page
            for page in store.get("pages", [])
            if isinstance(page, dict) and page.get("notion_chunk_id") not in classifications
        ][:limit]
        return Response({"run": _serialize_run(run, request), "count": len(pages), "pages": pages}, status=status.HTTP_200_OK)


class StartupUpdateNotionClassificationResultsView(APIView):
    authentication_classes = []
    permission_classes = [HasRooApiKey]

    def post(self, request, run_id: str):
        serializer = NotionClassificationResultsSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        run = _get_run_or_404(run_id)
        cancelled_response = _reject_if_run_cancelled(run)
        if cancelled_response is not None:
            return cancelled_response
        organization, binding, google_connection, profile = _get_org_and_binding_for_run(run)
        _update_run_step(run, step_key="notion_relevance_classification")
        connection = _latest_notion_connection(binding.user, organization)
        if connection is None:
            return Response({"error": "Notion is not connected."}, status=status.HTTP_400_BAD_REQUEST)
        store = _get_notion_run_store(connection, run.run_id)
        classifications = dict(store.get("classifications") or {})
        updated = 0
        for item in serializer.validated_data["results"]:
            chunk_id = item.get("notion_chunk_id") or f"{item['notion_page_id']}:main"
            classifications[chunk_id] = {
                **item,
                "notion_chunk_id": chunk_id,
                "extraction_hints": {
                    "important_block_ids": item.get("important_block_ids") or [],
                    "extraction_hint": item.get("extraction_hint", ""),
                },
            }
            updated += 1
        store["classifications"] = classifications
        _save_notion_run_store(connection, run.run_id, store)
        return Response({"run": _serialize_run(run, request), "updated_count": updated}, status=status.HTTP_200_OK)


class StartupUpdateNotionExtractionBatchView(APIView):
    authentication_classes = []
    permission_classes = [HasRooApiKey]

    def get(self, request, run_id: str):
        serializer = StartupUpdateBatchQuerySerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        limit = serializer.validated_data["limit"]
        run = _get_run_or_404(run_id)
        cancelled_response = _reject_if_run_cancelled(run)
        if cancelled_response is not None:
            return cancelled_response
        organization, binding, google_connection, profile = _get_org_and_binding_for_run(run)
        _update_run_step(run, step_key="notion_event_extraction")
        connection = _latest_notion_connection(binding.user, organization)
        if connection is None:
            return Response({"error": "Notion is not connected."}, status=status.HTTP_400_BAD_REQUEST)
        store = _get_notion_run_store(connection, run.run_id)
        classifications = store.get("classifications") or {}
        extracted = set(store.get("extracted_chunk_ids") or [])
        pages = []
        for page in store.get("pages", []):
            if not isinstance(page, dict):
                continue
            chunk_id = page.get("notion_chunk_id")
            classification = classifications.get(chunk_id) or {}
            label = classification.get("relevance_label")
            if chunk_id in extracted:
                continue
            if label not in EXTRACTABLE_RELEVANCE_LABELS:
                continue
            if not bool(classification.get("needs_extraction", True)):
                continue
            page = dict(page)
            page["relevance_label"] = label
            page["relevance_score"] = classification.get("relevance_score", 0.0)
            page["relevance_reason"] = classification.get("relevance_reason", "")
            page["extraction_hints"] = classification.get("extraction_hints") or {}
            pages.append(page)
            if len(pages) >= limit:
                break
        return Response({"run": _serialize_run(run, request), "count": len(pages), "pages": pages}, status=status.HTTP_200_OK)


class StartupUpdateNotionExtractionResultsView(APIView):
    authentication_classes = []
    permission_classes = [HasRooApiKey]

    @transaction.atomic
    def post(self, request, run_id: str):
        serializer = NotionExtractionResultsSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        run = _get_run_or_404(run_id)
        cancelled_response = _reject_if_run_cancelled(run)
        if cancelled_response is not None:
            return cancelled_response
        organization, binding, google_connection, profile = _get_org_and_binding_for_run(run)
        _update_run_step(run, step_key="notion_event_extraction")
        connection = _latest_notion_connection(binding.user, organization)
        if connection is None:
            return Response({"error": "Notion is not connected."}, status=status.HTTP_400_BAD_REQUEST)
        store = _get_notion_run_store(connection, run.run_id)
        extracted = set(store.get("extracted_chunk_ids") or [])
        backups = get_startup_update_run_cancel_backups(run)
        backups_changed = False
        event_count = 0
        metric_count = 0
        for item in serializer.validated_data["results"]:
            chunk_id = item.get("notion_chunk_id") or f"{item['notion_page_id']}:main"
            source_record_ids = [f"notion:page:{item['notion_page_id']}", chunk_id]
            source_metadata = {
                "source": "notion_page_extraction",
                "notion_page_id": item["notion_page_id"],
                "notion_chunk_id": chunk_id,
            }
            for event_data in item.get("events", []):
                existing_event = StartupEvent.objects.filter(
                    organization=organization,
                    canonical_key=event_data["canonical_key"],
                ).first()
                if existing_event is not None:
                    backups_changed = _backup_event_if_needed(run, existing_event, backups) or backups_changed
                evidence_ids = event_data.get("evidence_message_ids", []) or source_record_ids
                StartupEvent.objects.update_or_create(
                    organization=organization,
                    canonical_key=event_data["canonical_key"],
                    defaults={
                        "run": run,
                        "event_type": event_data["event_type"],
                        "title": event_data["title"],
                        "summary": event_data.get("summary", ""),
                        "event_date": event_data.get("event_date"),
                        "month_bucket": event_data["month_bucket"],
                        "date_precision": event_data.get("date_precision"),
                        "sentiment": event_data.get("sentiment", ""),
                        "investor_importance": event_data.get("investor_importance", 3),
                        "quantitative_facts": event_data.get("quantitative_facts", []),
                        "evidence_message_ids": evidence_ids,
                        "evidence_attachment_ids": event_data.get("evidence_attachment_ids", []),
                        "source_thread_ids": event_data.get("source_thread_ids", []) or source_record_ids,
                        "confidence": event_data.get("confidence", 0.0),
                        "status": event_data.get("status", "open"),
                        "needs_review": bool(event_data.get("needs_review", False)),
                        "merge_notes": event_data.get("merge_notes", ""),
                    },
                )
                event_count += 1
            for metric_data in item.get("metrics", []):
                existing_metric = StartupMetricObservation.objects.filter(
                    organization=organization,
                    source_provider=ExternalServiceProvider.NOTION,
                    metric_key=metric_data["metric_key"],
                    period_month=metric_data["period_month"],
                    value_text=metric_data["value_text"],
                ).first()
                if existing_metric is not None:
                    backups_changed = _backup_metric_if_needed(run, existing_metric, backups) or backups_changed
                evidence_ids = metric_data.get("evidence_message_ids", []) or source_record_ids
                StartupMetricObservation.objects.update_or_create(
                    organization=organization,
                    source_thread=None,
                    source_provider=ExternalServiceProvider.NOTION,
                    metric_key=metric_data["metric_key"],
                    period_month=metric_data["period_month"],
                    value_text=metric_data["value_text"],
                    defaults={
                        "run": run,
                        "metric_name": metric_data["metric_name"],
                        "value_number": metric_data.get("value_number"),
                        "unit": metric_data.get("unit", ""),
                        "observed_at": metric_data.get("observed_at"),
                        "confidence": metric_data.get("confidence", 0.0),
                        "evidence_message_ids": evidence_ids,
                        "evidence_attachment_ids": metric_data.get("evidence_attachment_ids", []),
                        "source_record_ids": source_record_ids,
                        "source_metadata": source_metadata,
                        "summary": metric_data.get("summary", ""),
                    },
                )
                metric_count += 1
            extracted.add(chunk_id)
        store["extracted_chunk_ids"] = sorted(extracted)
        _save_notion_run_store(connection, run.run_id, store)
        if backups_changed:
            set_startup_update_run_cancel_backups(run, backups)
            run.save(update_fields=["result", "updated_at"])
        return Response(
            {
                "run": _serialize_run(run, request),
                "event_count": event_count,
                "metric_count": metric_count,
            },
            status=status.HTTP_200_OK,
        )


class StartupUpdateGoogleAnalyticsBackfillView(APIView):
    authentication_classes = []
    permission_classes = [HasRooApiKey]

    def post(self, request, run_id: str):
        run = _get_run_or_404(run_id)
        cancelled_response = _reject_if_run_cancelled(run)
        if cancelled_response is not None:
            return cancelled_response
        _get_org_and_binding_for_run(run)
        _update_run_step(run, step_key="google_analytics_backfill")
        try:
            result = run_google_analytics_backfill(run)
        except requests.RequestException as exc:
            return Response(
                {
                    "error": "Google Analytics backfill failed while contacting Google Analytics.",
                    "detail": str(exc),
                },
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        reports_synced = int(result.get("reports_synced") or 0)
        properties_synced = int(result.get("properties_synced") or 0)
        metrics_synced = int(result.get("metrics_synced") or 0)
        payload = {
            "run": _serialize_run(run, request),
            "provider": "google_analytics",
            "status": result.get("status", "ok"),
            "reports_synced": reports_synced,
            "reportsSynced": reports_synced,
            "properties_synced": properties_synced,
            "propertiesSynced": properties_synced,
            "metrics_synced": metrics_synced,
            "metricsSynced": metrics_synced,
            "properties": result.get("properties", []),
            "has_more": bool(result.get("has_more")),
            "warnings": result.get("warnings", []),
        }
        if result.get("source_unavailable"):
            payload["source_unavailable"] = True
            payload["sourceUnavailable"] = True
            payload["code"] = result.get("code") or "google_analytics_source_unavailable"
            payload["warning"] = result.get("warning") or "Google Analytics is unavailable for this run."
        if result.get("retry_after_seconds") is not None:
            payload["retry_after_seconds"] = result["retry_after_seconds"]
        return Response(payload, status=status.HTTP_200_OK)


class StartupUpdateGoogleAnalyticsClassificationBatchView(APIView):
    authentication_classes = []
    permission_classes = [HasRooApiKey]

    def get(self, request, run_id: str):
        serializer = StartupUpdateBatchQuerySerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        limit = serializer.validated_data["limit"]
        run = _get_run_or_404(run_id)
        cancelled_response = _reject_if_run_cancelled(run)
        if cancelled_response is not None:
            return cancelled_response
        _get_org_and_binding_for_run(run)
        _update_run_step(run, step_key="google_analytics_relevance_classification")
        connection = resolve_google_analytics_connection_for_run(run)
        if connection is None:
            return Response(
                {"run": _serialize_run(run, request), "count": 0, "reports": []},
                status=status.HTTP_200_OK,
            )
        store = get_ga_run_store(connection, run.run_id)
        reports = ga_build_classification_batch(store, limit)
        return Response(
            {"run": _serialize_run(run, request), "count": len(reports), "reports": reports},
            status=status.HTTP_200_OK,
        )


class StartupUpdateGoogleAnalyticsClassificationResultsView(APIView):
    authentication_classes = []
    permission_classes = [HasRooApiKey]

    def post(self, request, run_id: str):
        serializer = GoogleAnalyticsClassificationResultsSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        run = _get_run_or_404(run_id)
        cancelled_response = _reject_if_run_cancelled(run)
        if cancelled_response is not None:
            return cancelled_response
        _get_org_and_binding_for_run(run)
        _update_run_step(run, step_key="google_analytics_relevance_classification")
        connection = resolve_google_analytics_connection_for_run(run)
        if connection is None:
            return Response(
                {"run": _serialize_run(run, request), "updated_count": 0},
                status=status.HTTP_200_OK,
            )
        store = get_ga_run_store(connection, run.run_id)
        updated = ga_apply_classification_results(store, serializer.validated_data["results"])
        save_ga_run_store(connection, run.run_id, store)
        return Response(
            {"run": _serialize_run(run, request), "updated_count": updated},
            status=status.HTTP_200_OK,
        )


class StartupUpdateGoogleAnalyticsExtractionBatchView(APIView):
    authentication_classes = []
    permission_classes = [HasRooApiKey]

    def get(self, request, run_id: str):
        serializer = StartupUpdateBatchQuerySerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        limit = serializer.validated_data["limit"]
        run = _get_run_or_404(run_id)
        cancelled_response = _reject_if_run_cancelled(run)
        if cancelled_response is not None:
            return cancelled_response
        _get_org_and_binding_for_run(run)
        _update_run_step(run, step_key="google_analytics_event_extraction")
        connection = resolve_google_analytics_connection_for_run(run)
        if connection is None:
            return Response(
                {"run": _serialize_run(run, request), "count": 0, "reports": []},
                status=status.HTTP_200_OK,
            )
        store = get_ga_run_store(connection, run.run_id)
        reports = ga_build_extraction_batch(
            store,
            limit,
            extractable_labels={str(label) for label in EXTRACTABLE_RELEVANCE_LABELS},
        )
        return Response(
            {"run": _serialize_run(run, request), "count": len(reports), "reports": reports},
            status=status.HTTP_200_OK,
        )


class StartupUpdateGoogleAnalyticsExtractionResultsView(APIView):
    authentication_classes = []
    permission_classes = [HasRooApiKey]

    @transaction.atomic
    def post(self, request, run_id: str):
        serializer = GoogleAnalyticsExtractionResultsSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        run = _get_run_or_404(run_id)
        cancelled_response = _reject_if_run_cancelled(run)
        if cancelled_response is not None:
            return cancelled_response
        organization, binding, google_connection, profile = _get_org_and_binding_for_run(run)
        _update_run_step(run, step_key="google_analytics_event_extraction")
        connection = resolve_google_analytics_connection_for_run(run)
        if connection is None:
            return Response(
                {"run": _serialize_run(run, request), "event_count": 0, "metric_count": 0},
                status=status.HTTP_200_OK,
            )
        store = get_ga_run_store(connection, run.run_id)
        reports = store.get("reports") or {}
        extracted = set(store.get("extracted_report_ids") or [])
        backups = get_startup_update_run_cancel_backups(run)
        backups_changed = False
        event_count = 0
        metric_count = 0
        for item in serializer.validated_data["results"]:
            ga_report_id = item["ga_report_id"]
            bundle = reports.get(ga_report_id) or {}
            property_id = str(bundle.get("property_id") or "")
            if property_id:
                source_record_ids = [f"google_analytics:property:{property_id}", ga_report_id]
            else:
                source_record_ids = [ga_report_id]
            source_metadata = {
                "source": "google_analytics_report_extraction",
                "ga_report_id": ga_report_id,
                "property_id": property_id,
                "report_type": bundle.get("report_type", ""),
            }
            for event_data in item.get("events", []):
                existing_event = StartupEvent.objects.filter(
                    organization=organization,
                    canonical_key=event_data["canonical_key"],
                ).first()
                if existing_event is not None:
                    backups_changed = _backup_event_if_needed(run, existing_event, backups) or backups_changed
                evidence_ids = event_data.get("evidence_message_ids", []) or source_record_ids
                StartupEvent.objects.update_or_create(
                    organization=organization,
                    canonical_key=event_data["canonical_key"],
                    defaults={
                        "run": run,
                        "event_type": event_data["event_type"],
                        "title": event_data["title"],
                        "summary": event_data.get("summary", ""),
                        "event_date": event_data.get("event_date"),
                        "month_bucket": event_data["month_bucket"],
                        "date_precision": event_data.get("date_precision"),
                        "sentiment": event_data.get("sentiment", ""),
                        "investor_importance": event_data.get("investor_importance", 3),
                        "quantitative_facts": event_data.get("quantitative_facts", []),
                        "evidence_message_ids": evidence_ids,
                        "evidence_attachment_ids": event_data.get("evidence_attachment_ids", []),
                        "source_thread_ids": event_data.get("source_thread_ids", []) or source_record_ids,
                        "confidence": event_data.get("confidence", 0.0),
                        "status": event_data.get("status", "open"),
                        "needs_review": bool(event_data.get("needs_review", False)),
                        "merge_notes": event_data.get("merge_notes", ""),
                    },
                )
                event_count += 1
            for metric_data in item.get("metrics", []):
                existing_metric = StartupMetricObservation.objects.filter(
                    organization=organization,
                    source_provider=ExternalServiceProvider.GOOGLE_ANALYTICS,
                    metric_key=metric_data["metric_key"],
                    period_month=metric_data["period_month"],
                    value_text=metric_data["value_text"],
                ).first()
                if existing_metric is not None:
                    backups_changed = _backup_metric_if_needed(run, existing_metric, backups) or backups_changed
                evidence_ids = metric_data.get("evidence_message_ids", []) or source_record_ids
                StartupMetricObservation.objects.update_or_create(
                    organization=organization,
                    source_thread=None,
                    source_provider=ExternalServiceProvider.GOOGLE_ANALYTICS,
                    metric_key=metric_data["metric_key"],
                    period_month=metric_data["period_month"],
                    value_text=metric_data["value_text"],
                    defaults={
                        "run": run,
                        "metric_name": metric_data["metric_name"],
                        "value_number": metric_data.get("value_number"),
                        "unit": metric_data.get("unit", ""),
                        "observed_at": metric_data.get("observed_at"),
                        "confidence": metric_data.get("confidence", 0.0),
                        "evidence_message_ids": evidence_ids,
                        "evidence_attachment_ids": metric_data.get("evidence_attachment_ids", []),
                        "source_record_ids": source_record_ids,
                        "source_metadata": source_metadata,
                        "summary": metric_data.get("summary", ""),
                    },
                )
                metric_count += 1
            extracted.add(ga_report_id)
        store["extracted_report_ids"] = sorted(extracted)
        save_ga_run_store(connection, run.run_id, store)
        if backups_changed:
            set_startup_update_run_cancel_backups(run, backups)
            run.save(update_fields=["result", "updated_at"])
        return Response(
            {
                "run": _serialize_run(run, request),
                "event_count": event_count,
                "metric_count": metric_count,
            },
            status=status.HTTP_200_OK,
        )


def _run_result_candidates(run: ContentFactoryRun) -> list[dict[str, Any]]:
    result = run.result if isinstance(run.result, dict) else {}
    candidates = result.get("update_candidates")
    if isinstance(candidates, list):
        return [item for item in candidates if isinstance(item, dict)]
    founder_review = result.get("founder_review") if isinstance(result.get("founder_review"), dict) else {}
    candidates = founder_review.get("candidates")
    if isinstance(candidates, list):
        return [item for item in candidates if isinstance(item, dict)]
    curation = result.get("curation") if isinstance(result.get("curation"), dict) else {}
    backend_response = curation.get("backend_response") if isinstance(curation.get("backend_response"), dict) else {}
    candidates = backend_response.get("candidates") or backend_response.get("update_candidates")
    if isinstance(candidates, list):
        return [item for item in candidates if isinstance(item, dict)]
    return []


def _save_run_result_candidates(run: ContentFactoryRun, candidates: list[dict[str, Any]]) -> None:
    result = dict(run.result or {})
    result["update_candidates"] = candidates
    run.result = result
    run.save(update_fields=["result", "updated_at"])


class StartupUpdateCurationContextView(APIView):
    authentication_classes = []
    permission_classes = [HasRooApiKey]

    def get(self, request, run_id: str):
        run = _get_run_or_404(run_id)
        cancelled_response = _reject_if_run_cancelled(run)
        if cancelled_response is not None:
            return cancelled_response
        organization, binding, google_connection, profile = _get_org_and_binding_for_run(run)
        _update_run_step(run, step_key="candidate_curation")
        current_month = get_startup_update_run_target_month(run)
        prior_updates = []
        draft_queryset = organization.monthly_update_drafts.order_by("-month", "-updated_at")
        if current_month is not None:
            draft_queryset = draft_queryset.exclude(month=current_month)
        for draft in draft_queryset[:6]:
            prior_updates.append(_serialize_draft(draft))
        return Response(
            {
                "run": _serialize_run(run, request),
                "timeline": build_timeline_payload(organization=organization),
                "startup_context": (run.run_request or {}).get("startup_context") or {},
                "external_context": (run.run_request or {}).get("external_context") or {},
                "startup_memory": (run.run_request or {}).get("startup_memory") or {},
                "prior_updates": prior_updates,
            },
            status=status.HTTP_200_OK,
        )


class StartupUpdateCurationResultsView(APIView):
    authentication_classes = []
    permission_classes = [HasRooApiKey]

    def post(self, request, run_id: str):
        serializer = CurationResultsSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        run = _get_run_or_404(run_id)
        cancelled_response = _reject_if_run_cancelled(run)
        if cancelled_response is not None:
            return cancelled_response
        _update_run_step(run, step_key="candidate_curation")
        candidates = list(serializer.validated_data["candidates"])
        _save_run_result_candidates(run, candidates)
        return Response(
            {
                "run": _serialize_run(run, request),
                "candidate_count": len(candidates),
                "candidates": candidates,
                "update_candidates": candidates,
            },
            status=status.HTTP_200_OK,
        )


class StartupUpdateReviewCandidatesView(APIView):
    authentication_classes = []
    permission_classes = [HasRooApiKey]

    def get(self, request, run_id: str):
        run = _get_run_or_404(run_id)
        return Response(
            {
                "run": _serialize_run(run, request),
                "candidates": _run_result_candidates(run),
            },
            status=status.HTTP_200_OK,
        )


class StartupUpdateFounderReviewAutoApproveView(APIView):
    authentication_classes = []
    permission_classes = [HasRooApiKey]

    def post(self, request, run_id: str):
        run = _get_run_or_404(run_id)
        cancelled_response = _reject_if_run_cancelled(run)
        if cancelled_response is not None:
            return cancelled_response
        _update_run_step(run, step_key="founder_review")
        candidates = _run_result_candidates(run)
        counts = {"approved": 0, "maybe": 0, "background": 0, "excluded": 0}
        reviewed = []
        for candidate in candidates:
            item = dict(candidate)
            decision = str(item.get("include_decision") or "").lower()
            include_score = float(item.get("include_score") or 0.0)
            evidence_quality = float(item.get("evidence_quality_score") or 0.0)
            sensitivity_risk = float(item.get("sensitivity_risk_score") or 0.0)
            if decision == "include" and include_score >= 0.80 and evidence_quality >= 0.35 and sensitivity_risk < 0.80:
                item["founder_status"] = item.get("founder_status") or "auto_approved"
                counts["approved"] += 1
            elif decision == "maybe":
                item["founder_status"] = item.get("founder_status") or "needs_review"
                counts["maybe"] += 1
            elif decision == "background":
                item["founder_status"] = item.get("founder_status") or "background"
                counts["background"] += 1
            else:
                item["founder_status"] = item.get("founder_status") or "excluded"
                counts["excluded"] += 1
            reviewed.append(item)
        _save_run_result_candidates(run, reviewed)
        return Response(
            {
                "run": _serialize_run(run, request),
                "approved_count": counts["approved"],
                "maybe_count": counts["maybe"],
                "background_count": counts["background"],
                "excluded_count": counts["excluded"],
                "candidates": reviewed,
            },
            status=status.HTTP_200_OK,
        )


class StartupUpdateCuratedTimelineView(APIView):
    authentication_classes = []
    permission_classes = [HasRooApiKey]

    def get(self, request, run_id: str):
        run = _get_run_or_404(run_id)
        audience = str(request.query_params.get("audience") or "investor").strip().lower()
        if audience not in {"investor", "community"}:
            return Response({"error": "audience must be investor or community."}, status=status.HTTP_400_BAD_REQUEST)
        organization, binding, google_connection, profile = _get_org_and_binding_for_run(run)
        candidates = _run_result_candidates(run)
        approved_event_ids = set()
        approved_metric_ids = set()
        for candidate in candidates:
            audiences = set(candidate.get("target_audiences") or [])
            founder_status = str(candidate.get("founder_status") or "").lower()
            if founder_status not in {"approved", "auto_approved"}:
                continue
            if audience not in audiences and "both" not in audiences:
                continue
            if candidate.get("event_id"):
                approved_event_ids.add(int(candidate["event_id"]))
            if candidate.get("metric_id"):
                approved_metric_ids.add(int(candidate["metric_id"]))
        timeline = build_timeline_payload(organization=organization)
        for bucket in (timeline.get("months") or {}).values():
            if not isinstance(bucket, dict):
                continue
            bucket["events"] = [
                event for event in bucket.get("events", []) if int(event.get("id") or 0) in approved_event_ids
            ]
            bucket["metrics"] = [
                metric for metric in bucket.get("metrics", []) if int(metric.get("id") or 0) in approved_metric_ids
            ]
        return Response(
            {
                "run": _serialize_run(run, request),
                "audience": audience,
                "timeline": timeline,
                "candidate_count": len(candidates),
            },
            status=status.HTTP_200_OK,
        )


class StartupUpdateTimelineView(APIView):
    authentication_classes = []
    permission_classes = [HasRooApiKey]

    def get(self, request, run_id: str):
        run = _get_run_or_404(run_id)
        cancelled_response = _reject_if_run_cancelled(run)
        if cancelled_response is not None:
            return cancelled_response
        organization, binding, google_connection, profile = _get_org_and_binding_for_run(run)
        _update_run_step(run, step_key="timeline_merge")
        return Response(
            {
                "run": _serialize_run(run, request),
                "timeline": build_timeline_payload(organization=organization),
            },
            status=status.HTTP_200_OK,
        )


class StartupUpdateDraftResultsView(APIView):
    authentication_classes = []
    permission_classes = [HasRooApiKey]

    def get(self, request, run_id: str):
        run = _get_run_or_404(run_id)
        drafts = list(run.monthly_update_drafts.order_by("-month", "-updated_at"))
        payload = _serialize_draft_results_bundle(drafts)
        if payload is None:
            return Response(
                {"error": "Draft results are not available yet."},
                status=status.HTTP_404_NOT_FOUND,
            )

        return Response(
            {
                "run_id": run.run_id,
                **payload,
            },
            status=status.HTTP_200_OK,
        )

    def post(self, request, run_id: str):
        serializer = DraftResultsSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        run = _get_run_or_404(run_id)
        cancelled_response = _reject_if_run_cancelled(run)
        if cancelled_response is not None:
            return cancelled_response
        organization, binding, google_connection, profile = _get_org_and_binding_for_run(run)
        _update_run_step(run, step_key="draft_generation")

        replace_existing = bool((run.run_request or {}).get("force_regenerate"))
        backups = get_startup_update_run_cancel_backups(run)
        backups_changed = False
        saved = []
        try:
            for item in serializer.validated_data["drafts"]:
                existing_draft = MonthlyUpdateDraft.objects.filter(
                    organization=organization,
                    month=item["month"],
                ).first()
                if existing_draft is not None:
                    backups_changed = _backup_draft_if_needed(run, existing_draft, backups) or backups_changed
                structured_memo, evidence_metric_ids = merge_xero_metrics_into_structured_memo(
                    organization=organization,
                    month=item["month"],
                    structured_memo=item["structured_memo"],
                    evidence_metric_ids=item.get("evidence_metric_ids", []),
                )
                structured_memo, evidence_metric_ids = merge_luma_metrics_into_structured_memo(
                    organization=organization,
                    month=item["month"],
                    structured_memo=structured_memo,
                    evidence_metric_ids=evidence_metric_ids,
                )
                draft = upsert_monthly_update_draft(
                    organization=organization,
                    month=item["month"],
                    run=run,
                    structured_memo=structured_memo,
                    model_name=item.get("model_name", ""),
                    status=item.get("status"),
                    groundedness_status=item.get("groundedness_status"),
                    evidence_event_ids=item.get("evidence_event_ids", []),
                    evidence_metric_ids=evidence_metric_ids,
                    carry_forward_event_ids=item.get("carry_forward_event_ids", []),
                    groundedness_notes=item.get("groundedness_notes", ""),
                    replace=replace_existing,
                )
                saved.append(_serialize_draft(draft))

            if backups_changed:
                set_startup_update_run_cancel_backups(run, backups)
                run.save(update_fields=["result", "updated_at"])
        except OperationalError as exc:
            if not _is_transient_sqlite_lock(exc):
                raise
            return Response(
                {
                    "error": "transient_database_lock",
                    "detail": "Draft results could not be saved because the database is temporarily locked.",
                    "retryable": True,
                    "retry_after_seconds": settings.SQLITE_LOCK_RETRY_AFTER_SECONDS,
                    "run_id": run.run_id,
                },
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        return Response(
            {
                "run": _serialize_run(run, request),
                "drafts": saved,
            },
            status=status.HTTP_200_OK,
        )


class StartupUpdateDraftListView(APIView):
    authentication_classes = []
    permission_classes = [HasRooApiKey]

    def get(self, request):
        domain = normalize_domain(request.query_params.get("domain") or "")
        if not domain:
            return Response(
                {"error": "domain query parameter is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        organization = get_object_or_404(Organization, domain=domain)
        drafts = organization.monthly_update_drafts.order_by("-month", "-updated_at")
        return Response(
            {
                "domain": organization.domain,
                "drafts": [_serialize_draft(draft) for draft in drafts],
            },
            status=status.HTTP_200_OK,
        )


class StartupUpdateDraftDetailView(APIView):
    authentication_classes = []
    permission_classes = [HasRooApiKey]

    def get(self, request, draft_id: int):
        draft = get_object_or_404(MonthlyUpdateDraft.objects.select_related("organization"), pk=draft_id)
        events = list(
            StartupEvent.objects.filter(
                organization=draft.organization,
                id__in=list(draft.evidence_event_ids or []),
            ).order_by("month_bucket", "-investor_importance", "title")
        )
        metrics = list(
            StartupMetricObservation.objects.filter(
                organization=draft.organization,
                id__in=list(draft.evidence_metric_ids or []),
            ).order_by("period_month", "metric_key")
        )
        return Response(
            {
                "draft": _serialize_draft(draft),
                "events": [_serialize_event(event) for event in events],
                "metrics": [_serialize_metric(metric) for metric in metrics],
            },
            status=status.HTTP_200_OK,
        )


class StartupUpdateOpenRunsView(APIView):
    authentication_classes = []
    permission_classes = [HasRooApiKey]

    def get(self, request):
        raw_limit = request.query_params.get("limit") or 100
        try:
            limit = max(1, min(int(raw_limit), 200))
        except (TypeError, ValueError):
            limit = 100

        runs = list(
            ContentFactoryRun.objects.filter(
                workflow=STARTUP_UPDATE_WORKFLOW,
                status__in=list(OPEN_RUN_STATUSES),
            ).order_by("-updated_at")[:limit]
        )
        return Response(
            {
                "count": len(runs),
                "runs": [_serialize_open_run(run) for run in runs],
            },
            status=status.HTTP_200_OK,
        )


class MonthlyDispatchTargetsView(APIView):
    """The companies that have opted in to automated monthly investor updates.

    Replaces the ops-managed ``MONTHLY_DISPATCH_BINDINGS`` env allowlist on the
    valley droplet as the source of truth for the monthly scheduler. Each target
    is a ``UserStartupBinding`` with ``monthly_updates_enabled=True`` whose
    organization has a domain — a founder self-serves scheduling per company by
    toggling the flag in-app, and a newly added startup is picked up on the next
    cycle without anyone editing server config.

    Service-key authenticated (valley uses the shared key). The shape mirrors an
    env binding (``user_id`` + ``domain``) plus ``organization_id`` /
    ``binding_id`` so valley can dedupe by the tenant key.
    """

    authentication_classes = []
    permission_classes = [HasRooApiKey]

    def get(self, request):
        bindings = (
            UserStartupBinding.objects.select_related("organization")
            .filter(monthly_updates_enabled=True)
            .exclude(organization__domain__isnull=True)
            .exclude(organization__domain="")
            .order_by("organization__domain", "user_id")
        )
        targets = [
            {
                "user_id": binding.user_id,
                "domain": binding.organization.domain,
                "organization_id": binding.organization_id,
                "binding_id": binding.id,
            }
            for binding in bindings
        ]
        return Response({"count": len(targets), "targets": targets}, status=status.HTTP_200_OK)
