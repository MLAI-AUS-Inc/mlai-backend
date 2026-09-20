"""Native/browser account privacy API; identities come only from Chat sessions."""

from rest_framework import serializers
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .authentication import CommunityChatAccountAuthentication
from .models import AccountDeletionRequest, AiConsentRecord
from .privacy import ai_disclosure, deletion_policy, has_ai_consent, request_account_deletion, set_ai_consent
from .throttles import CommunityChatScopedThrottle


class StrictSerializer(serializers.Serializer):
    """Reject fields that could be mistaken for account selection or authority."""

    def to_internal_value(self, data):
        if not isinstance(data, dict) or set(data) - set(self.fields):
            raise serializers.ValidationError({"non_field_errors": ["Unexpected privacy request fields."]})
        return super().to_internal_value(data)


class ConsentSerializer(StrictSerializer):
    granted = serializers.BooleanField()
    version = serializers.CharField(max_length=80, required=False, allow_blank=True, default="")
    provider_digest = serializers.CharField(max_length=64, required=False, allow_blank=True, default="")


class DeletionSerializer(StrictSerializer):
    scope = serializers.ChoiceField(choices=AccountDeletionRequest.Scope.choices)
    policy_version = serializers.CharField(max_length=80)
    confirmed = serializers.BooleanField()

    def validate_confirmed(self, value):
        if value is not True:
            raise serializers.ValidationError("Confirm the deletion scope before continuing.")
        return value


def deletion_receipt(record):
    """Return a minimal owner-only receipt, never operator notes or credentials."""
    return {"id": str(record.pk), "scope": record.scope, "status": record.status,
            "requested_at": record.requested_at, "completed_at": record.completed_at}


class PrivacyView(APIView):
    authentication_classes = (CommunityChatAccountAuthentication,)
    permission_classes = (IsAuthenticated,)
    throttle_classes = (CommunityChatScopedThrottle,)
    community_chat_throttle_scope = "community_chat_session"


class AiConsentView(PrivacyView):
    """Read the named recipients and explicitly grant or withdraw AI sharing."""

    def get(self, request):
        can_withdraw = AiConsentRecord.objects.filter(
            user=request.user, purpose="roo_chat", granted_at__isnull=False, withdrawn_at__isnull=True,
        ).exists()
        return Response({**ai_disclosure(), "granted": bool(has_ai_consent(request.user.pk)),
                         "can_withdraw": can_withdraw})

    def put(self, request):
        serializer = ConsentSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        set_ai_consent(authenticated_session=request.community_chat_account_session,
                       **serializer.validated_data)
        return self.get(request)


class AccountDeletionView(PrivacyView):
    """Initiate deletion inside the app and return a real, durable receipt."""

    def get(self, request):
        records = AccountDeletionRequest.objects.filter(user=request.user).order_by("-requested_at")[:20]
        return Response({**deletion_policy(), "requests": [deletion_receipt(row) for row in records]})

    def post(self, request):
        serializer = DeletionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        values = dict(serializer.validated_data)
        values.pop("confirmed")
        record, created = request_account_deletion(
            authenticated_session=request.community_chat_account_session, **values,
        )
        return Response({"request": deletion_receipt(record), **deletion_policy()}, status=201 if created else 200)
