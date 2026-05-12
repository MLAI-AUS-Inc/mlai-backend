from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from core.permissions import HasRooApiKey
from organizations.models import Organization
from startup_updates.data_deletion import (
    delete_startup_data_for_organization,
    serialize_startup_data_status,
)


class StartupDataStatusView(APIView):
    authentication_classes = []
    permission_classes = [HasRooApiKey]

    def get(self, request, organization_id: int):
        organization = Organization.objects.filter(id=organization_id).first()
        if organization is None:
            return Response({"detail": "Startup not found."}, status=status.HTTP_404_NOT_FOUND)
        return Response(serialize_startup_data_status(organization), status=status.HTTP_200_OK)


class StartupDataDeletionView(APIView):
    authentication_classes = []
    permission_classes = [HasRooApiKey]

    def delete(self, request, organization_id: int):
        organization = Organization.objects.filter(id=organization_id).first()
        if organization is None:
            return Response({"detail": "Startup not found."}, status=status.HTTP_404_NOT_FOUND)
        requested_by_user_id = request.data.get("requested_by_user_id")
        try:
            requested_by_user_id = int(requested_by_user_id) if requested_by_user_id not in (None, "") else None
        except (TypeError, ValueError):
            return Response({"detail": "requested_by_user_id must be an integer."}, status=status.HTTP_400_BAD_REQUEST)
        payload = delete_startup_data_for_organization(
            organization,
            requested_by_user_id=requested_by_user_id,
            reason=str(request.data.get("reason") or "user_request").strip() or "user_request",
            request_id=str(request.data.get("request_id") or "").strip() or None,
        )
        return Response(payload, status=status.HTTP_200_OK)
