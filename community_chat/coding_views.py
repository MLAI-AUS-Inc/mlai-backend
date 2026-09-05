"""Device-bound MLAI Coding entitlement and turn lifecycle endpoints."""

from django.conf import settings
from django.db import transaction
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from roo.coding import (
    CodingError,
    create_turn,
    current_pricing,
    finalize_turn,
    issue_turn_ticket,
    microroo_string,
    pricing_payload,
    reconcile_coding_reservations,
    roo_decimal_string,
    ticket_jwks,
    turn_remaining_microroo,
    user_has_pilot_access,
)
from roo.models import CodingModelCall, CodingTurn
from roo.services import PointsService

from .authentication import CommunityChatAccountAuthentication


def _error_response(exc: CodingError) -> Response:
    return Response(
        {"code": exc.code, "message": exc.message, **exc.extra},
        status=exc.http_status,
    )


def _turn_payload(turn: CodingTurn) -> dict:
    return {
        "id": str(turn.id),
        "status": turn.status,
        "local_session_id": str(turn.local_session_id),
        "reserved_microroo": microroo_string(turn.reserved_microroo),
        "settled_microroo": microroo_string(turn.settled_microroo),
        "remaining_microroo": microroo_string(turn_remaining_microroo(turn)),
        "expires_at": turn.expires_at,
    }


def _ticket_payload(turn: CodingTurn) -> dict:
    issued = issue_turn_ticket(turn)
    return {
        "turn_id": str(turn.id),
        "status": turn.status,
        "model": turn.model,
        "reserved_microroo": microroo_string(turn.reserved_microroo),
        "remaining_microroo": microroo_string(turn_remaining_microroo(turn)),
        "inference_base_url": str(
            getattr(settings, "MLAI_CODING_INFERENCE_BASE_URL", "https://inference.mlai.au/v1")
        ).rstrip("/"),
        "inference_ticket": issued.token,
        "ticket_type": "Bearer",
        "ticket_expires_at": issued.expires_at,
        "turn_expires_at": turn.expires_at,
    }


class CodingJwksView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def get(self, request):
        try:
            response = Response(ticket_jwks(), status=status.HTTP_200_OK)
        except CodingError as exc:
            return _error_response(exc)
        response["Cache-Control"] = "public, max-age=300"
        return response


class CodingEntitlementView(APIView):
    authentication_classes = (CommunityChatAccountAuthentication,)
    permission_classes = [IsAuthenticated]

    def get(self, request):
        # This endpoint is polled frequently by every Desktop client. Keep its
        # reconciliation work strictly account-scoped; the management command
        # owns global sweeps.
        reconcile_coding_reservations(user=request.user)
        try:
            pricing = current_pricing()
        except CodingError as exc:
            return _error_response(exc)
        balance = PointsService.get_balance(request.user)
        active = (
            CodingTurn.objects.filter(
                user=request.user,
                status__in=(CodingTurn.Status.ACTIVE, CodingTurn.Status.RECONCILING),
            )
            .select_related("pricing_version")
            .first()
        )
        pilot_access = user_has_pilot_access(request.user)
        return Response(
            {
                "pilot_access": pilot_access,
                "can_start_turn": bool(
                    pilot_access and balance["balance_microroo"] > 0 and active is None
                ),
                "model": "kimi-k3",
                "balance_microroo": microroo_string(balance["balance_microroo"]),
                "balance_roo": roo_decimal_string(balance["balance_microroo"]),
                "active_turn": _turn_payload(active) if active else None,
                "pricing": pricing_payload(pricing),
                "runtime": {
                    "desktop_only": True,
                    "kimi_code_version": "0.36.1",
                    "node_major": 24,
                },
            }
        )


class CodingTurnCreateView(APIView):
    authentication_classes = (CommunityChatAccountAuthentication,)
    permission_classes = [IsAuthenticated]

    def post(self, request):
        try:
            # Ticket generation is in the same transaction as turn creation so
            # a signing outage cannot strand the user's full Roo reservation.
            with transaction.atomic():
                turn, created = create_turn(
                    user=request.user,
                    account_session=getattr(request, "community_chat_account_session", None),
                    idempotency_key=request.data.get("idempotency_key"),
                    local_session_id=request.data.get("local_session_id"),
                    model=str(request.data.get("model") or ""),
                )
                payload = _ticket_payload(turn)
        except CodingError as exc:
            return _error_response(exc)
        return Response(
            payload,
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )


class CodingTurnTicketRefreshView(APIView):
    authentication_classes = (CommunityChatAccountAuthentication,)
    permission_classes = [IsAuthenticated]

    def post(self, request, turn_id):
        reconcile_coding_reservations(user=request.user, turn_id=turn_id)
        turn = get_object_or_404(CodingTurn, id=turn_id, user=request.user)
        session = getattr(request, "community_chat_account_session", None)
        if session is None or turn.device_id != session.installation_id:
            return Response(
                {"code": "device_mismatch", "message": "This turn belongs to another device."},
                status=status.HTTP_403_FORBIDDEN,
            )
        if turn.status != CodingTurn.Status.ACTIVE:
            return Response(
                {"code": "turn_not_active", "message": "Coding turn is not active."},
                status=status.HTTP_409_CONFLICT,
            )
        try:
            payload = _ticket_payload(turn)
        except CodingError as exc:
            return _error_response(exc)
        return Response(payload)


class CodingTurnFinalizeView(APIView):
    authentication_classes = (CommunityChatAccountAuthentication,)
    permission_classes = [IsAuthenticated]

    def post(self, request, turn_id):
        turn = get_object_or_404(CodingTurn, id=turn_id, user=request.user)
        session = getattr(request, "community_chat_account_session", None)
        if session is None or turn.device_id != session.installation_id:
            return Response(
                {"code": "device_mismatch", "message": "This turn belongs to another device."},
                status=status.HTTP_403_FORBIDDEN,
            )
        try:
            turn, _ = finalize_turn(
                turn=turn,
                outcome=str(request.data.get("outcome") or ""),
            )
        except CodingError as exc:
            return _error_response(exc)
        balance = PointsService.get_balance(request.user)
        has_ambiguous = turn.model_calls.filter(
            status=CodingModelCall.Status.AMBIGUOUS
        ).exists()
        return Response(
            {
                "turn_id": str(turn.id),
                "status": turn.status,
                "charged_microroo": microroo_string(turn.settled_microroo),
                "released_microroo": microroo_string(turn.released_microroo),
                "balance_microroo": microroo_string(balance["balance_microroo"]),
                "balance_roo": roo_decimal_string(balance["balance_microroo"]),
            },
            status=status.HTTP_202_ACCEPTED if has_ambiguous else status.HTTP_200_OK,
        )
