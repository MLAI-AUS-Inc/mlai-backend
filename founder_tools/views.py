from django.db import transaction
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from integrations.models import ExternalServiceConnection

from .models import VibeRaisingCompany, VibeRaisingProfile
from .serializers import (
    FounderActiveCompanySerializer,
    FounderCompanySerializer,
    FounderCompanyUpsertSerializer,
    FounderProfileSerializer,
    FounderProfileUpsertSerializer,
    serialize_founder_bootstrap,
)
from .services import (
    apply_shared_startup_details,
    ensure_company_organization,
    get_or_create_founder_profile,
    set_active_company,
)


def _connector_summaries(user):
    try:
        connections = ExternalServiceConnection.objects.filter(user=user).order_by("provider", "-updated_at")
    except Exception:
        return []

    summaries = []
    seen = set()
    for connection in connections:
        if connection.provider in seen:
            continue
        seen.add(connection.provider)
        summaries.append(
            {
                "provider": connection.provider,
                "status": connection.status,
                "accountLabel": connection.account_label,
                "updatedAt": connection.updated_at.isoformat() if connection.updated_at else None,
            }
        )
    return summaries


class FounderToolsBootstrapView(APIView):
    def get(self, request):
        profile = get_or_create_founder_profile(request.user)
        payload = serialize_founder_bootstrap(request.user, profile)
        payload["connectors"] = _connector_summaries(request.user)
        return Response(payload, status=status.HTTP_200_OK)


class FounderToolsProfileView(APIView):
    def get(self, request):
        profile = get_or_create_founder_profile(request.user)
        return Response(FounderProfileSerializer(profile).data, status=status.HTTP_200_OK)

    def put(self, request):
        return self._upsert(request)

    def post(self, request):
        return self._upsert(request)

    def _upsert(self, request):
        serializer = FounderProfileUpsertSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        profile = get_or_create_founder_profile(request.user)
        profile.role = VibeRaisingProfile.ROLE_FOUNDER
        profile.organization_name = None
        profile.save(update_fields=["role", "organization_name", "updated_at"])
        return Response(FounderProfileSerializer(profile).data, status=status.HTTP_200_OK)


class FounderToolsCompanyView(APIView):
    def get(self, request):
        profile = get_or_create_founder_profile(request.user)
        companies = profile.companies.select_related("organization").all()
        return Response(FounderCompanySerializer(companies, many=True).data, status=status.HTTP_200_OK)

    @transaction.atomic
    def post(self, request):
        profile = get_or_create_founder_profile(request.user)
        if profile.role != VibeRaisingProfile.ROLE_FOUNDER:
            profile.role = VibeRaisingProfile.ROLE_FOUNDER
            profile.organization_name = None
            profile.save(update_fields=["role", "organization_name", "updated_at"])

        serializer = FounderCompanyUpsertSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        company_id = data.pop("companyId", None)
        company_fields = {
            key: data[key]
            for key in ("name", "domain", "abn", "location", "registered")
            if key in data
        }

        if company_id:
            company = get_object_or_404(VibeRaisingCompany, pk=company_id, profile=profile)
            for field, value in company_fields.items():
                setattr(company, field, value)
            company.save()
        else:
            company = VibeRaisingCompany.objects.create(profile=profile, **company_fields)

        ensure_company_organization(company)
        apply_shared_startup_details(user=request.user, company=company, data=data)
        if profile.active_company_id is None:
            set_active_company(profile, company)

        return Response(FounderCompanySerializer(company).data, status=status.HTTP_200_OK)


class FounderToolsActiveCompanyView(APIView):
    @transaction.atomic
    def post(self, request):
        profile = get_or_create_founder_profile(request.user)
        serializer = FounderActiveCompanySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        company = get_object_or_404(
            VibeRaisingCompany,
            pk=serializer.validated_data["companyId"],
            profile=profile,
        )
        set_active_company(profile, company)
        return Response(FounderProfileSerializer(profile).data, status=status.HTTP_200_OK)
