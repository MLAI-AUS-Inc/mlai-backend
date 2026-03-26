import calendar
import logging
import urllib.parse
from datetime import timedelta
from typing import Optional

from django.conf import settings
from django.db import transaction
from django.shortcuts import get_object_or_404
from django.urls import reverse
from django.utils import timezone
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from core.models import ContentFactoryRun, ContentFactoryRunStatus, Organization
from integrations.services.startup_updates import (
    DEFAULT_BACKFILL_MONTHS,
    STARTUP_UPDATE_WORKFLOW,
    bind_user_to_startup,
    create_startup_update_run,
    get_default_binding_for_domain,
    get_open_startup_update_run,
    resolve_or_create_profile,
    sync_startup_profile_from_company,
)
from integrations.services.valley_harness import notify_valley_run_created
from integrations.utils import normalize_domain
from .models import VibeRaisingCompany, VibeRaisingProfile
from .serializers import (
    VibeRaisingActiveCompanySerializer,
    VibeRaisingCompanySerializer,
    VibeRaisingCompanyUpsertSerializer,
    VibeRaisingProfileSerializer,
    VibeRaisingProfileUpsertSerializer,
)


logger = logging.getLogger(__name__)

QUEUED_REDISPATCH_AFTER = timedelta(seconds=30)


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
        "organizationId": binding.organization_id,
        "organizationDomain": binding.organization.domain,
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


def _build_status_payload(*, user, company, domain):
    google_connected = bool(getattr(user, "google_connection", None))
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
    open_run = get_open_startup_update_run(organization=organization) if organization else None
    latest_run = (
        ContentFactoryRun.objects.filter(
            workflow=STARTUP_UPDATE_WORKFLOW,
            domain=organization.domain,
        )
        .order_by("-updated_at")
        .first()
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
    google_connected = bool(getattr(user, "google_connection", None))
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
        latest_run = selected_run or get_open_startup_update_run(organization=organization) or run_queryset.first()
        drafts = list(organization.monthly_update_drafts.order_by("-month", "-updated_at")[:3])

    draft_payload = _serialize_draft_bundle(drafts)
    email_draft_payload = _serialize_email_draft_bundle(drafts)

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
    elif email_draft_payload:
        state = "completed"
    elif latest_run and latest_run.status in {
        ContentFactoryRunStatus.FAILED,
        ContentFactoryRunStatus.DENIED,
    }:
        state = "failed"
        error = "Gmail processing failed. Please try again."
    elif latest_run and latest_run.status == ContentFactoryRunStatus.COMPLETED:
        state = "failed"
        error = "Draft generation completed without producing a draft."
    else:
        state = "failed"
        error = "Draft generation has not started yet."

    run_payload = _serialize_run_summary(latest_run)
    return {
        "state": state,
        "gmailConnected": google_connected,
        "company": company_payload,
        "authUrl": auth_url,
        "run": run_payload,
        "draft": draft_payload,
        "runId": run_payload["runId"] if run_payload else None,
        "status": run_payload["status"] if run_payload else None,
        "currentStep": run_payload["currentStep"] if run_payload else None,
        "stepStates": (run_payload or {}).get("stepStates", {}),
        "currentMonth": (email_draft_payload or {}).get("currentMonth"),
        "pastMonths": (email_draft_payload or {}).get("pastMonths", []),
        "error": error,
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

        existing_run = get_open_startup_update_run(organization=organization)
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
        existing_run = get_open_startup_update_run(organization=organization)
        reusable_drafts_exist = organization.monthly_update_drafts.exists()

        created = False
        if existing_run is None and reusable_drafts_exist and not force_regenerate:
            payload = _build_email_draft_payload(
                request=request,
                user=request.user,
                company=company,
                domain=domain,
            )
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

        payload = _build_email_draft_payload(
            request=request,
            user=request.user,
            company=company,
            domain=domain,
            run_id=run.run_id,
        )
        return Response(payload, status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)


class VibeRaisingEmailDraftStatusView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        context, error_response = _get_founder_company_context_or_response(request.user)
        if error_response:
            return error_response

        run_id = str(request.query_params.get("run_id") or "").strip() or None
        return Response(
            _build_email_draft_payload(
                request=request,
                user=request.user,
                company=context["company"],
                domain=context["domain"],
                run_id=run_id,
            ),
            status=status.HTTP_200_OK,
        )


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
