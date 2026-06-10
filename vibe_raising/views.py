import calendar
import logging
import mimetypes
import os
import urllib.parse
from datetime import date, timedelta
from pathlib import Path
from typing import Optional
from uuid import uuid4

from django.conf import settings
from django.db import transaction
from django.shortcuts import get_object_or_404
from django.urls import reverse
from django.utils import timezone
from django.utils.text import slugify
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from core.firebase_utils import (
    create_signed_read_url,
    create_signed_upload_url,
    delete_storage_object,
    download_storage_object_bytes,
    finalize_private_uploaded_storage_object,
    finalize_uploaded_storage_object,
    upload_file_to_storage,
)
from organizations.models import Organization
from workflow_runs.models import ContentFactoryRun, ContentFactoryRunStatus, ContentFactoryStepStatus
from integrations.models import (
    ExternalServiceConnection,
    ExternalServiceConnectionStatus,
    ExternalServiceProvider,
)
from startup_updates.models import (
    MonthlyUpdateDraft,
    MonthlyUpdateDraftStatus,
    StartupManualDocument,
)
from startup_updates.manual_documents import parse_manual_document
from startup_updates.metric_catalog import (
    STARTUP_UPDATE_METRIC_LABELS,
    startup_update_metric_key,
    startup_update_metric_label,
)
from integrations.services.external_connectors import mark_sources_sync_requested
from integrations.services.gmail_scopes import (
    gmail_scope_status_payload,
    has_gmail_read_scope,
)
from startup_updates.services import (
    DEFAULT_BACKFILL_MONTHS,
    MANUAL_DOCUMENTS_SOURCE,
    OPEN_RUN_STATUSES,
    RUN_STEP_ORDER,
    STARTUP_UPDATE_WORKFLOW,
    bind_user_to_startup,
    coerce_startup_update_sources_for_gmail_scope,
    cancel_startup_update_run,
    create_startup_update_run,
    get_default_binding_for_domain,
    get_latest_startup_update_run,
    get_open_startup_update_run,
    get_startup_update_run_target_month,
    gmail_required_for_sources,
    build_startup_update_target_windows,
    merge_xero_metrics_into_structured_memo,
    merge_source_warnings,
    normalize_startup_update_input_sources,
    parse_startup_update_target_month,
    publish_xero_metric_observations,
    record_valley_dispatch_result,
    resolve_or_create_profile,
    refresh_startup_update_run_source_context,
    set_startup_update_run_target_month,
    startup_update_run_matches_target_month,
    sync_startup_profile_from_company,
)
from founder_tools.models import VibeRaisingCompany, VibeRaisingProfile
from founder_tools.services import ensure_company_organization, set_active_company
from integrations.services.valley_harness import cancel_valley_run, notify_valley_run_created
from integrations.utils import normalize_domain
from .serializers import (
    VibeRaisingActiveCompanySerializer,
    VibeRaisingCompanySerializer,
    VibeRaisingCompanyUpsertSerializer,
    VibeRaisingMonthlyUpdateUpsertSerializer,
    VibeRaisingProfileSerializer,
    VibeRaisingProfileUpsertSerializer,
)


logger = logging.getLogger(__name__)

QUEUED_REDISPATCH_AFTER = timedelta(seconds=30)
VALLEY_META_KEY = "_valley_meta"
MAX_VIBE_RAISING_VIDEO_SIZE_BYTES = 250 * 1024 * 1024
MAX_VIBE_RAISING_MANUAL_DOCUMENT_SIZE_BYTES = 25 * 1024 * 1024
VIBE_RAISING_SIGNED_UPLOAD_TTL = timedelta(minutes=15)
VIBE_RAISING_SIGNED_READ_TTL = timedelta(minutes=5)
VIBE_RAISING_VIDEO_EXTENSION_CONTENT_TYPES = {
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".m4v": "video/x-m4v",
    ".webm": "video/webm",
    ".avi": "video/x-msvideo",
    ".mpeg": "video/mpeg",
    ".mpg": "video/mpeg",
    ".3gp": "video/3gpp",
    ".3g2": "video/3gpp2",
    ".ogv": "video/ogg",
    ".mkv": "video/x-matroska",
}
VIBE_RAISING_VIDEO_CONTENT_TYPES = set(VIBE_RAISING_VIDEO_EXTENSION_CONTENT_TYPES.values()) | {"video/mp4"}
VIBE_RAISING_MANUAL_DOCUMENT_EXTENSION_CONTENT_TYPES = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xlsm": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".csv": "text/csv",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".html": "text/html",
    ".htm": "text/html",
    ".rtf": "application/rtf",
    ".odt": "application/vnd.oasis.opendocument.text",
}
VIBE_RAISING_MANUAL_DOCUMENT_CONTENT_TYPES = set(VIBE_RAISING_MANUAL_DOCUMENT_EXTENSION_CONTENT_TYPES.values()) | {
    "application/vnd.ms-excel",
    "text/plain",
}
EMAIL_DRAFT_DISPLAY_STAGES = {
    "profile_resolution": "Preparing company context",
    "gmail_backfill": "Scanning recent Gmail messages",
    "relevance_classification": "Finding investor-relevant updates",
    "thread_hydration": "Pulling full thread context",
    "event_extraction": "Extracting metrics and highlights",
    "slack_backfill": "Scanning selected Slack channels",
    "slack_relevance_classification": "Filtering Slack highlights",
    "slack_event_extraction": "Extracting Slack highlights",
    "timeline_merge": "Building timeline",
    "draft_generation": "Drafting monthly updates",
    "groundedness_review": "Final review",
}
VIBE_RAISING_INPUT_SOURCE_KEYS = {
    "gmail",
    "stripe",
    "xero",
    "bank_feed",
    "notion",
    "google_drive",
    "slack",
    "linear",
    MANUAL_DOCUMENTS_SOURCE,
}
XERO_DRAFT_SYNC_STALE_AFTER = timedelta(minutes=15)


def _sync_selected_financial_sources_for_draft(user, input_sources: list[str]) -> dict[str, list[str]]:
    warnings: dict[str, list[str]] = {}
    selected = set(input_sources or [])
    if ExternalServiceProvider.XERO not in selected:
        return warnings

    connection = (
        ExternalServiceConnection.objects.filter(
            user=user,
            provider=ExternalServiceProvider.XERO,
        )
        .exclude(status=ExternalServiceConnectionStatus.DISCONNECTED)
        .order_by("-updated_at", "-id")
        .first()
    )
    if not connection:
        warnings[ExternalServiceProvider.XERO] = ["Xero was selected but no active Xero connection is available."]
        return warnings

    last_synced_at = connection.last_synced_at
    should_sync = (
        connection.status != ExternalServiceConnectionStatus.CONNECTED
        or last_synced_at is None
        or timezone.now() - last_synced_at > XERO_DRAFT_SYNC_STALE_AFTER
    )
    if not should_sync:
        return warnings

    try:
        payload = mark_sources_sync_requested(
            user,
            [ExternalServiceProvider.XERO],
            financial_only=True,
        )
    except Exception as exc:
        logger.exception("Unable to prepare Xero source before monthly update draft", extra={"user_id": user.id})
        warnings[ExternalServiceProvider.XERO] = [str(exc) or "Xero sync failed before draft generation."]
        return warnings

    sync_errors = [
        str(item.get("error") or item.get("warning") or "").strip()
        for item in payload.get("syncRuns", []) or payload.get("sync_runs", [])
        if str(item.get("status") or "").lower() == "error"
    ]
    if payload.get("status") == "error" or sync_errors:
        warnings[ExternalServiceProvider.XERO] = [
            item for item in sync_errors if item
        ] or ["Xero sync failed before draft generation."]
    return warnings


def _monthly_update_drafts_cover_input_sources(
    organization: Organization,
    input_sources: list[str],
    *,
    target_month: Optional[date] = None,
) -> bool:
    if MANUAL_DOCUMENTS_SOURCE in set(normalize_startup_update_input_sources(input_sources)):
        return False

    draft_queryset = organization.monthly_update_drafts.select_related("run")
    if target_month is not None:
        draft_queryset = draft_queryset.filter(month=target_month)
    draft = draft_queryset.order_by("-month", "-updated_at").first()
    if draft is None:
        return False

    requested_sources = set(normalize_startup_update_input_sources(input_sources))
    if draft.run is None:
        return requested_sources == {"gmail"}

    run_request = draft.run.run_request or {}
    draft_sources = set(normalize_startup_update_input_sources(run_request.get("input_sources")))
    return requested_sources.issubset(draft_sources)


def _month_start(value: date) -> date:
    return date(value.year, value.month, 1)


def _previous_month_start(month: date) -> date:
    if month.month == 1:
        return date(month.year - 1, 12, 1)
    return date(month.year, month.month - 1, 1)


def _refresh_reusable_xero_metrics_for_drafts(
    *,
    organization: Organization,
    input_sources: list[str],
    source_warnings: dict[str, list[str]],
    target_month: Optional[date] = None,
) -> None:
    selected = set(normalize_startup_update_input_sources(input_sources))
    if ExternalServiceProvider.XERO not in selected:
        return

    windows = build_startup_update_target_windows(target_month)
    try:
        summary = publish_xero_metric_observations(
            organization=organization,
            run=None,
            start_date=windows["financial_start_date"],
            end_date=windows["financial_end_date"],
        )
    except Exception as exc:
        logger.exception(
            "Unable to refresh Xero metrics for reusable monthly update drafts",
            extra={"organization_id": organization.id},
        )
        source_warnings.setdefault(ExternalServiceProvider.XERO, []).append(
            str(exc) or "Xero metrics could not be refreshed before loading existing drafts."
        )
        return

    for warning in summary.get("warnings", []) or []:
        text = str(warning or "").strip()
        if text:
            source_warnings.setdefault(ExternalServiceProvider.XERO, []).append(text)


def _get_profile_or_404(user):
    return get_object_or_404(
        VibeRaisingProfile.objects.select_related("active_company").prefetch_related("companies"),
        user=user,
    )


def _get_founder_profile_or_response(user):
    profile = _get_profile_or_404(user)
    if profile.role != VibeRaisingProfile.ROLE_FOUNDER:
        return None, Response(
            {"detail": "Only founders can access this endpoint."},
            status=status.HTTP_403_FORBIDDEN,
        )
    return profile, None


def _get_active_company(profile):
    company = profile.active_company or profile.companies.first()
    if company:
        ensure_company_organization(company)
    return company


def _serialize_company_summary(company):
    if not company:
        return None

    return {
        "id": str(company.id),
        "name": company.name,
        "domain": company.domain,
        "abn": company.abn,
        "registered": company.registered,
    }


def _serialize_binding_summary(binding):
    if not binding:
        return None

    return {
        "id": binding.id,
        "organizationId": binding.organization_id,
        "organizationDomain": binding.organization.domain,
        "googleConnectionId": binding.google_connection_id,
        "isDefaultForGmail": binding.is_default_for_gmail,
    }


def _run_failure_message(run) -> str:
    fallback = "Draft generation failed. Please try again."
    if run is None:
        return fallback
    error_text = str(run.error or "").strip()
    if error_text:
        return error_text
    for step in run.steps.order_by("display_order", "id"):
        if step.status == ContentFactoryStepStatus.FAILED and str(step.error or "").strip():
            return str(step.error).strip()
    return fallback


def _serialize_run_summary(run):
    if not run:
        return None
    target_month = get_startup_update_run_target_month(run)

    step_states = {}
    for step in run.steps.order_by("display_order", "id"):
        step_states[step.step_key] = {
            "name": step.step_key,
            "required": step.required,
            "status": step.status,
            "attempts": step.attempts,
            "message": step.message or None,
            "startedAt": step.started_at.isoformat() if step.started_at else None,
            "completedAt": step.completed_at.isoformat() if step.completed_at else None,
            "error": step.error or None,
            "artifacts": step.artifacts or [],
        }

    return {
        "runId": run.run_id,
        "workflow": run.workflow,
        "domain": run.domain,
        "status": run.status,
        "currentStep": run.current_step,
        "stepOrder": run.step_order or [],
        "stepStates": step_states,
        "targetMonth": target_month.isoformat() if target_month else None,
        "createdAt": run.created_at.isoformat(),
        "updatedAt": run.updated_at.isoformat(),
    }


def _frontend_base_url():
    for setting_name in ("VIBE_RAISING_URL", "DEFAULT_FRONTEND_URL"):
        value = str(getattr(settings, setting_name, "") or "").strip()
        if value:
            return value.rstrip("/")

    return "http://localhost:5173"


def _build_vibe_raising_frontend_next(path_or_url):
    frontend_base = _frontend_base_url()
    default_next = f"{frontend_base}/vibe-raising/create-update?email_draft=1"
    raw_next = str(path_or_url or "").strip()
    if not raw_next:
        return default_next

    parsed = urllib.parse.urlparse(raw_next)
    if parsed.scheme or parsed.netloc:
        allowed_base = urllib.parse.urlparse(frontend_base)
        if parsed.scheme != allowed_base.scheme or parsed.netloc != allowed_base.netloc:
            return default_next
        candidate = urllib.parse.urlunparse(("", "", parsed.path, "", parsed.query, ""))
    else:
        candidate = raw_next

    if not (
        candidate.startswith("/vibe-raising/connect-data")
        or candidate.startswith("/vibe-raising/create-update")
    ):
        return default_next

    return f"{frontend_base}{candidate}"


def _build_google_oauth_url(request):
    next_url = _build_vibe_raising_frontend_next(request.query_params.get("next"))
    connect_url = request.build_absolute_uri(reverse("google_connect"))
    return f"{connect_url}?{urllib.parse.urlencode({'next': next_url})}"


def _should_dispatch_existing_run(run) -> bool:
    if not bool(run) or run.status != ContentFactoryRunStatus.QUEUED:
        return False
    if _get_run_meta(run).get("dispatch_status") == "failed":
        return True
    return bool(run.updated_at) and run.updated_at <= timezone.now() - QUEUED_REDISPATCH_AFTER


def _valley_dispatch_failure_payload(run, dispatch_result) -> dict:
    return {
        "run_id": run.run_id,
        "runId": run.run_id,
        "error": "valley_dispatch_failed",
        "retryable": True,
        "message": "The update run was saved, but Valley could not be reached. Check Valley connectivity and retry.",
        "valleyDispatch": {
            "status": "failed",
            "failureKind": str(getattr(dispatch_result, "failure_kind", "") or "unknown"),
            "statusCode": getattr(dispatch_result, "status_code", None),
            "detail": str(getattr(dispatch_result, "detail", "") or "")[:300],
        },
    }


def _dispatch_run_to_valley(run):
    dispatch_result = notify_valley_run_created(run.run_id)
    record_valley_dispatch_result(run, dispatch_result)
    return dispatch_result


def _requested_target_month_from_request(request) -> date:
    raw_value = request.data.get("target_month") or request.data.get("targetMonth")
    return parse_startup_update_target_month(raw_value)


def _target_month_conflict_payload(*, requested_target_month: date, active_run) -> dict:
    active_target_month = get_startup_update_run_target_month(active_run)
    active_label = active_target_month.strftime("%B %Y") if active_target_month else "another month"
    requested_label = requested_target_month.strftime("%B %Y")
    return {
        "targetMonthConflict": True,
        "target_month_conflict": True,
        "requestedTargetMonth": requested_target_month.isoformat(),
        "requested_target_month": requested_target_month.isoformat(),
        "activeTargetMonth": active_target_month.isoformat() if active_target_month else None,
        "active_target_month": active_target_month.isoformat() if active_target_month else None,
        "error": f"{active_label} is already generating. Finish or cancel it before generating {requested_label}.",
    }


def _normalize_text_list(value):
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
        items = []
        for item in value:
            items.extend(_normalize_text_list(item))
        return items

    text = str(value).strip()
    return [text] if text else []


def _normalize_input_source_list(value):
    if value is None:
        return []

    if isinstance(value, str):
        raw_items = value.split(",")
    elif isinstance(value, (list, tuple, set)):
        raw_items = value
    else:
        return []

    normalized = []
    seen = set()
    for item in raw_items:
        key = str(item or "").strip().lower().replace("-", "_")
        if key in VIBE_RAISING_INPUT_SOURCE_KEYS and key not in seen:
            seen.add(key)
            normalized.append(key)
    return normalized


def _get_requested_input_sources(request):
    return _normalize_input_source_list(
        request.data.get("inputSources") or request.data.get("input_sources")
    )


def _get_requested_manual_document_ids(request):
    raw_items = request.data.get("manualDocumentIds")
    if raw_items is None:
        raw_items = request.data.get("manual_document_ids")
    if raw_items is None:
        return []
    if isinstance(raw_items, str):
        raw_items = [item.strip() for item in raw_items.split(",")]
    if not isinstance(raw_items, (list, tuple)):
        return []

    normalized = []
    seen = set()
    for item in raw_items:
        value = str(item or "").strip()
        if value and value not in seen:
            seen.add(value)
            normalized.append(value)
    return normalized


def _get_requested_manual_summary(request):
    return str(request.data.get("manualSummary") or request.data.get("manual_summary") or "").strip()


def _include_manual_source_if_needed(input_sources, *, manual_document_ids, manual_summary):
    selected = list(input_sources or [])
    if (manual_document_ids or manual_summary) and MANUAL_DOCUMENTS_SOURCE not in selected:
        selected.append(MANUAL_DOCUMENTS_SOURCE)
    return normalize_startup_update_input_sources(selected)


def _resolve_manual_documents_for_request(*, user, organization, company, document_ids):
    if not document_ids:
        return []

    documents_by_id = {
        str(document.id): document
        for document in StartupManualDocument.objects.select_related("company", "company__profile")
        .filter(organization=organization, company=company, id__in=document_ids)
    }
    documents = []
    missing_ids = []
    for document_id in document_ids:
        document = documents_by_id.get(str(document_id))
        if document is None or not _manual_document_access_allowed(user, document, active_company=company):
            missing_ids.append(str(document_id))
            continue
        documents.append(document)

    if missing_ids:
        raise ValueError("One or more manual documents were not found for this startup.")
    return documents


def _join_text_items(value):
    items = _normalize_text_list(value)
    if not items:
        return ""

    text = ". ".join(item.rstrip(". ") for item in items if item.strip())
    text = text.strip()
    if text and text[-1].isalnum():
        text += "."
    return text


def _join_text_items_with_newlines(value):
    items = _normalize_text_list(value)
    return "\n".join(item.strip() for item in items if item.strip())


def _join_named_sections(structured_memo, sections):
    lines = []
    for label, key in sections:
        for item in _normalize_text_list((structured_memo or {}).get(key)):
            prefix = f"{label}: " if label else ""
            lines.append(f"{prefix}{item}")
    return "\n".join(lines)


def _normalize_metric_value(value):
    if value is None:
        return None

    text = str(value).strip()
    return text or None


def _metric_key_from_label(label):
    return startup_update_metric_key(label)


MANUAL_METRIC_LABELS = STARTUP_UPDATE_METRIC_LABELS


def _extract_metrics(structured_memo):
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


def _extract_metric_suggestions(structured_memo):
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


def _structured_memo_section_text(structured_memo, key):
    return _join_text_items_with_newlines((structured_memo or {}).get(key))


def _structured_memo_manual_documents(structured_memo):
    documents = (structured_memo or {}).get("manual_documents") or (structured_memo or {}).get("manualDocuments") or []
    return documents if isinstance(documents, list) else []


def _structured_memo_with_xero_metrics(draft):
    structured_memo = draft.structured_memo or {}
    if not getattr(draft, "organization_id", None):
        return structured_memo

    merged_memo, _evidence_metric_ids = merge_xero_metrics_into_structured_memo(
        organization=draft.organization,
        month=draft.month,
        structured_memo=structured_memo,
        evidence_metric_ids=getattr(draft, "evidence_metric_ids", []) or [],
    )
    return merged_memo


def _serialize_draft_for_form(draft):
    structured_memo = _structured_memo_with_xero_metrics(draft)
    video_metadata = _structured_memo_video_metadata(structured_memo)
    month_value = draft.month
    return {
        "month": calendar.month_name[month_value.month],
        "year": month_value.year,
        "summary": _structured_memo_text(structured_memo, "summary", "topline"),
        "sourceUrl": _structured_memo_text(structured_memo, "sourceUrl", "source_url"),
        "manualDocuments": _structured_memo_manual_documents(structured_memo),
        "videoUrl": _structured_memo_video_url(structured_memo),
        "videoContentType": _structured_memo_text(video_metadata, "content_type", "contentType"),
        "videoOriginalFilename": _structured_memo_text(video_metadata, "original_filename", "originalFilename"),
        "videoStoragePath": _structured_memo_text(video_metadata, "storage_path", "storagePath"),
        "videoFileSizeBytes": video_metadata.get("file_size_bytes"),
        "highlights": _join_named_sections(structured_memo, [
            ("Financial performance", "financial_performance"),
            ("", "highlights"),
            ("Product / GTM / Team / Fundraising", "operations"),
        ]),
        "challenges": _join_text_items_with_newlines(structured_memo.get("lowlights")),
        "asks": _join_named_sections(structured_memo, [
            ("", "asks"),
        ]),
        "learnings": _structured_memo_section_text(structured_memo, "learnings"),
        "next30Days": _structured_memo_section_text(structured_memo, "next_30_days"),
        "metrics": _extract_metrics(structured_memo),
        "metricSuggestions": _extract_metric_suggestions(structured_memo),
    }


def _split_editor_text(value):
    return [
        item.strip()
        for item in str(value or "").replace("\r\n", "\n").split("\n")
        if item.strip()
    ]


def _optional_text(value):
    if value is None:
        return None

    text = str(value).strip()
    return text or None


def _structured_memo_text(structured_memo, *keys):
    for key in keys:
        value = _optional_text((structured_memo or {}).get(key))
        if value:
            return value
    return None


def _structured_memo_video_url(structured_memo):
    video_url = _structured_memo_text(structured_memo, "videoUrl", "video_url")
    if video_url:
        return video_url

    video_payload = (structured_memo or {}).get("video") or {}
    if isinstance(video_payload, dict):
        return _structured_memo_text(video_payload, "url", "videoUrl", "video_url")
    return None


def _structured_memo_video_metadata(structured_memo):
    video_payload = (structured_memo or {}).get("video") or {}
    return video_payload if isinstance(video_payload, dict) else {}


def _build_manual_kpi_snapshot(metrics):
    snapshot = []
    for metric_key, label in MANUAL_METRIC_LABELS.items():
        value = str((metrics or {}).get(metric_key) or "").strip()
        if not value:
            continue
        snapshot.append(
            {
                "metric_key": metric_key,
                "label": label,
                "value": value,
                "value_text": value,
            }
        )
    return snapshot


def _build_manual_structured_memo(payload):
    memo = {
        "highlights": _split_editor_text(payload.get("highlights")),
        "lowlights": _split_editor_text(payload.get("challenges")),
        "asks": _split_editor_text(payload.get("asks")),
        "learnings": _split_editor_text(payload.get("learnings")),
        "next_30_days": _split_editor_text(payload.get("next30Days")),
        "kpi_snapshot": _build_manual_kpi_snapshot(payload.get("metrics") or {}),
        "metric_suggestions": list(payload.get("metricSuggestions") or []),
    }

    summary = _optional_text(payload.get("summary"))
    manual_summary = _optional_text(payload.get("manualSummary"))
    source_url = _optional_text(payload.get("sourceUrl"))
    video_url = _optional_text(payload.get("videoUrl"))
    video_storage_path = _optional_text(payload.get("videoStoragePath"))
    video_content_type = _optional_text(payload.get("videoContentType"))
    video_original_filename = _optional_text(payload.get("videoOriginalFilename"))
    video_file_size_bytes = payload.get("videoFileSizeBytes")

    if summary:
        memo["summary"] = summary
    elif manual_summary:
        memo["summary"] = manual_summary
    if manual_summary:
        memo["manual_summary"] = manual_summary
    if source_url:
        memo["source_url"] = source_url
    if video_url:
        memo["video_url"] = video_url

    video = {}
    if video_url:
        video["url"] = video_url
    if video_storage_path:
        video["storage_path"] = video_storage_path
    if video_content_type:
        video["content_type"] = video_content_type
    if video_original_filename:
        video["original_filename"] = video_original_filename
    if video_file_size_bytes is not None:
        video["file_size_bytes"] = video_file_size_bytes
    if video:
        memo["video"] = video

    manual_documents = payload.get("manualDocuments") or []
    if manual_documents:
        memo["manual_documents"] = list(manual_documents)

    return memo


def _serialize_monthly_update(draft):
    structured_memo = _structured_memo_with_xero_metrics(draft)
    video_metadata = _structured_memo_video_metadata(structured_memo)
    return {
        "id": draft.id,
        "isoMonth": draft.month.isoformat(),
        "month": f"{calendar.month_name[draft.month.month]} {draft.month.year}",
        "monthName": calendar.month_name[draft.month.month],
        "year": draft.month.year,
        "date": draft.updated_at.isoformat(),
        "status": draft.status,
        "summary": _structured_memo_text(structured_memo, "summary", "topline"),
        "sourceUrl": _structured_memo_text(structured_memo, "sourceUrl", "source_url"),
        "manualDocuments": _structured_memo_manual_documents(structured_memo),
        "videoUrl": _structured_memo_video_url(structured_memo),
        "videoContentType": _structured_memo_text(video_metadata, "content_type", "contentType"),
        "videoOriginalFilename": _structured_memo_text(video_metadata, "original_filename", "originalFilename"),
        "videoStoragePath": _structured_memo_text(video_metadata, "storage_path", "storagePath"),
        "videoFileSizeBytes": video_metadata.get("file_size_bytes"),
        "metrics": _extract_metrics(structured_memo),
        "metricSuggestions": _extract_metric_suggestions(structured_memo),
        "highlights": _join_named_sections(structured_memo, [
            ("Financial performance", "financial_performance"),
            ("", "highlights"),
            ("Product / GTM / Team / Fundraising", "operations"),
        ]),
        "challenges": _join_text_items_with_newlines(structured_memo.get("lowlights")),
        "asks": _join_named_sections(structured_memo, [
            ("", "asks"),
        ]),
        "learnings": _structured_memo_section_text(structured_memo, "learnings"),
        "next30Days": _structured_memo_section_text(structured_memo, "next_30_days"),
    }


def _serialize_draft_bundle(drafts):
    if not drafts:
        return None

    current = _serialize_draft_for_form(drafts[0])
    past_months = []
    for draft in drafts[1:3]:
        structured_memo = _structured_memo_with_xero_metrics(draft)
        month_value = draft.month
        past_months.append(
            {
                "month": f"{calendar.month_name[month_value.month]} {month_value.year}",
                "highlights": _join_named_sections(structured_memo, [
                    ("Financial performance", "financial_performance"),
                    ("", "highlights"),
                    ("Product / GTM / Team / Fundraising", "operations"),
                ]),
                "challenges": _join_text_items_with_newlines(structured_memo.get("lowlights")),
                "asks": _join_named_sections(structured_memo, [
                    ("", "asks"),
                ]),
                "learnings": _structured_memo_section_text(structured_memo, "learnings"),
                "next30Days": _structured_memo_section_text(structured_memo, "next_30_days"),
                "metrics": _extract_metrics(structured_memo),
                "metricSuggestions": _extract_metric_suggestions(structured_memo),
            }
        )

    return {
        **current,
        "pastMonths": past_months,
    }


def _serialize_email_draft_month(draft):
    structured_memo = _structured_memo_with_xero_metrics(draft)
    video_metadata = _structured_memo_video_metadata(structured_memo)
    month_value = draft.month
    return {
        "draftId": draft.id,
        "isoMonth": month_value.isoformat(),
        "month": calendar.month_name[month_value.month],
        "year": month_value.year,
        "summary": _structured_memo_text(structured_memo, "summary", "topline"),
        "sourceUrl": _structured_memo_text(structured_memo, "sourceUrl", "source_url"),
        "manualDocuments": _structured_memo_manual_documents(structured_memo),
        "videoUrl": _structured_memo_video_url(structured_memo),
        "videoContentType": _structured_memo_text(video_metadata, "content_type", "contentType"),
        "videoOriginalFilename": _structured_memo_text(video_metadata, "original_filename", "originalFilename"),
        "videoStoragePath": _structured_memo_text(video_metadata, "storage_path", "storagePath"),
        "videoFileSizeBytes": video_metadata.get("file_size_bytes"),
        "metrics": _extract_metrics(structured_memo),
        "metricSuggestions": _extract_metric_suggestions(structured_memo),
        "highlights": _join_named_sections(structured_memo, [
            ("Financial performance", "financial_performance"),
            ("", "highlights"),
            ("Product / GTM / Team / Fundraising", "operations"),
        ]),
        "challenges": _join_text_items_with_newlines(structured_memo.get("lowlights")),
        "asks": _join_named_sections(structured_memo, [
            ("", "asks"),
        ]),
        "learnings": _structured_memo_section_text(structured_memo, "learnings"),
        "next30Days": _structured_memo_section_text(structured_memo, "next_30_days"),
    }


def _serialize_email_draft_bundle(drafts):
    if not drafts:
        return None

    current = _serialize_email_draft_month(drafts[0])
    past_months = [_serialize_email_draft_month(draft) for draft in reversed(drafts[1:])]
    return {
        "currentMonth": current,
        "pastMonths": past_months,
    }


def _serialize_email_draft_results_bundle(drafts):
    if not drafts:
        return None

    draft_payload = _serialize_draft_bundle(drafts)
    email_payload = _serialize_email_draft_bundle(drafts) or {}
    months = [*email_payload.get("pastMonths", []), email_payload.get("currentMonth")]
    return {
        "draft": draft_payload,
        "currentMonth": email_payload.get("currentMonth"),
        "pastMonths": email_payload.get("pastMonths", []),
        "months": [item for item in months if item],
    }


def _sync_startup_profile(*, startup_profile, organization, company, user):
    sync_startup_profile_from_company(
        startup_profile=startup_profile,
        organization=organization,
        company=company,
        user=user,
    )


def _get_founder_company_context_or_response(user):
    profile, error_response = _get_founder_profile_or_response(user)
    if error_response:
        return None, error_response

    company = _get_active_company(profile)
    if company is None:
        return None, Response(
            {"detail": "No active company found for this founder."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    domain = normalize_domain(company.domain or "")
    return {
        "profile": profile,
        "company": company,
        "domain": domain,
    }, None


def _ensure_binding_for_company(*, user, company):
    organization, startup_profile = resolve_or_create_profile(domain=company.domain)
    _sync_startup_profile(
        startup_profile=startup_profile,
        organization=organization,
        company=company,
        user=user,
    )
    binding = bind_user_to_startup(
        user=user,
        organization=organization,
        google_connection=getattr(user, "google_connection", None),
        role="founder",
        is_default_for_gmail=True,
    )
    return organization, startup_profile, binding


def _normalize_vibe_raising_video_content_type(*, content_type="", filename=""):
    content_type = str(content_type or "").split(";")[0].strip().lower()
    if content_type in VIBE_RAISING_VIDEO_CONTENT_TYPES:
        return content_type

    filename = str(filename or "")
    guessed_type, _encoding = mimetypes.guess_type(filename)
    if guessed_type and guessed_type in VIBE_RAISING_VIDEO_CONTENT_TYPES:
        return guessed_type

    extension = Path(filename).suffix.lower()
    return VIBE_RAISING_VIDEO_EXTENSION_CONTENT_TYPES.get(extension, "")


def _resolve_vibe_raising_video_content_type(uploaded_file):
    return _normalize_vibe_raising_video_content_type(
        content_type=getattr(uploaded_file, "content_type", ""),
        filename=getattr(uploaded_file, "name", ""),
    )


def _parse_vibe_raising_video_size(raw_size):
    try:
        size = int(raw_size)
    except (TypeError, ValueError):
        return None
    return size if size >= 0 else None


def _validate_vibe_raising_video_metadata(*, filename, content_type, file_size_bytes):
    resolved_content_type = _normalize_vibe_raising_video_content_type(
        content_type=content_type,
        filename=filename,
    )
    if not resolved_content_type:
        raise ValueError("Uploaded file must be a supported video.")

    parsed_size = _parse_vibe_raising_video_size(file_size_bytes)
    if parsed_size is None:
        raise ValueError("fileSizeBytes is required.")

    if parsed_size > MAX_VIBE_RAISING_VIDEO_SIZE_BYTES:
        raise ValueError(
            f"Video exceeds maximum size of {MAX_VIBE_RAISING_VIDEO_SIZE_BYTES // (1024 * 1024)} MB."
        )

    return resolved_content_type, parsed_size


def _vibe_raising_video_storage_prefix(*, user, organization):
    return os.path.join(
        "vibe-raising",
        "update-videos",
        f"org-{organization.id}",
        f"user-{user.id}",
    )


def _vibe_raising_video_storage_path(*, user, organization, filename, content_type):
    stem = slugify(Path(filename or "update-video").stem) or "update-video"
    extension = Path(filename or "").suffix.lower()
    if extension not in VIBE_RAISING_VIDEO_EXTENSION_CONTENT_TYPES:
        extension = mimetypes.guess_extension(content_type) or ".mp4"

    timestamp = timezone.now().strftime("%Y%m%d%H%M%S")
    return os.path.join(
        _vibe_raising_video_storage_prefix(user=user, organization=organization),
        f"{timestamp}-{stem}-{uuid4().hex[:8]}{extension}",
    )


def _store_vibe_raising_update_video(*, user, organization, video_file):
    content_type = _resolve_vibe_raising_video_content_type(video_file)
    if not content_type:
        raise ValueError("Uploaded file must be a supported video.")

    if video_file.size > MAX_VIBE_RAISING_VIDEO_SIZE_BYTES:
        raise ValueError(
            f"Video exceeds maximum size of {MAX_VIBE_RAISING_VIDEO_SIZE_BYTES // (1024 * 1024)} MB."
        )

    filename = video_file.name or "update-video"
    storage_path = _vibe_raising_video_storage_path(
        user=user,
        organization=organization,
        filename=filename,
        content_type=content_type,
    )
    video_url = upload_file_to_storage(video_file, storage_path, content_type=content_type)

    return {
        "videoUrl": video_url,
        "storagePath": storage_path,
        "contentType": content_type,
        "fileSizeBytes": video_file.size,
        "originalFilename": filename,
    }


def _create_vibe_raising_signed_video_upload(*, user, organization, filename, content_type, file_size_bytes):
    resolved_content_type, parsed_size = _validate_vibe_raising_video_metadata(
        filename=filename,
        content_type=content_type,
        file_size_bytes=file_size_bytes,
    )
    storage_path = _vibe_raising_video_storage_path(
        user=user,
        organization=organization,
        filename=filename,
        content_type=resolved_content_type,
    )
    upload_url = create_signed_upload_url(
        storage_path,
        resolved_content_type,
        expires_in=VIBE_RAISING_SIGNED_UPLOAD_TTL,
    )
    expires_at = timezone.now() + VIBE_RAISING_SIGNED_UPLOAD_TTL
    return {
        "uploadUrl": upload_url,
        "storagePath": storage_path,
        "contentType": resolved_content_type,
        "fileSizeBytes": parsed_size,
        "expiresAt": expires_at.isoformat(),
        "maxUploadBytes": MAX_VIBE_RAISING_VIDEO_SIZE_BYTES,
        "requiredHeaders": {"Content-Type": resolved_content_type},
    }


def _complete_vibe_raising_signed_video_upload(
    *,
    user,
    organization,
    storage_path,
    filename,
    content_type,
    file_size_bytes,
):
    expected_prefix = f"{_vibe_raising_video_storage_prefix(user=user, organization=organization)}/"
    if not str(storage_path or "").startswith(expected_prefix):
        raise ValueError("Invalid upload path.")

    resolved_content_type, parsed_size = _validate_vibe_raising_video_metadata(
        filename=filename,
        content_type=content_type,
        file_size_bytes=file_size_bytes,
    )
    finalized = finalize_uploaded_storage_object(storage_path, content_type=resolved_content_type)
    actual_size = int(finalized.get("fileSizeBytes") or parsed_size)
    if actual_size > MAX_VIBE_RAISING_VIDEO_SIZE_BYTES:
        raise ValueError(
            f"Video exceeds maximum size of {MAX_VIBE_RAISING_VIDEO_SIZE_BYTES // (1024 * 1024)} MB."
        )
    if parsed_size and actual_size and parsed_size != actual_size:
        raise ValueError("Uploaded video size does not match the finalized object.")

    return {
        "videoUrl": finalized["url"],
        "storagePath": storage_path,
        "contentType": finalized.get("contentType") or resolved_content_type,
        "fileSizeBytes": actual_size,
        "originalFilename": filename or "update-video",
    }


def _is_admin_user(user):
    return bool(getattr(user, "is_staff", False) or getattr(user, "is_superuser", False))


def _normalize_vibe_raising_manual_document_content_type(*, content_type="", filename=""):
    content_type = str(content_type or "").split(";")[0].strip().lower()
    if content_type in VIBE_RAISING_MANUAL_DOCUMENT_CONTENT_TYPES:
        return content_type

    filename = str(filename or "")
    guessed_type, _encoding = mimetypes.guess_type(filename)
    if guessed_type and guessed_type in VIBE_RAISING_MANUAL_DOCUMENT_CONTENT_TYPES:
        return guessed_type

    extension = Path(filename or "").suffix.lower()
    return VIBE_RAISING_MANUAL_DOCUMENT_EXTENSION_CONTENT_TYPES.get(extension, "")


def _parse_vibe_raising_manual_document_size(raw_size):
    try:
        size = int(raw_size)
    except (TypeError, ValueError):
        return None
    return size if size >= 0 else None


def _validate_vibe_raising_manual_document_metadata(*, filename, content_type, file_size_bytes):
    resolved_content_type = _normalize_vibe_raising_manual_document_content_type(
        content_type=content_type,
        filename=filename,
    )
    if not resolved_content_type:
        raise ValueError("Uploaded file must be a supported document.")

    parsed_size = _parse_vibe_raising_manual_document_size(file_size_bytes)
    if parsed_size is None:
        raise ValueError("fileSizeBytes is required.")

    if parsed_size > MAX_VIBE_RAISING_MANUAL_DOCUMENT_SIZE_BYTES:
        raise ValueError(
            f"Document exceeds maximum size of {MAX_VIBE_RAISING_MANUAL_DOCUMENT_SIZE_BYTES // (1024 * 1024)} MB."
        )

    return resolved_content_type, parsed_size


def _vibe_raising_manual_document_storage_prefix(*, user, organization, company):
    return os.path.join(
        "vibe-raising",
        "manual-documents",
        f"org-{organization.id}",
        f"company-{company.id}",
        f"user-{user.id}",
    )


def _vibe_raising_manual_document_storage_path(*, user, organization, company, filename, content_type):
    stem = slugify(Path(filename or "manual-document").stem) or "manual-document"
    extension = Path(filename or "").suffix.lower()
    if extension not in VIBE_RAISING_MANUAL_DOCUMENT_EXTENSION_CONTENT_TYPES:
        extension = mimetypes.guess_extension(content_type) or ".txt"

    timestamp = timezone.now().strftime("%Y%m%d%H%M%S")
    return os.path.join(
        _vibe_raising_manual_document_storage_prefix(user=user, organization=organization, company=company),
        f"{timestamp}-{stem}-{uuid4().hex[:8]}{extension}",
    )


def _create_vibe_raising_signed_manual_document_upload(
    *,
    user,
    organization,
    company,
    filename,
    content_type,
    file_size_bytes,
):
    resolved_content_type, parsed_size = _validate_vibe_raising_manual_document_metadata(
        filename=filename,
        content_type=content_type,
        file_size_bytes=file_size_bytes,
    )
    storage_path = _vibe_raising_manual_document_storage_path(
        user=user,
        organization=organization,
        company=company,
        filename=filename,
        content_type=resolved_content_type,
    )
    upload_url = create_signed_upload_url(
        storage_path,
        resolved_content_type,
        expires_in=VIBE_RAISING_SIGNED_UPLOAD_TTL,
    )
    expires_at = timezone.now() + VIBE_RAISING_SIGNED_UPLOAD_TTL
    return {
        "uploadUrl": upload_url,
        "storagePath": storage_path,
        "contentType": resolved_content_type,
        "fileSizeBytes": parsed_size,
        "expiresAt": expires_at.isoformat(),
        "maxUploadBytes": MAX_VIBE_RAISING_MANUAL_DOCUMENT_SIZE_BYTES,
        "requiredHeaders": {"Content-Type": resolved_content_type},
    }


def _serialize_manual_document(document: StartupManualDocument):
    return {
        "id": str(document.id),
        "originalFilename": document.original_filename,
        "contentType": document.content_type,
        "fileSizeBytes": document.file_size_bytes,
        "extractionStatus": document.extraction_status,
        "textSizeChars": document.text_size_chars,
        "parseNotes": document.parse_notes,
        "lastError": document.last_error,
        "createdAt": document.created_at.isoformat() if document.created_at else None,
        "updatedAt": document.updated_at.isoformat() if document.updated_at else None,
    }


def _serialize_manual_document_for_memo(document: StartupManualDocument):
    return {
        "id": str(document.id),
        "original_filename": document.original_filename,
        "content_type": document.content_type,
        "file_size_bytes": document.file_size_bytes,
        "extraction_status": document.extraction_status,
        "text_size_chars": document.text_size_chars,
        "created_at": document.created_at.isoformat() if document.created_at else None,
    }


def _get_manual_document_company_context_or_response(request):
    requested_company_id = (
        str(request.data.get("companyId") or request.data.get("company_id") or "").strip()
        if hasattr(request, "data")
        else ""
    ) or str(request.query_params.get("companyId") or request.query_params.get("company_id") or "").strip()

    if requested_company_id and _is_admin_user(request.user):
        company = get_object_or_404(
            VibeRaisingCompany.objects.select_related("profile", "profile__user", "organization"),
            id=requested_company_id,
        )
        domain = normalize_domain(company.domain or "")
        if not domain:
            return None, Response(
                {"detail": "Add a company domain before uploading documents."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        organization, _startup_profile, binding = _ensure_binding_for_company(
            user=company.profile.user,
            company=company,
        )
        return {
            "profile": company.profile,
            "company": company,
            "domain": domain,
            "organization": organization,
            "binding": binding,
        }, None

    context, error_response = _get_founder_company_context_or_response(request.user)
    if error_response:
        return None, error_response
    if not context["domain"]:
        return None, Response(
            {"detail": "Add a company domain before uploading documents."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    organization, _startup_profile, binding = _ensure_binding_for_company(
        user=request.user,
        company=context["company"],
    )
    return {
        **context,
        "organization": organization,
        "binding": binding,
    }, None


def _manual_document_access_allowed(user, document: StartupManualDocument, *, active_company=None):
    if _is_admin_user(user):
        return True
    if active_company is None or document.company_id != active_company.id:
        return False
    return document.created_by_id == user.id or document.company.profile.user_id == user.id


def _get_accessible_manual_document_or_response(request, document_id):
    document = get_object_or_404(
        StartupManualDocument.objects.select_related("organization", "company", "company__profile"),
        id=document_id,
    )
    if _is_admin_user(request.user):
        return document, None

    context, error_response = _get_founder_company_context_or_response(request.user)
    if error_response:
        return None, error_response
    if not _manual_document_access_allowed(request.user, document, active_company=context["company"]):
        return None, Response({"detail": "Document not found."}, status=status.HTTP_404_NOT_FOUND)
    return document, None


def _complete_vibe_raising_signed_manual_document_upload(
    *,
    user,
    organization,
    company,
    storage_path,
    filename,
    content_type,
    file_size_bytes,
):
    expected_prefix = f"{_vibe_raising_manual_document_storage_prefix(user=user, organization=organization, company=company)}/"
    if not str(storage_path or "").startswith(expected_prefix):
        raise ValueError("Invalid upload path.")

    resolved_content_type, parsed_size = _validate_vibe_raising_manual_document_metadata(
        filename=filename,
        content_type=content_type,
        file_size_bytes=file_size_bytes,
    )
    finalized = finalize_private_uploaded_storage_object(storage_path, content_type=resolved_content_type)
    actual_size = int(finalized.get("fileSizeBytes") or parsed_size)
    if actual_size > MAX_VIBE_RAISING_MANUAL_DOCUMENT_SIZE_BYTES:
        raise ValueError(
            f"Document exceeds maximum size of {MAX_VIBE_RAISING_MANUAL_DOCUMENT_SIZE_BYTES // (1024 * 1024)} MB."
        )
    if parsed_size and actual_size and parsed_size != actual_size:
        raise ValueError("Uploaded document size does not match the finalized object.")

    raw_bytes = download_storage_object_bytes(storage_path)
    parsed = parse_manual_document(
        filename=filename or "manual-document",
        content_type=finalized.get("contentType") or resolved_content_type,
        raw_bytes=raw_bytes,
    )
    document = StartupManualDocument.objects.create(
        organization=organization,
        company=company,
        created_by=user,
        original_filename=filename or "manual-document",
        content_type=finalized.get("contentType") or resolved_content_type,
        file_size_bytes=actual_size,
        storage_path=storage_path,
        extraction_status=parsed.extraction_status,
        extracted_text=parsed.extracted_text,
        text_size_chars=len(parsed.extracted_text or ""),
        parse_notes=parsed.parse_notes,
        last_error=parsed.last_error,
        metadata={
            "storage_updated": finalized.get("updated"),
        },
    )
    return document


def _get_run_result_payload(run) -> dict:
    payload = run.result or {}
    return payload if isinstance(payload, dict) else {}


def _get_run_meta(run) -> dict:
    payload = _get_run_result_payload(run)
    meta = payload.get(VALLEY_META_KEY) or {}
    return meta if isinstance(meta, dict) else {}


def _get_run_generated_draft_months(run) -> list[str]:
    payload = _get_run_result_payload(run)
    raw_months = payload.get("generated_draft_months") or (run.run_request or {}).get("draft_months") or []
    if not isinstance(raw_months, (list, tuple)):
        return []

    months = []
    for item in raw_months:
        text = str(item or "").strip()
        if text:
            months.append(text)
    return months


def _get_email_draft_display_stage(current_step: Optional[str]) -> str:
    step_key = str(current_step or "").strip() or RUN_STEP_ORDER[0]
    return EMAIL_DRAFT_DISPLAY_STAGES.get(step_key, "Preparing company context")


def _count_completed_run_steps(run) -> tuple[int, int]:
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


def _serialize_run_progress(run):
    if not run:
        return None

    completed_steps, total_steps = _count_completed_run_steps(run)
    meta = _get_run_meta(run)
    target_month = get_startup_update_run_target_month(run)
    return {
        "runId": run.run_id,
        "status": run.status,
        "currentStep": run.current_step,
        "completedSteps": completed_steps,
        "totalSteps": total_steps,
        "displayStage": _get_email_draft_display_stage(run.current_step),
        "lastHeartbeatAt": meta.get("last_heartbeat_at"),
        "canRetry": run.status in {
            ContentFactoryRunStatus.FAILED,
            ContentFactoryRunStatus.DENIED,
        },
        "terminalState": (
            run.status
            if run.status in {
                ContentFactoryRunStatus.COMPLETED,
                ContentFactoryRunStatus.FAILED,
                ContentFactoryRunStatus.DENIED,
                ContentFactoryRunStatus.CANCELLED,
            }
            else None
        ),
        "generatedDraftMonths": _get_run_generated_draft_months(run),
        "targetMonth": target_month.isoformat() if target_month else None,
    }


def _get_recent_drafts_for_organization(organization):
    if organization is None:
        return []
    return list(organization.monthly_update_drafts.order_by("-month", "-updated_at")[:3])


def _get_drafts_for_run(run):
    if run is None:
        return []
    return list(run.monthly_update_drafts.order_by("-month", "-updated_at"))


def _build_status_payload(*, user, company, domain):
    google_connection = getattr(user, "google_connection", None)
    google_connected = bool(google_connection)
    google_scope_status = gmail_scope_status_payload(google_connection)
    google_connection_id = getattr(google_connection, "id", None)
    company_payload = _serialize_company_summary(company)

    if not domain:
        return {
            "state": "needs_domain",
            "googleConnected": google_connected,
            **google_scope_status,
            "company": company_payload,
            "run": None,
            "draft": None,
            "error": "Add a company domain before connecting Gmail.",
        }

    binding = get_default_binding_for_domain(user=user, domain=domain)
    organization = binding.organization if binding else Organization.objects.filter(domain=domain).first()
    open_run = (
        get_open_startup_update_run(
            organization=organization,
            google_connection_id=google_connection_id,
        )
        if organization
        else None
    )
    latest_run = (
        get_latest_startup_update_run(
            organization=organization,
            google_connection_id=google_connection_id,
        )
        if organization
        else None
    )
    drafts = (
        list(organization.monthly_update_drafts.order_by("-month", "-updated_at")[:3])
        if organization
        else []
    )
    draft_payload = _serialize_draft_bundle(drafts)
    latest_run_requires_gmail = (
        gmail_required_for_sources((latest_run.run_request or {}).get("input_sources"))
        if latest_run is not None
        else True
    )

    error = None
    if latest_run_requires_gmail and not google_scope_status["hasGmailScope"]:
        state = "needs_google_auth"
    elif open_run is not None:
        state = "processing"
    elif latest_run and latest_run.status == ContentFactoryRunStatus.CANCELLED:
        state = "cancelled"
    elif latest_run and latest_run.status in {
        ContentFactoryRunStatus.FAILED,
        ContentFactoryRunStatus.DENIED,
    }:
        state = "failed"
        error = _run_failure_message(latest_run)
    elif draft_payload:
        state = "ready"
    elif latest_run and latest_run.status == ContentFactoryRunStatus.COMPLETED:
        state = "failed"
        error = "Draft generation completed without producing a draft."
    else:
        state = "failed"
        error = "Draft generation has not started yet."

    return {
        "state": state,
        "googleConnected": google_connected,
        **google_scope_status,
        "company": company_payload,
        "run": _serialize_run_summary(open_run or latest_run),
        "draft": draft_payload,
        "error": error,
    }


def _build_email_draft_payload(
    *,
    request,
    user,
    company,
    domain,
    run_id: Optional[str] = None,
    target_month: Optional[date] = None,
):
    google_connection = getattr(user, "google_connection", None)
    google_connected = bool(google_connection)
    google_scope_status = gmail_scope_status_payload(google_connection)
    google_connection_id = getattr(google_connection, "id", None)
    company_payload = _serialize_company_summary(company)
    auth_url = _build_google_oauth_url(request)
    requested_target_month = target_month

    if not domain:
        return {
            "state": "needs_domain",
            "gmailConnected": google_connected,
            **google_scope_status,
            "company": company_payload,
            "authUrl": auth_url,
            "run": None,
            "draft": None,
            "runId": None,
            "status": None,
            "currentStep": None,
            "stepStates": {},
            "currentMonth": None,
            "pastMonths": [],
            "targetMonth": requested_target_month.isoformat() if requested_target_month else None,
            "requestedTargetMonth": requested_target_month.isoformat() if requested_target_month else None,
            "error": "Add a company domain before connecting Gmail.",
        }

    binding = get_default_binding_for_domain(user=user, domain=domain)
    organization = binding.organization if binding else Organization.objects.filter(domain=domain).first()
    latest_run = None
    selected_run = None
    drafts = []
    if organization is not None:
        run_queryset = ContentFactoryRun.objects.filter(
            workflow=STARTUP_UPDATE_WORKFLOW,
            domain=organization.domain,
        ).order_by("-updated_at")
        selected_run = run_queryset.filter(run_id=run_id).first() if run_id else None
        latest_matching_run = None
        if requested_target_month is not None:
            for candidate in run_queryset:
                if startup_update_run_matches_target_month(candidate, requested_target_month):
                    latest_matching_run = candidate
                    break
        latest_run = selected_run or get_open_startup_update_run(
            organization=organization,
            google_connection_id=google_connection_id,
            target_month=requested_target_month,
        )
        if latest_run is None:
            latest_run = latest_matching_run if requested_target_month is not None else get_latest_startup_update_run(
                organization=organization,
                google_connection_id=google_connection_id,
            )
        drafts = _get_drafts_for_run(latest_run)
        if requested_target_month is not None:
            target_drafts = list(
                organization.monthly_update_drafts.filter(month=requested_target_month).order_by("-month", "-updated_at")
            )
            if target_drafts:
                drafts = target_drafts
        if not drafts and latest_run is None and selected_run is None and requested_target_month is None:
            drafts = _get_recent_drafts_for_organization(organization)

    draft_payload = _serialize_draft_bundle(drafts)
    email_draft_payload = _serialize_email_draft_bundle(drafts)
    run_payload = _serialize_run_summary(latest_run)
    progress_payload = _serialize_run_progress(latest_run)
    active_target_month = get_startup_update_run_target_month(latest_run) if latest_run is not None else None
    resolved_target_month = requested_target_month or active_target_month
    latest_run_requires_gmail = (
        gmail_required_for_sources((latest_run.run_request or {}).get("input_sources"))
        if latest_run is not None
        else True
    )

    error = None
    if latest_run_requires_gmail and not google_scope_status["hasGmailScope"]:
        state = "auth_required"
    elif latest_run is not None and latest_run.status == ContentFactoryRunStatus.QUEUED:
        state = "queued"
    elif latest_run is not None and latest_run.status in {
        ContentFactoryRunStatus.RUNNING,
        ContentFactoryRunStatus.BLOCKED,
        ContentFactoryRunStatus.AWAITING_CONFIRMATION,
        ContentFactoryRunStatus.AWAITING_APPROVAL,
        ContentFactoryRunStatus.AWAITING_DELIVERY_MODE,
        ContentFactoryRunStatus.APPROVAL_REQUIRED,
    }:
        state = "running"
    elif latest_run is not None and latest_run.status == ContentFactoryRunStatus.CANCELLED:
        state = "cancelled"
    elif latest_run is not None and latest_run.status == ContentFactoryRunStatus.COMPLETED and email_draft_payload:
        state = "completed"
    elif latest_run and latest_run.status == ContentFactoryRunStatus.COMPLETED:
        state = "failed"
        error = "Draft generation completed without producing a draft."
    elif latest_run and latest_run.status in {
        ContentFactoryRunStatus.FAILED,
        ContentFactoryRunStatus.DENIED,
    }:
        state = "failed"
        error = _run_failure_message(latest_run)
    elif email_draft_payload:
        state = "completed"
    else:
        state = "failed"
        error = "Draft generation has not started yet."

    return {
        "state": state,
        "gmailConnected": google_connected,
        **google_scope_status,
        "company": company_payload,
        "binding": _serialize_binding_summary(binding),
        "authUrl": auth_url,
        "run": run_payload,
        "progress": progress_payload,
        "draft": draft_payload,
        "runId": run_payload["runId"] if run_payload else None,
        "status": run_payload["status"] if run_payload else None,
        "currentStep": run_payload["currentStep"] if run_payload else None,
        "stepStates": (run_payload or {}).get("stepStates", {}),
        "completedSteps": progress_payload["completedSteps"] if progress_payload else 0,
        "totalSteps": progress_payload["totalSteps"] if progress_payload else len(RUN_STEP_ORDER),
        "displayStage": progress_payload["displayStage"] if progress_payload else _get_email_draft_display_stage(None),
        "lastHeartbeatAt": progress_payload["lastHeartbeatAt"] if progress_payload else None,
        "canRetry": progress_payload["canRetry"] if progress_payload else False,
        "terminalState": progress_payload["terminalState"] if progress_payload else None,
        "generatedDraftMonths": progress_payload["generatedDraftMonths"] if progress_payload else [],
        "targetMonth": resolved_target_month.isoformat() if resolved_target_month else None,
        "requestedTargetMonth": requested_target_month.isoformat() if requested_target_month else None,
        "activeTargetMonth": active_target_month.isoformat() if active_target_month else None,
        "targetMonthConflict": False,
        "currentMonth": (email_draft_payload or {}).get("currentMonth"),
        "pastMonths": (email_draft_payload or {}).get("pastMonths", []),
        "error": error,
    }


def _build_email_draft_results_payload(*, request, user, company, domain, run_id: Optional[str] = None):
    payload = _build_email_draft_payload(
        request=request,
        user=user,
        company=company,
        domain=domain,
        run_id=run_id,
    )

    draft_results = _serialize_email_draft_results_bundle(
        _get_drafts_for_run(
            ContentFactoryRun.objects.filter(
                workflow=STARTUP_UPDATE_WORKFLOW,
                run_id=run_id,
            ).first()
        )
    ) if run_id else None

    if draft_results is None and payload.get("draft") is not None:
        draft_results = {
            "draft": payload.get("draft"),
            "currentMonth": payload.get("currentMonth"),
            "pastMonths": payload.get("pastMonths", []),
            "months": [*payload.get("pastMonths", []), payload.get("currentMonth")],
        }

    return {
        **payload,
        "currentMonth": (draft_results or {}).get("currentMonth"),
        "pastMonths": (draft_results or {}).get("pastMonths", []),
        "months": [item for item in (draft_results or {}).get("months", []) if item],
        "draft": (draft_results or {}).get("draft"),
    }


class VibeRaisingProfileView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        profile = VibeRaisingProfile.objects.select_related("active_company").prefetch_related("companies").filter(
            user=request.user
        ).first()
        if not profile:
            return Response({"detail": "Profile not found."}, status=status.HTTP_404_NOT_FOUND)

        return Response(VibeRaisingProfileSerializer(profile).data, status=status.HTTP_200_OK)

    def post(self, request):
        return self._upsert(request)

    def put(self, request):
        return self._upsert(request)

    def _upsert(self, request):
        serializer = VibeRaisingProfileUpsertSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        with transaction.atomic():
            profile, _created = VibeRaisingProfile.objects.get_or_create(
                user=request.user,
                defaults={
                    "role": serializer.validated_data["role"],
                    "organization_name": serializer.validated_data["organization_name"],
                },
            )

            profile.role = serializer.validated_data["role"]
            profile.organization_name = serializer.validated_data["organization_name"]
            if profile.role == VibeRaisingProfile.ROLE_INVESTOR:
                profile.active_company = None
            profile.save()

        profile = _get_profile_or_404(request.user)
        return Response(VibeRaisingProfileSerializer(profile).data, status=status.HTTP_200_OK)


class VibeRaisingCompanyView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        profile, error_response = _get_founder_profile_or_response(request.user)
        if error_response:
            return error_response

        serializer = VibeRaisingCompanyUpsertSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        company_id = serializer.validated_data.get("companyId")

        with transaction.atomic():
            if company_id:
                company = get_object_or_404(VibeRaisingCompany, pk=company_id, profile=profile)
                company.name = serializer.validated_data["name"]
                if "domain" in serializer.validated_data:
                    company.domain = serializer.validated_data["domain"]
                if "abn" in serializer.validated_data:
                    company.abn = serializer.validated_data["abn"]
                if "registered" in serializer.validated_data:
                    company.registered = serializer.validated_data["registered"]
                company.save()
            else:
                company = profile.companies.filter(
                    name__iexact=serializer.validated_data["name"]
                ).first()
                if company is None:
                    company = VibeRaisingCompany.objects.create(
                        profile=profile,
                        name=serializer.validated_data["name"],
                        domain=serializer.validated_data.get("domain"),
                        abn=serializer.validated_data.get("abn"),
                        registered=serializer.validated_data.get("registered", False),
                    )
                else:
                    company.name = serializer.validated_data["name"]
                    if "domain" in serializer.validated_data:
                        company.domain = serializer.validated_data["domain"]
                    if "abn" in serializer.validated_data:
                        company.abn = serializer.validated_data["abn"]
                    if "registered" in serializer.validated_data:
                        company.registered = serializer.validated_data["registered"]
                    company.save()

                ensure_company_organization(company)
                if profile.active_company_id is None:
                    set_active_company(profile, company)

            ensure_company_organization(company)

        return Response(VibeRaisingCompanySerializer(company).data, status=status.HTTP_200_OK)


class VibeRaisingActiveCompanyView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        profile, error_response = _get_founder_profile_or_response(request.user)
        if error_response:
            return error_response

        serializer = VibeRaisingActiveCompanySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        company = get_object_or_404(
            VibeRaisingCompany,
            pk=serializer.validated_data["companyId"],
            profile=profile,
        )

        if profile.active_company_id != company.id:
            set_active_company(profile, company)

        return Response(status=status.HTTP_204_NO_CONTENT)


class VibeRaisingVideoUploadView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        context, error_response = _get_founder_company_context_or_response(request.user)
        if error_response:
            return error_response

        if not context["domain"]:
            return Response(
                {"detail": "Add a company domain before uploading videos."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        video_file = request.FILES.get("video")
        if video_file is None:
            return Response({"detail": "video is required"}, status=status.HTTP_400_BAD_REQUEST)

        company = context["company"]
        organization, _startup_profile, _binding = _ensure_binding_for_company(
            user=request.user,
            company=company,
        )

        try:
            payload = _store_vibe_raising_update_video(
                user=request.user,
                organization=organization,
                video_file=video_file,
            )
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as exc:
            logger.exception("Failed to upload Vibe Raising update video")
            return Response(
                {"detail": f"Failed to upload video: {exc}"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        return Response(payload, status=status.HTTP_201_CREATED)


class VibeRaisingVideoUploadSessionView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        context, error_response = _get_founder_company_context_or_response(request.user)
        if error_response:
            return error_response

        if not context["domain"]:
            return Response(
                {"detail": "Add a company domain before uploading videos."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        original_filename = str(request.data.get("originalFilename") or "").strip() or "update-video"
        content_type = str(request.data.get("contentType") or "").strip()
        file_size_bytes = request.data.get("fileSizeBytes")

        company = context["company"]
        organization, _startup_profile, _binding = _ensure_binding_for_company(
            user=request.user,
            company=company,
        )

        try:
            payload = _create_vibe_raising_signed_video_upload(
                user=request.user,
                organization=organization,
                filename=original_filename,
                content_type=content_type,
                file_size_bytes=file_size_bytes,
            )
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as exc:
            logger.exception("Failed to create Vibe Raising video upload session")
            return Response(
                {"detail": f"Failed to create upload session: {exc}"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        return Response(payload, status=status.HTTP_201_CREATED)


class VibeRaisingVideoUploadCompleteView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        context, error_response = _get_founder_company_context_or_response(request.user)
        if error_response:
            return error_response

        if not context["domain"]:
            return Response(
                {"detail": "Add a company domain before uploading videos."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        storage_path = str(request.data.get("storagePath") or "").strip()
        original_filename = str(request.data.get("originalFilename") or "").strip() or "update-video"
        content_type = str(request.data.get("contentType") or "").strip()
        file_size_bytes = request.data.get("fileSizeBytes")
        if not storage_path:
            return Response({"detail": "storagePath is required."}, status=status.HTTP_400_BAD_REQUEST)

        company = context["company"]
        organization, _startup_profile, _binding = _ensure_binding_for_company(
            user=request.user,
            company=company,
        )

        try:
            payload = _complete_vibe_raising_signed_video_upload(
                user=request.user,
                organization=organization,
                storage_path=storage_path,
                filename=original_filename,
                content_type=content_type,
                file_size_bytes=file_size_bytes,
            )
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except FileNotFoundError:
            return Response(
                {"detail": "Uploaded video was not found in storage. Please upload it again."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        except Exception as exc:
            logger.exception("Failed to finalize Vibe Raising update video")
            return Response(
                {"detail": f"Failed to finalize video upload: {exc}"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        return Response(payload, status=status.HTTP_200_OK)


class VibeRaisingManualDocumentListView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        context, error_response = _get_manual_document_company_context_or_response(request)
        if error_response:
            return error_response

        documents = StartupManualDocument.objects.filter(
            organization=context["organization"],
            company=context["company"],
        ).order_by("-created_at")
        return Response(
            {"documents": [_serialize_manual_document(document) for document in documents]},
            status=status.HTTP_200_OK,
        )


class VibeRaisingManualDocumentUploadSessionView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        context, error_response = _get_manual_document_company_context_or_response(request)
        if error_response:
            return error_response

        original_filename = str(request.data.get("originalFilename") or "").strip() or "manual-document"
        content_type = str(request.data.get("contentType") or "").strip()
        file_size_bytes = request.data.get("fileSizeBytes")

        try:
            payload = _create_vibe_raising_signed_manual_document_upload(
                user=request.user,
                organization=context["organization"],
                company=context["company"],
                filename=original_filename,
                content_type=content_type,
                file_size_bytes=file_size_bytes,
            )
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as exc:
            logger.exception("Failed to create Vibe Raising manual document upload session")
            return Response(
                {"detail": f"Failed to create upload session: {exc}"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        return Response(payload, status=status.HTTP_201_CREATED)


class VibeRaisingManualDocumentUploadCompleteView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        context, error_response = _get_manual_document_company_context_or_response(request)
        if error_response:
            return error_response

        storage_path = str(request.data.get("storagePath") or "").strip()
        original_filename = str(request.data.get("originalFilename") or "").strip() or "manual-document"
        content_type = str(request.data.get("contentType") or "").strip()
        file_size_bytes = request.data.get("fileSizeBytes")
        if not storage_path:
            return Response({"detail": "storagePath is required."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            document = _complete_vibe_raising_signed_manual_document_upload(
                user=request.user,
                organization=context["organization"],
                company=context["company"],
                storage_path=storage_path,
                filename=original_filename,
                content_type=content_type,
                file_size_bytes=file_size_bytes,
            )
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except FileNotFoundError:
            return Response(
                {"detail": "Uploaded document was not found in storage. Please upload it again."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        except Exception as exc:
            logger.exception("Failed to finalize Vibe Raising manual document upload")
            return Response(
                {"detail": f"Failed to finalize document upload: {exc}"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        return Response({"document": _serialize_manual_document(document)}, status=status.HTTP_200_OK)


class VibeRaisingManualDocumentDetailView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def delete(self, request, document_id):
        document, error_response = _get_accessible_manual_document_or_response(request, document_id)
        if error_response:
            return error_response

        storage_path = document.storage_path
        document.delete()
        try:
            delete_storage_object(storage_path)
        except Exception:
            logger.warning(
                "Failed to delete Vibe Raising manual document from storage",
                exc_info=True,
                extra={"document_id": str(document_id), "storage_path": storage_path},
            )
        return Response(status=status.HTTP_204_NO_CONTENT)


class VibeRaisingManualDocumentDownloadView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, document_id):
        document, error_response = _get_accessible_manual_document_or_response(request, document_id)
        if error_response:
            return error_response

        try:
            download_url = create_signed_read_url(
                document.storage_path,
                expires_in=VIBE_RAISING_SIGNED_READ_TTL,
            )
        except Exception as exc:
            logger.exception("Failed to create Vibe Raising manual document download URL")
            return Response(
                {"detail": f"Failed to create download URL: {exc}"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        return Response(
            {
                "downloadUrl": download_url,
                "expiresAt": (timezone.now() + VIBE_RAISING_SIGNED_READ_TTL).isoformat(),
                "document": _serialize_manual_document(document),
            },
            status=status.HTTP_200_OK,
        )


class VibeRaisingMonthlyUpdateView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        context, error_response = _get_founder_company_context_or_response(request.user)
        if error_response:
            return error_response

        domain = context["domain"]
        if not domain:
            return Response({"updates": []}, status=status.HTTP_200_OK)

        binding = get_default_binding_for_domain(user=request.user, domain=domain)
        organization = binding.organization if binding else Organization.objects.filter(domain=domain).first()
        if organization is None:
            return Response({"updates": []}, status=status.HTTP_200_OK)

        updates = [
            _serialize_monthly_update(draft)
            for draft in organization.monthly_update_drafts.order_by("-month", "-updated_at")
        ]
        return Response({"updates": updates}, status=status.HTTP_200_OK)

    def post(self, request):
        context, error_response = _get_founder_company_context_or_response(request.user)
        if error_response:
            return error_response

        if not context["domain"]:
            return Response(
                {"detail": "Add a company domain before publishing updates."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        serializer = VibeRaisingMonthlyUpdateUpsertSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        company = context["company"]
        organization, _startup_profile, _binding = _ensure_binding_for_company(
            user=request.user,
            company=company,
        )
        try:
            manual_documents = _resolve_manual_documents_for_request(
                user=request.user,
                organization=organization,
                company=company,
                document_ids=serializer.validated_data.get("manualDocumentIds") or [],
            )
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        structured_payload = {
            **serializer.validated_data,
            "manualDocuments": [
                _serialize_manual_document_for_memo(document)
                for document in manual_documents
            ],
        }
        month_bucket = date(
            serializer.validated_data["year"],
            serializer.validated_data["month_number"],
            1,
        )
        draft, created = MonthlyUpdateDraft.objects.update_or_create(
            organization=organization,
            month=month_bucket,
            defaults={
                "status": MonthlyUpdateDraftStatus.READY,
                "title": f"{company.name} {serializer.validated_data['month']} {serializer.validated_data['year']} Update",
                "model_name": "vibe-raising-manual",
                "structured_memo": _build_manual_structured_memo(structured_payload),
            },
        )

        return Response(
            {"update": _serialize_monthly_update(draft)},
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )


class VibeRaisingStartupUpdateBootstrapView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        context, error_response = _get_founder_company_context_or_response(request.user)
        if error_response:
            return error_response

        company = context["company"]
        domain = context["domain"]
        if not domain:
            return Response(
                {
                    "state": "needs_domain",
                    "company": _serialize_company_summary(company),
                    "error": "Add a company domain before connecting Gmail.",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        organization, _startup_profile, binding = _ensure_binding_for_company(
            user=request.user,
            company=company,
        )

        return Response(
            {
                "googleConnected": bool(getattr(request.user, "google_connection", None)),
                **gmail_scope_status_payload(getattr(request.user, "google_connection", None)),
                "company": _serialize_company_summary(company),
                "binding": _serialize_binding_summary(binding),
                "oauthUrl": _build_google_oauth_url(request),
            },
            status=status.HTTP_200_OK,
        )


class VibeRaisingStartupUpdateRunView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        context, error_response = _get_founder_company_context_or_response(request.user)
        if error_response:
            return error_response

        company = context["company"]
        domain = context["domain"]
        if not domain:
            return Response(
                _build_status_payload(user=request.user, company=company, domain=domain),
                status=status.HTTP_200_OK,
            )

        organization, _startup_profile, binding = _ensure_binding_for_company(
            user=request.user,
            company=company,
        )
        requested_manual_document_ids = _get_requested_manual_document_ids(request)
        manual_summary = _get_requested_manual_summary(request)
        try:
            manual_documents = _resolve_manual_documents_for_request(
                user=request.user,
                organization=organization,
                company=company,
                document_ids=requested_manual_document_ids,
            )
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        manual_document_ids = [str(document.id) for document in manual_documents]
        input_sources = _include_manual_source_if_needed(
            _get_requested_input_sources(request),
            manual_document_ids=manual_document_ids,
            manual_summary=manual_summary,
        )
        try:
            target_month = _requested_target_month_from_request(request)
        except ValueError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        google_connection = getattr(request.user, "google_connection", None)
        input_sources, google_connection, gmail_scope_warnings = coerce_startup_update_sources_for_gmail_scope(
            input_sources,
            google_connection,
        )
        source_warnings = merge_source_warnings(
            _sync_selected_financial_sources_for_draft(request.user, input_sources),
            gmail_scope_warnings,
        )
        if gmail_required_for_sources(input_sources) and (
            google_connection is None or not has_gmail_read_scope(google_connection)
        ):
            return Response(
                _build_status_payload(user=request.user, company=company, domain=domain),
                status=status.HTTP_200_OK,
            )

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
            payload = _build_status_payload(user=request.user, company=company, domain=domain)
            payload["run"] = _serialize_run_summary(conflicting_run)
            payload.update(_target_month_conflict_payload(
                requested_target_month=target_month,
                active_run=conflicting_run,
            ))
            return Response(payload, status=status.HTTP_200_OK)
        if existing_run is None:
            run = create_startup_update_run(
                organization=organization,
                binding=binding,
                window_months=DEFAULT_BACKFILL_MONTHS,
                input_sources=input_sources,
                source_warnings=source_warnings,
                target_month=target_month,
                manual_document_ids=manual_document_ids,
                manual_summary=manual_summary,
            )
            dispatch_result = _dispatch_run_to_valley(run)
            if not dispatch_result:
                payload = _build_status_payload(user=request.user, company=company, domain=domain)
                payload["run"] = _serialize_run_summary(run)
                payload.update(_valley_dispatch_failure_payload(run, dispatch_result))
                return Response(payload, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        else:
            run = existing_run
            set_startup_update_run_target_month(run, target_month)
            if input_sources:
                windows = build_startup_update_target_windows(target_month)
                refresh_startup_update_run_source_context(
                    run=run,
                    organization=organization,
                    input_sources=input_sources,
                    start_date=windows["financial_start_date"],
                    end_date=windows["financial_end_date"],
                    source_warnings=source_warnings,
                    manual_document_ids=manual_document_ids,
                    manual_summary=manual_summary,
                )
            if _should_dispatch_existing_run(run):
                logger.info(
                    "Re-dispatching queued startup update run to Valley",
                    extra={"run_id": run.run_id, "organization_id": organization.id},
                )
                dispatch_result = _dispatch_run_to_valley(run)
                if not dispatch_result:
                    payload = _build_status_payload(user=request.user, company=company, domain=domain)
                    payload["run"] = _serialize_run_summary(run)
                    payload.update(_valley_dispatch_failure_payload(run, dispatch_result))
                    return Response(payload, status=status.HTTP_503_SERVICE_UNAVAILABLE)

        payload = _build_status_payload(user=request.user, company=company, domain=domain)
        payload["run"] = _serialize_run_summary(run)
        return Response(
            payload,
            status=status.HTTP_200_OK if existing_run else status.HTTP_201_CREATED,
        )


class VibeRaisingStartupUpdateStatusView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        context, error_response = _get_founder_company_context_or_response(request.user)
        if error_response:
            return error_response

        return Response(
            _build_status_payload(
                user=request.user,
                company=context["company"],
                domain=context["domain"],
            ),
            status=status.HTTP_200_OK,
        )


class VibeRaisingEmailDraftStartView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        context, error_response = _get_founder_company_context_or_response(request.user)
        if error_response:
            return error_response

        company = context["company"]
        domain = context["domain"]
        if not domain:
            return Response(
                _build_email_draft_payload(
                    request=request,
                    user=request.user,
                    company=company,
                    domain=domain,
                ),
                status=status.HTTP_400_BAD_REQUEST,
            )

        organization, _startup_profile, binding = _ensure_binding_for_company(
            user=request.user,
            company=company,
        )
        requested_manual_document_ids = _get_requested_manual_document_ids(request)
        manual_summary = _get_requested_manual_summary(request)
        try:
            manual_documents = _resolve_manual_documents_for_request(
                user=request.user,
                organization=organization,
                company=company,
                document_ids=requested_manual_document_ids,
            )
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        manual_document_ids = [str(document.id) for document in manual_documents]
        input_sources = _include_manual_source_if_needed(
            _get_requested_input_sources(request),
            manual_document_ids=manual_document_ids,
            manual_summary=manual_summary,
        )
        try:
            target_month = _requested_target_month_from_request(request)
        except ValueError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        google_connection = getattr(request.user, "google_connection", None)
        input_sources, google_connection, gmail_scope_warnings = coerce_startup_update_sources_for_gmail_scope(
            input_sources,
            google_connection,
        )
        source_warnings = merge_source_warnings(
            _sync_selected_financial_sources_for_draft(request.user, input_sources),
            gmail_scope_warnings,
        )
        if gmail_required_for_sources(input_sources) and (
            google_connection is None or not has_gmail_read_scope(google_connection)
        ):
            return Response(
                _build_email_draft_payload(
                    request=request,
                    user=request.user,
                    company=company,
                    domain=domain,
                    target_month=target_month,
                ),
                status=status.HTTP_200_OK,
            )

        if google_connection is not None and binding.google_connection_id != google_connection.id:
            binding.google_connection = google_connection
            binding.save(update_fields=["google_connection", "updated_at"])

        raw_force_regenerate = request.data.get("force_regenerate") or request.data.get("forceRegenerate")
        force_regenerate = str(raw_force_regenerate or "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
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
            payload = _build_email_draft_payload(
                request=request,
                user=request.user,
                company=company,
                domain=domain,
                run_id=conflicting_run.run_id,
                target_month=target_month,
            )
            payload["reusedExistingRun"] = True
            payload.update(_target_month_conflict_payload(
                requested_target_month=target_month,
                active_run=conflicting_run,
            ))
            return Response(payload, status=status.HTTP_200_OK)

        if force_regenerate and existing_run is not None:
            # "Run again": supersede the in-flight run so a brand-new run
            # re-pulls the latest source data instead of resuming stale work.
            try:
                cancel_result = cancel_startup_update_run(
                    run_id=existing_run.run_id,
                    organization=organization,
                    binding_id=binding.id,
                    google_connection_id=google_connection.id if google_connection else None,
                    cancelled_by_user_id=request.user.id,
                )
            except (ContentFactoryRun.DoesNotExist, PermissionError):
                cancel_result = None
            if cancel_result and cancel_result.get("cancel_applied"):
                try:
                    cancel_valley_run(existing_run.run_id)
                except Exception:  # noqa: BLE001 - best-effort revoke of the superseded worker
                    logger.warning(
                        "Failed to revoke superseded valley run during forced regenerate",
                        extra={"run_id": existing_run.run_id, "organization_id": organization.id},
                    )
            existing_run = None

        reusable_drafts_cover_input_sources = _monthly_update_drafts_cover_input_sources(
            organization,
            input_sources,
            target_month=target_month,
        )

        created = False
        if existing_run is None and reusable_drafts_cover_input_sources and not force_regenerate:
            _refresh_reusable_xero_metrics_for_drafts(
                organization=organization,
                input_sources=input_sources,
                source_warnings=source_warnings,
                target_month=target_month,
            )
            latest_draft = organization.monthly_update_drafts.filter(month=target_month).order_by("-updated_at").first()
            logger.info(
                "Skipping Valley dispatch for Vibe Raising email draft start because reusable drafts already exist",
                extra={
                    "user_id": request.user.id,
                    "organization_id": organization.id,
                    "organization_domain": organization.domain,
                    "google_connection_id": google_connection.id if google_connection else None,
                    "force_regenerate": force_regenerate,
                    "draft_count": organization.monthly_update_drafts.count(),
                    "latest_draft_month": latest_draft.month.isoformat() if latest_draft else None,
                    "input_sources": input_sources,
                    "skip_reason": "reusable_drafts_available",
                },
            )
            payload = _build_email_draft_payload(
                request=request,
                user=request.user,
                company=company,
                domain=domain,
                target_month=target_month,
            )
            payload["reusedExistingRun"] = False
            return Response(payload, status=status.HTTP_200_OK)

        run = existing_run
        if run is None:
            run = create_startup_update_run(
                organization=organization,
                binding=binding,
                window_months=DEFAULT_BACKFILL_MONTHS,
                input_sources=input_sources,
                source_warnings=source_warnings,
                target_month=target_month,
                manual_document_ids=manual_document_ids,
                manual_summary=manual_summary,
                force_regenerate=force_regenerate,
            )
            created = True
            dispatch_result = _dispatch_run_to_valley(run)
            if not dispatch_result:
                payload = _build_email_draft_payload(
                    request=request,
                    user=request.user,
                    company=company,
                    domain=domain,
                    run_id=run.run_id,
                    target_month=target_month,
                )
                payload["reusedExistingRun"] = False
                payload.update(_valley_dispatch_failure_payload(run, dispatch_result))
                return Response(payload, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        elif _should_dispatch_existing_run(run):
            set_startup_update_run_target_month(run, target_month)
            if input_sources:
                windows = build_startup_update_target_windows(target_month)
                refresh_startup_update_run_source_context(
                    run=run,
                    organization=organization,
                    input_sources=input_sources,
                    start_date=windows["financial_start_date"],
                    end_date=windows["financial_end_date"],
                    source_warnings=source_warnings,
                    manual_document_ids=manual_document_ids,
                    manual_summary=manual_summary,
                )
            logger.info(
                "Re-dispatching queued email draft run to Valley",
                extra={"run_id": run.run_id, "organization_id": organization.id},
            )
            dispatch_result = _dispatch_run_to_valley(run)
            if not dispatch_result:
                payload = _build_email_draft_payload(
                    request=request,
                    user=request.user,
                    company=company,
                    domain=domain,
                    run_id=run.run_id,
                    target_month=target_month,
                )
                payload["reusedExistingRun"] = True
                payload.update(_valley_dispatch_failure_payload(run, dispatch_result))
                return Response(payload, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        else:
            set_startup_update_run_target_month(run, target_month)
            if input_sources:
                windows = build_startup_update_target_windows(target_month)
                refresh_startup_update_run_source_context(
                    run=run,
                    organization=organization,
                    input_sources=input_sources,
                    start_date=windows["financial_start_date"],
                    end_date=windows["financial_end_date"],
                    source_warnings=source_warnings,
                    manual_document_ids=manual_document_ids,
                    manual_summary=manual_summary,
                )
            logger.info(
                "Skipping Valley dispatch for Vibe Raising email draft start because an open run is already active",
                extra={
                    "user_id": request.user.id,
                    "organization_id": organization.id,
                    "organization_domain": organization.domain,
                    "google_connection_id": google_connection.id if google_connection else None,
                    "run_id": run.run_id,
                    "run_status": run.status,
                    "run_updated_at": run.updated_at.isoformat() if run.updated_at else None,
                    "skip_reason": "active_run_reused",
                },
            )

        payload = _build_email_draft_payload(
            request=request,
            user=request.user,
            company=company,
            domain=domain,
            run_id=run.run_id,
            target_month=target_month,
        )
        payload["reusedExistingRun"] = not created
        return Response(payload, status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)


class VibeRaisingEmailDraftStatusView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, run_id: Optional[str] = None):
        context, error_response = _get_founder_company_context_or_response(request.user)
        if error_response:
            return error_response

        requested_run_id = run_id or str(request.query_params.get("run_id") or "").strip() or None
        return Response(
            _build_email_draft_payload(
                request=request,
                user=request.user,
                company=context["company"],
                domain=context["domain"],
                run_id=requested_run_id,
            ),
            status=status.HTTP_200_OK,
        )


class VibeRaisingEmailDraftActiveRunView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        context, error_response = _get_founder_company_context_or_response(request.user)
        if error_response:
            return error_response

        google_connection = getattr(request.user, "google_connection", None)
        google_connection_id = getattr(google_connection, "id", None)
        domain = context["domain"]
        if not domain:
            return Response(None, status=status.HTTP_200_OK)

        binding = get_default_binding_for_domain(user=request.user, domain=domain)
        organization = binding.organization if binding else Organization.objects.filter(domain=domain).first()
        open_run = (
            get_open_startup_update_run(
                organization=organization,
                google_connection_id=google_connection_id,
            )
            if organization is not None
            else None
        )
        if open_run is None or open_run.status not in OPEN_RUN_STATUSES:
            return Response(None, status=status.HTTP_200_OK)

        return Response(
            _build_email_draft_payload(
                request=request,
                user=request.user,
                company=context["company"],
                domain=domain,
                run_id=open_run.run_id,
            ),
            status=status.HTTP_200_OK,
        )


class VibeRaisingEmailDraftCancelView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, run_id: str):
        context, error_response = _get_founder_company_context_or_response(request.user)
        if error_response:
            return error_response

        company = context["company"]
        domain = context["domain"]
        if not domain:
            return Response(
                {"error": "Add a company domain before cancelling Gmail draft generation."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        organization, _startup_profile, binding = _ensure_binding_for_company(
            user=request.user,
            company=company,
        )
        google_connection = getattr(request.user, "google_connection", None)
        google_connection_id = getattr(google_connection, "id", None) or binding.google_connection_id

        try:
            cancel_result = cancel_startup_update_run(
                run_id=run_id,
                organization=organization,
                binding_id=binding.id,
                google_connection_id=google_connection_id,
                cancelled_by_user_id=request.user.id,
            )
        except ContentFactoryRun.DoesNotExist:
            return Response({"error": "Run not found."}, status=status.HTTP_404_NOT_FOUND)
        except PermissionError:
            return Response({"error": "Run not found."}, status=status.HTTP_404_NOT_FOUND)

        run = cancel_result["run"]
        cleanup = cancel_result["cleanup"]
        revoke_payload = {
            "revoke_requested": False,
            "revoke_succeeded": False,
            "revoked_job_ids": [],
            "missing_job_ids": [],
        }
        if cancel_result["cancel_applied"]:
            revoke_payload = cancel_valley_run(run.run_id)

        return Response(
            {
                "run_id": run.run_id,
                "status": run.status,
                "terminal_state": run.status if run.status in {
                    ContentFactoryRunStatus.COMPLETED,
                    ContentFactoryRunStatus.CANCELLED,
                } else None,
                "cancel_applied": bool(cancel_result["cancel_applied"]),
                "cleanup": cleanup,
                "revoke_requested": bool(revoke_payload.get("revoke_requested")),
                "revoke_succeeded": bool(revoke_payload.get("revoke_succeeded")),
                "revoked_job_ids": list(revoke_payload.get("revoked_job_ids") or []),
                "missing_job_ids": list(revoke_payload.get("missing_job_ids") or []),
            },
            status=status.HTTP_200_OK,
        )


class VibeRaisingEmailDraftResultsView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, run_id: Optional[str] = None):
        context, error_response = _get_founder_company_context_or_response(request.user)
        if error_response:
            return error_response

        requested_run_id = run_id or str(request.query_params.get("run_id") or "").strip() or None
        payload = _build_email_draft_results_payload(
            request=request,
            user=request.user,
            company=context["company"],
            domain=context["domain"],
            run_id=requested_run_id,
        )
        if not payload.get("draft"):
            return Response(
                {"error": "Draft results are not available yet."},
                status=status.HTTP_404_NOT_FOUND,
            )
        return Response(payload, status=status.HTTP_200_OK)


class VibeRaisingEmailDraftLatestView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        context, error_response = _get_founder_company_context_or_response(request.user)
        if error_response:
            return error_response

        return Response(
            _build_email_draft_payload(
                request=request,
                user=request.user,
                company=context["company"],
                domain=context["domain"],
            ),
            status=status.HTTP_200_OK,
        )
