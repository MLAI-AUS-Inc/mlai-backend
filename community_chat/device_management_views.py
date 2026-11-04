"""Private device management through the existing Chat account credential."""

from rest_framework import serializers
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .adapter import MembershipAdapterConflict, MembershipAdapterUnavailable
from .authentication import CommunityChatAccountAuthentication
from .device_management import account_devices, remove_account_device, update_account_device
from .privacy_views import StrictSerializer
from .throttles import CommunityChatScopedThrottle


class DeviceNameSerializer(StrictSerializer):
    name = serializers.CharField(max_length=120, allow_blank=False)


class DeviceRemovalSerializer(StrictSerializer):
    expected_public_key = serializers.RegexField(r"^[0-9a-f]{64}$")
    confirmed = serializers.BooleanField()

    def validate_confirmed(self, value):
        if value is not True:
            raise serializers.ValidationError("Confirm device removal.")
        return value


class AccountDevicesView(APIView):
    """Expose private registration activity only to its authenticated owner."""

    authentication_classes = (CommunityChatAccountAuthentication,)
    permission_classes = (IsAuthenticated,)
    throttle_classes = (CommunityChatScopedThrottle,)
    community_chat_throttle_scope = "community_chat_session"

    def get(self, request):
        response = Response({"devices": account_devices(
            request.user, request.community_chat_account_session,
        )})
        response["Cache-Control"] = "no-store"
        return response


class AccountDeviceView(AccountDevicesView):
    """Rename or explicitly retire one owned registration by stable row ID."""

    community_chat_throttle_scope = "community_chat_revoke"
    http_method_names = ("patch", "delete", "options")

    def patch(self, request, device_id):
        serializer = DeviceNameSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        update_account_device(authenticated_session=request.community_chat_account_session,
            device_id=device_id, **serializer.validated_data)
        return Response({"status": "updated"})

    def delete(self, request, device_id):
        serializer = DeviceRemovalSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            relay_status = remove_account_device(
                authenticated_session=request.community_chat_account_session,
                device_id=device_id,
                expected_public_key=serializer.validated_data["expected_public_key"],
            )
        except MembershipAdapterConflict as exc:
            return Response({"error": exc.code}, status=409)
        except MembershipAdapterUnavailable:
            return Response({"error": "membership_service_unavailable"}, status=503)
        return Response({"status": "revoked", "relay_status": relay_status})
