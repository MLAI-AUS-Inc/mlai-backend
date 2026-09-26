"""Scoped account administration for mobile and desktop member management."""

import re

from django.shortcuts import get_object_or_404
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from core.models import AccountBan
from .account_bans import ban_account, lift_account_ban
from .authentication import CommunityChatAccountAuthentication
from .models import CommunityChatDevice
from .permissions import account_chat_role
from .throttles import CommunityChatScopedThrottle


def _serialize(ban):
    return {
        "id": ban.pk,
        "name": ban.user.full_name,
        "email": ban.email,
        "reason": ban.reason,
        "banned": ban.revoked_at is None,
        "revocation_pending": ban.revocation_pending,
        "created_at": ban.created_at.isoformat(),
    }


class AccountBanView(APIView):
    """Create/list/lift retained bans; every action requires this device's admin role."""

    authentication_classes = (CommunityChatAccountAuthentication,)
    permission_classes = (IsAuthenticated,)
    throttle_classes = (CommunityChatScopedThrottle,)
    community_chat_throttle_scope = "community_chat_home"

    def _require_admin(self, request):
        if (
            account_chat_role(
                request.user,
                request.community_chat_public_key,
                request.community_chat_installation_id,
            )
            != "admin"
        ):
            raise PermissionDenied("Only MLAI administrators can manage account bans.")

    def get(self, request):
        self._require_admin(request)
        # A bounded cursor keeps the list usable without truncating older bans.
        try:
            after = max(0, int(request.query_params.get("after", 0)))
        except (TypeError, ValueError) as exc:
            raise ValidationError("Invalid ban cursor.") from exc
        bans = list(
            AccountBan.objects.select_related("user")
            .filter(revoked_at__isnull=True, pk__gt=after)
            .order_by("pk")[:101]
        )
        return Response(
            {
                "bans": [_serialize(b) for b in bans[:100]],
                "next": bans[99].pk if len(bans) > 100 else None,
            },
            headers={"Cache-Control": "no-store"},
        )

    def post(self, request, ban_id=None):
        self._require_admin(request)
        if not isinstance(request.data, dict):
            raise ValidationError("Use an account ban request.")
        if ban_id is not None:
            if request.data.get("enabled") is not False:
                raise ValidationError("Use enabled=false to lift a ban.")
            get_object_or_404(AccountBan, pk=ban_id)
            ban = lift_account_ban(actor=request.user, ban_id=ban_id)
        else:
            key = request.data.get("public_key")
            if not isinstance(key, str) or not re.fullmatch(r"[0-9a-f]{64}", key):
                raise ValidationError("Choose a verified MLAI member.")
            device = get_object_or_404(
                CommunityChatDevice.objects.select_related("user"),
                public_key=key,
                status="verified",
                revoked_at__isnull=True,
            )
            ban = ban_account(
                actor=request.user,
                user_id=device.user_id,
                reason=request.data.get("reason", ""),
            )
        return Response(
            _serialize(ban),
            status=202 if ban.revocation_pending else 200,
            headers={"Cache-Control": "no-store"},
        )
