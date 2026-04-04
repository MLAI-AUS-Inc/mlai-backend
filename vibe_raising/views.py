import calendar
import logging
import urllib.parse
from datetime import date, timedelta
from typing import Optional

from django.conf import settings
from django.db import transaction
from django.shortcuts import get_object_or_404
from django.urls import reverse
from django.utils import timezone
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from core.models import ContentFactoryRun, ContentFactoryRunStatus, ContentFactoryStepStatus, Organization
from integrations.models import MonthlyUpdateDraft, MonthlyUpdateDraftStatus
from integrations.services.startup_updates import (
    DEFAULT_BACKFILL_MONTHS,
    OPEN_RUN_STATUSES,
    RUN_STEP_ORDER,
    STARTUP_UPDATE_WORKFLOW,
    bind_user_to_startup,
    cancel_startup_update_run,
    create_startup_update_run,
    get_default_binding_for_domain,
    get_latest_startup_update_run,
    get_open_startup_update_run,
    resolve_or_create_profile,
    sync_startup_profile_from_company,
)
from integrations.services.valley_harness import cancel_valley_run, notify_valley_run_created
from integrations.utils import normalize_domain
from .models import VibeRaisingCompany, VibeRaisingProfile
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
EMAIL_DRAFT_DISPLAY_STAGES = {
    "profile_resolution": "Preparing company context",
    "gmail_backfill": "Scanning recent Gmail messages",
    "relevance_classification": "Finding investor-relevant updates",
    "thread_hydration": "Pulling full thread context",
    "event_extraction": "Extracting metrics and highlights",
    "timeline_merge": "Building timeline",
    "draft_generation": "Drafting monthly updates",
    "groundedness_review": "Final review",
}


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
    return profile.active_company or profile.companies.first()


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


def _serialize_run_summary(run):
    if not run:
        return None

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
        "createdAt": run.created_at.isoformat(),
        "updatedAt": run.updated_at.isoformat(),
    }


def _frontend_base_url():
    for setting_name in ("VIBE_RAISING_URL", "DEFAULT_FRONTEND_URL"):
        value = str(getattr(settings, setting_name, "") or "").strip()
        if value:
            return value.rstrip("/")

    return "http://localhost:5173"


def _build_google_oauth_url(request):
    next_url = f"{_frontend_base_url()}/vibe-raising/create-update?email_draft=1"
    connect_url = request.build_absolute_uri(reverse("google_connect"))
    return f"{connect_url}?{urllib.parse.urlencode({'next': next_url})}"


def _should_dispatch_existing_run(run) -> bool:
    return (
        bool(run)
        and run.status == ContentFactoryRunStatus.QUEUED
        and bool(run.updated_at)
        and run.updated_at <= timezone.now() - QUEUED_REDISPATCH_AFTER
    )


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


def _normalize_metric_value(value):
    if value is None:
        return None

    text = str(value).strip()
    return text or None


def _metric_key_from_label(label):
    normalized = str(label or "").strip().lower()
    if normalized in {"revenue", "monthly revenue"}:
        return "revenue"
    if normalized in {"active users", "users", "monthly active users"}:
        return "activeUsers"
    if normalized in {"mrr", "monthly recurring revenue"}:
        return "mrr"
    if normalized in {"burn rate", "burn"}:
        return "burnRate"
    if normalized == "runway":
        return "runway"
    return None


MANUAL_METRIC_LABELS = {
    "revenue": "Revenue",
    "activeUsers": "Active Users",
    "mrr": "MRR",
    "burnRate": "Burn Rate",
    "runway": "Runway",
}


def _extract_metrics(structured_memo):
    metrics = {}
    snapshot = (structured_memo or {}).get("kpi_snapshot") or []
    for item in snapshot:
        if not isinstance(item, dict):
            continue

        metric_key = str(item.get("metric_key") or "").strip() or _metric_key_from_label(
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


def _serialize_draft_for_form(draft):
    structured_memo = draft.structured_memo or {}
    month_value = draft.month
    return {
        "month": calendar.month_name[month_value.month],
        "year": month_value.year,
        "highlights": _join_text_items(structured_memo.get("highlights")),
        "challenges": _join_text_items(structured_memo.get("lowlights")),
        "asks": _join_text_items(structured_memo.get("asks")),
        "metrics": _extract_metrics(structured_memo),
    }


def _split_editor_text(value):
    return [
        item.strip()
        for item in str(value or "").replace("\r\n", "\n").split("\n")
        if item.strip()
    ]


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
    return {
        "highlights": _split_editor_text(payload.get("highlights")),
        "lowlights": _split_editor_text(payload.get("challenges")),
        "asks": _split_editor_text(payload.get("asks")),
        "kpi_snapshot": _build_manual_kpi_snapshot(payload.get("metrics") or {}),
    }


def _serialize_monthly_update(draft):
    structured_memo = draft.structured_memo or {}
    return {
        "id": draft.id,
        "isoMonth": draft.month.isoformat(),
        "month": f"{calendar.month_name[draft.month.month]} {draft.month.year}",
        "monthName": calendar.month_name[draft.month.month],
        "year": draft.month.year,
        "date": draft.updated_at.isoformat(),
        "status": draft.status,
        "metrics": _extract_metrics(structured_memo),
        "highlights": _join_text_items_with_newlines(structured_memo.get("highlights")),
        "challenges": _join_text_items_with_newlines(structured_memo.get("lowlights")),
        "asks": _join_text_items_with_newlines(structured_memo.get("asks")),
    }


def _serialize_draft_bundle(drafts):
    if not drafts:
        return None

    current = _serialize_draft_for_form(drafts[0])
    past_months = []
    for draft in drafts[1:3]:
        structured_memo = draft.structured_memo or {}
        month_value = draft.month
        past_months.append(
            {
                "month": f"{calendar.month_name[month_value.month]} {month_value.year}",
                "highlights": _join_text_items(structured_memo.get("highlights")),
                "challenges": _join_text_items(structured_memo.get("lowlights")),
                "asks": _join_text_items(structured_memo.get("asks")),
                "metrics": _extract_metrics(structured_memo),
            }
        )

    return {
        **current,
        "pastMonths": past_months,
    }


def _serialize_email_draft_month(draft):
    structured_memo = draft.structured_memo or {}
    month_value = draft.month
    return {
        "draftId": draft.id,
        "isoMonth": month_value.isoformat(),
        "month": calendar.month_name[month_value.month],
        "year": month_value.year,
        "metrics": _extract_metrics(structured_memo),
        "highlights": _join_text_items(structured_memo.get("highlights")),
        "challenges": _join_text_items(structured_memo.get("lowlights")),
        "asks": _join_text_items(structured_memo.get("asks")),
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
    google_connection_id = getattr(google_connection, "id", None)
    company_payload = _serialize_company_summary(company)

    if not domain:
        return {
            "state": "needs_domain",
            "googleConnected": google_connected,
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

    error = None
    if not google_connected:
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
        error = "Gmail processing failed. Please try again."
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
        "company": company_payload,
        "run": _serialize_run_summary(open_run or latest_run),
        "draft": draft_payload,
        "error": error,
    }


def _build_email_draft_payload(*, request, user, company, domain, run_id: Optional[str] = None):
    google_connection = getattr(user, "google_connection", None)
    google_connected = bool(google_connection)
    google_connection_id = getattr(google_connection, "id", None)
    company_payload = _serialize_company_summary(company)
    auth_url = _build_google_oauth_url(request)

    if not domain:
        return {
            "state": "needs_domain",
            "gmailConnected": google_connected,
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
        latest_run = selected_run or get_open_startup_update_run(
            organization=organization,
            google_connection_id=google_connection_id,
        ) or get_latest_startup_update_run(
            organization=organization,
            google_connection_id=google_connection_id,
        )
        drafts = _get_drafts_for_run(latest_run)
        if not drafts and latest_run is None and selected_run is None:
            drafts = _get_recent_drafts_for_organization(organization)

    draft_payload = _serialize_draft_bundle(drafts)
    email_draft_payload = _serialize_email_draft_bundle(drafts)
    run_payload = _serialize_run_summary(latest_run)
    progress_payload = _serialize_run_progress(latest_run)

    error = None
    if not google_connected:
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
        error = "Gmail processing failed. Please try again."
    elif email_draft_payload:
        state = "completed"
    else:
        state = "failed"
        error = "Draft generation has not started yet."

    return {
        "state": state,
        "gmailConnected": google_connected,
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

                if profile.active_company_id is None:
                    profile.active_company = company
                    profile.save(update_fields=["active_company", "updated_at"])

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
            profile.active_company = company
            profile.save(update_fields=["active_company", "updated_at"])

        return Response(status=status.HTTP_204_NO_CONTENT)


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
                "structured_memo": _build_manual_structured_memo(serializer.validated_data),
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
        google_connection = getattr(request.user, "google_connection", None)
        if google_connection is None:
            return Response(
                _build_status_payload(user=request.user, company=company, domain=domain),
                status=status.HTTP_200_OK,
            )

        existing_run = get_open_startup_update_run(
            organization=organization,
            google_connection_id=google_connection.id,
        )
        if existing_run is None:
            run = create_startup_update_run(
                organization=organization,
                binding=binding,
                window_months=DEFAULT_BACKFILL_MONTHS,
            )
            transaction.on_commit(lambda: notify_valley_run_created(run.run_id))
        else:
            run = existing_run
            if _should_dispatch_existing_run(run):
                logger.info(
                    "Re-dispatching queued startup update run to Valley",
                    extra={"run_id": run.run_id, "organization_id": organization.id},
                )
                transaction.on_commit(lambda: notify_valley_run_created(run.run_id))

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
        google_connection = getattr(request.user, "google_connection", None)
        if google_connection is None:
            return Response(
                _build_email_draft_payload(
                    request=request,
                    user=request.user,
                    company=company,
                    domain=domain,
                ),
                status=status.HTTP_200_OK,
            )

        if binding.google_connection_id != google_connection.id:
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
            google_connection_id=google_connection.id,
        )
        reusable_drafts_exist = organization.monthly_update_drafts.exists()

        created = False
        if existing_run is None and reusable_drafts_exist and not force_regenerate:
            latest_draft = organization.monthly_update_drafts.order_by("-month", "-updated_at").first()
            logger.info(
                "Skipping Valley dispatch for Vibe Raising email draft start because reusable drafts already exist",
                extra={
                    "user_id": request.user.id,
                    "organization_id": organization.id,
                    "organization_domain": organization.domain,
                    "google_connection_id": google_connection.id,
                    "force_regenerate": force_regenerate,
                    "draft_count": organization.monthly_update_drafts.count(),
                    "latest_draft_month": latest_draft.month.isoformat() if latest_draft else None,
                    "skip_reason": "reusable_drafts_available",
                },
            )
            payload = _build_email_draft_payload(
                request=request,
                user=request.user,
                company=company,
                domain=domain,
            )
            payload["reusedExistingRun"] = False
            return Response(payload, status=status.HTTP_200_OK)

        run = existing_run
        if run is None:
            run = create_startup_update_run(
                organization=organization,
                binding=binding,
                window_months=DEFAULT_BACKFILL_MONTHS,
            )
            created = True
            transaction.on_commit(lambda: notify_valley_run_created(run.run_id))
        elif _should_dispatch_existing_run(run):
            logger.info(
                "Re-dispatching queued email draft run to Valley",
                extra={"run_id": run.run_id, "organization_id": organization.id},
            )
            transaction.on_commit(lambda: notify_valley_run_created(run.run_id))
        else:
            logger.info(
                "Skipping Valley dispatch for Vibe Raising email draft start because an open run is already active",
                extra={
                    "user_id": request.user.id,
                    "organization_id": organization.id,
                    "organization_domain": organization.domain,
                    "google_connection_id": google_connection.id,
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
