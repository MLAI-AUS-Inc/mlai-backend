import calendar
import urllib.parse

from django.conf import settings
from django.db import transaction
from django.shortcuts import get_object_or_404
from django.urls import reverse
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

    return {
        "runId": run.run_id,
        "workflow": run.workflow,
        "domain": run.domain,
        "status": run.status,
        "currentStep": run.current_step,
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
    next_url = (
        f"{_frontend_base_url()}/vibe-raising/create-update"
        "?gmail_connected=1&draft_from_email=1"
    )
    connect_url = request.build_absolute_uri(reverse("google_connect"))
    return f"{connect_url}?{urllib.parse.urlencode({'next': next_url})}"


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

        metric_key = _metric_key_from_label(
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


def _sync_startup_profile(*, startup_profile, organization, company, user):
    org_name = str(company.name or "").strip()
    if org_name and organization.name != org_name:
        organization.name = org_name
        organization.save(update_fields=["name"])

    update_fields = []

    def merge_strings(existing, *values):
        merged = []
        seen = set()
        for raw in [*(existing or []), *values]:
            value = str(raw or "").strip()
            if not value:
                continue
            key = value.lower()
            if key in seen:
                continue
            seen.add(key)
            merged.append(value)
        return merged

    company_aliases = merge_strings(startup_profile.company_aliases, company.name)
    if company_aliases != list(startup_profile.company_aliases or []):
        startup_profile.company_aliases = company_aliases
        update_fields.append("company_aliases")

    domain_aliases = merge_strings(startup_profile.domain_aliases, organization.domain)
    if domain_aliases != list(startup_profile.domain_aliases or []):
        startup_profile.domain_aliases = domain_aliases
        update_fields.append("domain_aliases")

    founder_name = str(getattr(user, "full_name", "") or "").strip()
    founder_names = merge_strings(startup_profile.founder_names, founder_name)
    if founder_names != list(startup_profile.founder_names or []):
        startup_profile.founder_names = founder_names
        update_fields.append("founder_names")

    if update_fields:
        update_fields.append("updated_at")
        startup_profile.save(update_fields=update_fields)


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
