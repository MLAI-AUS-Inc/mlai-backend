from django.db import transaction
from django.shortcuts import get_object_or_404
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

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
