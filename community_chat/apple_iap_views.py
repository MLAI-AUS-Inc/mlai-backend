"""Account-bound StoreKit delivery and signed Apple notification endpoints."""

from django.conf import settings
from rest_framework import serializers
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from roo.apple_purchases import deliver_purchase, process_notification, sandbox_account
from roo.apple_verification import (
    AppleVerificationUnavailable, InvalidApplePurchase, MAX_SIGNED_PAYLOAD_BYTES,
    PRODUCT_MICROROO, verify_notification, verify_purchase,
)
from roo.services import PointsService
from .authentication import CommunityChatAccountAuthentication
from .privacy import locked_privacy_session
from .privacy_views import StrictSerializer
from .throttles import CommunityChatScopedThrottle


class AppleTransactionSerializer(StrictSerializer):
    signed_transaction = serializers.CharField(max_length=MAX_SIGNED_PAYLOAD_BYTES, trim_whitespace=False)


class AppleIapView(APIView):
    """Return the account token and digital-only balance; Apple supplies prices."""

    authentication_classes = (CommunityChatAccountAuthentication,)
    permission_classes = (IsAuthenticated,)
    throttle_classes = (CommunityChatScopedThrottle,)
    community_chat_throttle_scope = "community_chat_session"

    def get(self, request):
        account = PointsService.get_or_create_account(request.user)
        return Response({
            "available": bool(getattr(settings, "APPLE_IAP_ENABLED", False)),
            "app_account_token": str(request.user.community_chat_profile_id),
            "digital_balance_microroo": account.digital_balance_microroo,
            "digital_refund_debt_microroo": account.digital_refund_debt_microroo,
            "products": [{"id": key, "microroo": amount} for key, amount in PRODUCT_MICROROO.items()],
        })


class AppleIapTransactionView(AppleIapView):
    """Finish a purchase only after its verified credit/refund is durable."""

    def post(self, request):
        serializer = AppleTransactionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            purchase = verify_purchase(
                serializer.validated_data["signed_transaction"], sandbox=sandbox_account(request.user),
            )
            with locked_privacy_session(request.community_chat_account_session) as (user, _):
                from .onboarding import require_community_access
                require_community_access(user)
                row = deliver_purchase(user=user, purchase=purchase)
                account = PointsService.get_or_create_account(user)
                result = {"acknowledged": True, "transaction_id": row.transaction_id,
                          "product_id": row.product_id, "status": row.status,
                          "digital_balance_microroo": account.digital_balance_microroo,
                          "digital_refund_debt_microroo": account.digital_refund_debt_microroo}
            return Response(result)
        except AppleVerificationUnavailable:
            return Response({"code": "apple_verification_unavailable"}, status=503, headers={"Retry-After": "2"})
        except InvalidApplePurchase as error:
            return Response({"code": str(error)}, status=400)


class AppleIapNotificationView(APIView):
    """Apple authenticates both envelope and transaction using signed JWS."""

    authentication_classes = ()
    permission_classes = (AllowAny,)

    def post(self, request, environment):
        payload = request.data.get("signedPayload") if isinstance(request.data, dict) else None
        try:
            notification = verify_notification(payload, sandbox=environment == "sandbox")
            process_notification(notification)
            return Response({"received": True})
        except AppleVerificationUnavailable:
            return Response({"code": "apple_verification_unavailable"}, status=503)
        except InvalidApplePurchase as error:
            return Response({"code": str(error)}, status=400)
