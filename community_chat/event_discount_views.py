"""Account-authenticated read facade for the member's event coupon receipts."""

from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from core.authentication import CustomJWTAuthentication
from roo.event_discounts import list_redemptions
from .authentication import CommunityChatAccountAuthentication, CommunityChatBootstrapAuthentication
from .throttles import CommunityChatScopedThrottle


class MyEventDiscountsView(APIView):
    """Return only the session owner's receipts, accepting no target identity."""

    authentication_classes = (CommunityChatAccountAuthentication, CommunityChatBootstrapAuthentication, CustomJWTAuthentication)
    permission_classes = [IsAuthenticated]
    throttle_classes = [CommunityChatScopedThrottle]
    community_chat_throttle_scope = "community_chat_home"

    def get(self, request):
        return Response(list_redemptions(request.user), headers={"Cache-Control": "private, no-store"})
