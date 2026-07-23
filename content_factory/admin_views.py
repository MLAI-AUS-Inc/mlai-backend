"""Admin-only Vibe Marketing usage endpoint."""

from rest_framework import permissions
from rest_framework.response import Response
from rest_framework.views import APIView

from roo.permissions import is_points_admin_user

from .admin_metrics import build_vibe_marketing_admin_usage_payload


class IsVibeMarketingAdmin(permissions.BasePermission):
    message = "Vibe Marketing admin access is required."

    def has_permission(self, request, view):
        user = getattr(request, "user", None)
        return bool(user and user.is_authenticated and is_points_admin_user(user))


class VibeMarketingAdminUsageView(APIView):
    permission_classes = [IsVibeMarketingAdmin]

    def get(self, request):
        return Response(
            build_vibe_marketing_admin_usage_payload(request.query_params.get("range"))
        )
