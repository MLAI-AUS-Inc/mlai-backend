"""Service-only proof checks and replay-safe worker receipts."""

from rest_framework.response import Response
from rest_framework.views import APIView
from core.permissions import HasRooApiKey
from . import article_preview_recovery as recovery, article_preview_recovery_worker as worker
from .website_contract import WebsiteAuthorityError


class PreviewRecoveryServiceView(APIView):
    authentication_classes = []
    permission_classes = [HasRooApiKey]
    operation = "authorize"

    def post(self, request, run_id):
        try:
            if self.operation == "result":
                data = worker.callback(run_id, request.data)
            elif self.operation == "token":
                data = worker.read_token(run_id, request.data)
            else:
                with recovery.authority(run_id, request.data) as op:
                    data = {"authorized": True, **op.payload["wire"]}
            return Response(data, headers={"Cache-Control": "private, no-store"})
        except (recovery.Refused, WebsiteAuthorityError) as exc:
            return Response(
                {"authorized": False, "reason": getattr(exc, "reason", "access_changed")},
                status=getattr(exc, "status", 409),
                headers={"Cache-Control": "private, no-store"},
            )
