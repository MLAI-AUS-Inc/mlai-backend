"""Strictly service-authenticated inference metering endpoints."""

from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from core.permissions import HasStrictRooApiKey

from .coding import (
    CodingError,
    USAGE_ENVELOPE_FAILURE_REASON,
    admit_call,
    fail_call,
    microroo_string,
    settle_call,
    start_call_dispatch,
)
from .models import CodingModelCall


def _error_response(exc: CodingError) -> Response:
    return Response(
        {"code": exc.code, "message": exc.message, **exc.extra},
        status=exc.http_status,
    )


class CodingCallAdmitView(APIView):
    permission_classes = [HasStrictRooApiKey]

    def post(self, request):
        try:
            call, created, remaining = admit_call(
                turn_id=request.data.get("turn_id"),
                call_id=request.data.get("call_id"),
                subject=request.data.get("subject"),
                device_id=request.data.get("device_id"),
                estimated_input_tokens=request.data.get("estimated_input_tokens"),
                requested_output_tokens=request.data.get("requested_output_tokens"),
                dispatch_owner=request.data.get("dispatch_owner"),
            )
        except CodingError as exc:
            return _error_response(exc)
        return Response(
            {
                "status": "reserved" if created else "already_reserved",
                # Admission reserves Roo but deliberately does not authorize a
                # provider request. The gateway must obtain the one-shot
                # dispatch-start acknowledgement below.
                "dispatch_allowed": False,
                "dispatch_start_required": True,
                "dispatch_lease_expires_at": call.dispatch_lease_expires_at,
                "reservation_id": str(call.id),
                "turn_id": str(call.turn_id),
                "call_id": str(call.call_id),
                "max_output_tokens": call.max_output_tokens,
                "reserved_microroo": microroo_string(call.reserved_microroo),
                "remaining_microroo": microroo_string(remaining),
            },
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )


class CodingCallDispatchView(APIView):
    permission_classes = [HasStrictRooApiKey]

    def post(self, request):
        try:
            call, started, remaining = start_call_dispatch(
                reservation_id=request.data.get("reservation_id"),
                turn_id=request.data.get("turn_id"),
                call_id=request.data.get("call_id"),
                dispatch_owner=request.data.get("dispatch_owner"),
            )
        except CodingError as exc:
            return _error_response(exc)
        return Response(
            {
                "status": "dispatch_started" if started else "already_started",
                "dispatch_allowed": started,
                "reservation_id": str(call.id),
                "turn_id": str(call.turn_id),
                "call_id": str(call.call_id),
                "remaining_microroo": microroo_string(remaining),
            },
            status=status.HTTP_201_CREATED if started else status.HTTP_200_OK,
        )


class CodingCallSettleView(APIView):
    permission_classes = [HasStrictRooApiKey]

    def post(self, request):
        try:
            call, created, balance, remaining = settle_call(
                reservation_id=request.data.get("reservation_id"),
                turn_id=request.data.get("turn_id"),
                call_id=request.data.get("call_id"),
                provider_request_id=request.data.get("provider_request_id"),
                trace_id=request.data.get("provider_trace_id"),
                input_tokens=request.data.get("input_tokens"),
                cached_input_tokens=request.data.get("cached_input_tokens"),
                output_tokens=request.data.get("output_tokens"),
                dispatch_owner=request.data.get("dispatch_owner"),
            )
        except CodingError as exc:
            return _error_response(exc)
        if call.status == CodingModelCall.Status.RELEASED:
            return Response(
                {
                    "status": "released_unbilled",
                    "reservation_id": str(call.id),
                    "turn_id": str(call.turn_id),
                    "call_id": str(call.call_id),
                    "charged_microroo": "0",
                    "balance_microroo": microroo_string(balance),
                    "remaining_microroo": microroo_string(remaining),
                },
                status=status.HTTP_200_OK,
            )
        if call.failure_reason == USAGE_ENVELOPE_FAILURE_REASON:
            return Response(
                {
                    "status": "usage_rejected" if created else "already_rejected",
                    "code": USAGE_ENVELOPE_FAILURE_REASON,
                    "message": (
                        "Reported provider usage exceeded the admitted token envelope. "
                        "The reservation is held for reconciliation and no Roo was charged."
                    ),
                    "reservation_id": str(call.id),
                    "turn_id": str(call.turn_id),
                    "call_id": str(call.call_id),
                    "estimated_input_tokens": call.estimated_input_tokens,
                    "max_output_tokens": call.max_output_tokens,
                    "reported_input_tokens": call.input_tokens,
                    "reported_output_tokens": call.output_tokens,
                    "charged_microroo": "0",
                    "balance_microroo": microroo_string(balance),
                    "remaining_microroo": microroo_string(remaining),
                    "reconcile_after": call.reconcile_after,
                },
                # The first response is an explicit contract rejection. An
                # identical outbox replay is terminal and receives 200 so the
                # gateway can acknowledge it instead of retrying forever.
                status=(
                    status.HTTP_422_UNPROCESSABLE_ENTITY
                    if created
                    else status.HTTP_200_OK
                ),
            )
        return Response(
            {
                "status": "settled" if created else "already_settled",
                "reservation_id": str(call.id),
                "turn_id": str(call.turn_id),
                "call_id": str(call.call_id),
                "charged_microroo": microroo_string(call.charged_microroo),
                "calculated_microroo": microroo_string(call.calculated_microroo),
                "pricing_shortfall_microroo": microroo_string(
                    max(call.calculated_microroo - call.charged_microroo, 0)
                ),
                "balance_microroo": microroo_string(balance),
                "remaining_microroo": microroo_string(remaining),
            },
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )


class CodingCallFailView(APIView):
    permission_classes = [HasStrictRooApiKey]

    def post(self, request):
        try:
            call, changed, remaining = fail_call(
                reservation_id=request.data.get("reservation_id"),
                turn_id=request.data.get("turn_id"),
                call_id=request.data.get("call_id"),
                reason=request.data.get("reason"),
                ambiguous=request.data.get("ambiguous"),
                dispatch_owner=request.data.get("dispatch_owner"),
                provider_request_id=request.data.get("provider_request_id"),
                trace_id=request.data.get("provider_trace_id"),
            )
        except CodingError as exc:
            return _error_response(exc)
        return Response(
            {
                "status": (
                    "already_released"
                    if call.status == CodingModelCall.Status.RELEASED and not changed
                    else call.status
                ),
                "changed": changed,
                "reservation_id": str(call.id),
                "turn_id": str(call.turn_id),
                "call_id": str(call.call_id),
                "remaining_microroo": microroo_string(remaining),
                "reconcile_after": call.reconcile_after,
            }
        )
