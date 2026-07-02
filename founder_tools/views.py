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
    CompanyDomainChangeBlocked,
    DomainOwnershipError,
    DuplicateCompanyDomainError,
    apply_company_domain_change,
    apply_shared_startup_details,
    assert_company_domain_available,
    domain_is_available_to,
    ensure_company_organization,
    get_or_create_founder_profile,
    set_active_company,
)
from vibe_raising.registration import (
    attempt_company_verification,
    set_unverified_company_abn,
)


def _connector_summaries(user):
    try:
        from integrations.services.external_connectors import active_organization_for_user

        connections = ExternalServiceConnection.objects.filter(user=user).order_by("provider", "-updated_at")
        organization = active_organization_for_user(user)
        if organization is not None:
            # Scope the connector summary to the active startup so two startups
            # under one login don't see each other's connections.
            connections = connections.filter(organization=organization)
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
        create_new = bool(data.pop("createNew", False))
        confirm_domain_change = bool(data.pop("confirmDomainChange", False))
        plain_fields = {
            key: data[key]
            for key in ("name", "domain", "location")
            if key in data
        }

        try:
            if company_id:
                company = get_object_or_404(VibeRaisingCompany, pk=company_id, profile=profile)
            elif create_new:
                company = VibeRaisingCompany(profile=profile)
            else:
                company = profile.companies.filter(name__iexact=data["name"]).first() or VibeRaisingCompany(
                    profile=profile
                )

            if "domain" in plain_fields:
                assert_company_domain_available(
                    profile, plain_fields["domain"], exclude_company_id=company.id
                )
                is_admin = bool(
                    getattr(request.user, "is_staff", False) or getattr(request.user, "is_superuser", False)
                )
                # First-claim-wins tenancy: a founder may only take a domain that
                # is unclaimed or already theirs (parity with the vibe-raising
                # company endpoint).
                if (
                    plain_fields["domain"]
                    and not is_admin
                    and not domain_is_available_to(request.user, plain_fields["domain"])
                ):
                    raise DomainOwnershipError()
                if plain_fields["domain"]:
                    # Rename the org in place when safe; otherwise a re-point
                    # strands the old org's data and needs confirmation.
                    apply_company_domain_change(
                        company, plain_fields["domain"], confirmed=confirm_domain_change
                    )

            for field, value in plain_fields.items():
                setattr(company, field, value)
            if "abn" in data:
                set_unverified_company_abn(company, data["abn"])
            if "registered" in data:
                company.registered = bool(data.get("registered"))
            company.save()

            # Best-effort verification — unlocks perks (e.g. the coworking discount) when
            # the ABN/ACN check out, but never blocks setup if they don't.
            attempt_company_verification(company, abn=company.abn, acn=data.get("acn"))
        except DuplicateCompanyDomainError as exc:
            return Response(
                {
                    "detail": str(exc),
                    "code": "duplicate_company_domain",
                    "field": "domain",
                    "companyId": str(exc.existing_company.id),
                },
                status=status.HTTP_409_CONFLICT,
            )
        except CompanyDomainChangeBlocked as exc:
            return Response(
                {
                    "detail": str(exc),
                    "code": "company_domain_change_moves_data",
                    "field": "domain",
                    "companyId": str(exc.company.id),
                    "currentDomain": exc.current_domain,
                    "newDomain": exc.new_domain,
                    "data": exc.data_summary,
                },
                status=status.HTTP_409_CONFLICT,
            )

        ensure_company_organization(company)
        apply_shared_startup_details(user=request.user, company=company, data=data)
        if profile.active_company_id is None:
            set_active_company(profile, company)
        company.refresh_from_db()

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
