"""Chat role discovery for account clients and the trusted relay."""

import hmac
import re

from django.conf import settings
from django.contrib.admin.models import CHANGE, LogEntry
from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from rest_framework.permissions import BasePermission, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .authentication import CommunityChatAccountAuthentication
from .models import CommunityChatDevice, DeviceBindingStatus, Moderator
from .permissions import (
    account_chat_role,
    chat_role,
    device_chat_role,
    role_capabilities,
)
from .throttles import CommunityChatScopedThrottle


class ChatPermissionsView(APIView):
    """Return capabilities of the exact device in this account session."""

    authentication_classes = (CommunityChatAccountAuthentication,)
    permission_classes = (IsAuthenticated,)
    throttle_classes = (CommunityChatScopedThrottle,)
    community_chat_throttle_scope = "community_chat_home"

    def get(self, request):
        public_key = request.community_chat_public_key
        response = Response(
            {
                **role_capabilities(
                    account_chat_role(
                        request.user,
                        public_key,
                        request.community_chat_installation_id,
                    )
                ),
                "public_key": public_key,
                "relay_url": settings.COMMUNITY_CHAT_RELAY_URL,
            }
        )
        response["Cache-Control"] = "no-store"
        return response


class IsChatRelay(BasePermission):
    """A dedicated read-only service credential, never an account token."""

    def has_permission(self, request, view):
        expected = getattr(settings, "COMMUNITY_CHAT_ROLE_SERVICE_TOKEN", "")
        actual = request.headers.get("Authorization", "")
        return len(expected) >= 32 and hmac.compare_digest(
            actual.encode(), f"Bearer {expected}".encode()
        )


class ChatModeratorView(APIView):
    """Let Chat admins appoint/revoke Moderators without changing any admin class."""

    authentication_classes = (CommunityChatAccountAuthentication,)
    permission_classes = (IsAuthenticated,)
    throttle_classes = (CommunityChatScopedThrottle,)
    community_chat_throttle_scope = "community_chat_home"

    @transaction.atomic
    def post(self, request, public_key):
        if (
            account_chat_role(
                request.user,
                request.community_chat_public_key,
                request.community_chat_installation_id,
            )
            != "admin"
        ):
            return Response({"error": "chat_admin_required"}, status=403)
        enabled = (
            request.data.get("enabled") if isinstance(request.data, dict) else None
        )
        if type(enabled) is not bool or not re.fullmatch(r"[0-9a-f]{64}", public_key):
            return Response({"error": "invalid_moderator_request"}, status=400)
        device = (
            CommunityChatDevice.objects.select_related("user")
            .filter(
                public_key=public_key,
                status=DeviceBindingStatus.VERIFIED,
                revoked_at__isnull=True,
                user__is_active=True,
            )
            .first()
        )
        if device is None:
            return Response({"error": "verified_member_not_found"}, status=404)
        if device.user_id == request.user.pk or chat_role(device.user) == "admin":
            return Response({"error": "protected_account"}, status=403)
        appointment, _ = Moderator.objects.update_or_create(
            user=device.user,
            defaults={"is_active": enabled},
        )
        LogEntry.objects.log_action(
            user_id=request.user.pk,
            content_type_id=ContentType.objects.get_for_model(Moderator).pk,
            object_id=appointment.pk,
            object_repr=f"Moderator appointment {appointment.pk}",
            action_flag=CHANGE,
            change_message="Moderator enabled" if enabled else "Moderator disabled",
        )
        return Response({"public_key": public_key, "role": chat_role(device.user)})


class ChatMemberRolesView(APIView):
    """Bounded role-only enrichment for the administrator's relay member list."""

    authentication_classes = (CommunityChatAccountAuthentication,)
    permission_classes = (IsAuthenticated,)
    throttle_classes = (CommunityChatScopedThrottle,)
    community_chat_throttle_scope = "community_chat_home"

    def post(self, request):
        from roo.models import PointsAdmin

        if (
            account_chat_role(
                request.user,
                request.community_chat_public_key,
                request.community_chat_installation_id,
            )
            != "admin"
        ):
            return Response({"error": "chat_admin_required"}, status=403)
        keys = (
            request.data.get("public_keys") if isinstance(request.data, dict) else None
        )
        if (
            not isinstance(keys, list)
            or len(keys) > 200
            or any(
                not isinstance(key, str) or not re.fullmatch(r"[0-9a-f]{64}", key)
                for key in keys
            )
        ):
            return Response({"error": "invalid_public_keys"}, status=400)
        devices = list(
            CommunityChatDevice.objects.select_related("user").filter(
                public_key__in=keys,
                status=DeviceBindingStatus.VERIFIED,
                revoked_at__isnull=True,
                user__is_active=True,
            )
        )
        users = [device.user_id for device in devices]
        admins = set(
            PointsAdmin.objects.filter(
                user_id__in=users, is_active=True, role__in=("admin", "committee")
            ).values_list("user_id", flat=True)
        )
        moderators = set(
            Moderator.objects.filter(user_id__in=users, is_active=True).values_list(
                "user_id", flat=True
            )
        )
        roles = dict.fromkeys(keys, "member")
        for device in devices:
            roles[device.public_key] = (
                "admin"
                if device.user.is_superuser or device.user_id in admins
                else ("moderator" if device.user_id in moderators else "member")
            )
        response = Response({"roles": roles})
        response["Cache-Control"] = "no-store"
        return response


class RelayChatRoleView(APIView):
    """Resolve a verified key's role without exposing account or profile data."""

    authentication_classes = ()
    permission_classes = (IsChatRelay,)

    def get(self, request, public_key):
        if not re.fullmatch(r"[0-9a-f]{64}", public_key):
            return Response({"error": "invalid_public_key"}, status=400)
        response = Response(
            {
                "public_key": public_key,
                "role": device_chat_role(public_key),
                "relay_url": settings.COMMUNITY_CHAT_RELAY_URL,
            }
        )
        response["Cache-Control"] = "no-store"
        return response
