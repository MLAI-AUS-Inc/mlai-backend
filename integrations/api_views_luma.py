from __future__ import annotations

from datetime import date

from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from core.permissions import HasRooApiKey
from roo.permissions import can_export_luma_attendees
from integrations.services.luma import (
    LumaAPIError,
    LumaAttendeeReportService,
    LumaConfigurationError,
)


class LumaAttendeeReportView(APIView):
    """Roo-only endpoint for Luma attendee summaries and CSV payloads."""

    authentication_classes = []
    permission_classes = [HasRooApiKey]

    def get(self, request):
        slack_user_id = str(request.query_params.get("slack_user_id") or "").strip()
        if not slack_user_id:
            return Response({"error": "slack_user_id is required"}, status=status.HTTP_400_BAD_REQUEST)

        if not can_export_luma_attendees(slack_user_id):
            return Response(
                {"error": "Only Points Admins with admin, committee, or partner role can export Luma attendee data"},
                status=status.HTTP_403_FORBIDDEN,
            )

        event_count, error_response = self._parse_event_count(request.query_params.get("event_count"))
        if error_response:
            return error_response

        event_date, error_response = self._parse_event_date(request.query_params.get("event_date"))
        if error_response:
            return error_response

        approval_status = str(request.query_params.get("approval_status") or "approved").strip() or "approved"
        include_csv = self._parse_bool(request.query_params.get("include_csv"), default=False)

        service = LumaAttendeeReportService()
        try:
            report = service.build_attendee_report(
                event_count=event_count,
                event_date=event_date,
                approval_status=approval_status,
                include_csv=include_csv,
            )
        except LumaConfigurationError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        except LumaAPIError as exc:
            status_code = exc.status_code or status.HTTP_502_BAD_GATEWAY
            if status_code in (401, 403):
                return Response({"error": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)
            if status_code == 429:
                return Response({"error": str(exc)}, status=status.HTTP_429_TOO_MANY_REQUESTS)
            return Response({"error": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)

        return Response(report)

    @staticmethod
    def _parse_event_count(raw_value):
        if raw_value in (None, ""):
            return 3, None
        try:
            count = int(raw_value)
        except (TypeError, ValueError):
            return None, Response({"error": "event_count must be an integer"}, status=status.HTTP_400_BAD_REQUEST)
        if count < 1:
            return None, Response({"error": "event_count must be at least 1"}, status=status.HTTP_400_BAD_REQUEST)
        return min(count, 10), None

    @staticmethod
    def _parse_event_date(raw_value):
        if raw_value in (None, ""):
            return None, None
        try:
            return date.fromisoformat(str(raw_value).strip()), None
        except ValueError:
            return None, Response({"error": "event_date must use YYYY-MM-DD"}, status=status.HTTP_400_BAD_REQUEST)

    @staticmethod
    def _parse_bool(raw_value, *, default: bool) -> bool:
        if raw_value is None:
            return default
        normalized = str(raw_value).strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
        return default
