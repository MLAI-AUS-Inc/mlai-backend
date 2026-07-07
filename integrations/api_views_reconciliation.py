from __future__ import annotations

from datetime import datetime, time, timedelta, timezone

from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from core.permissions import HasRooApiKey
from roo.permissions import is_points_admin
from integrations.services.reconciliation import (
    ReconciliationReportService,
    StripeAPIError,
    StripeConfigurationError,
)


MAX_WINDOW_DAYS = 92
DEFAULT_WINDOW_DAYS = 30


class ReconciliationReportView(APIView):
    """Roo-only endpoint: Luma->Stripe reconciliation report for Points Admins.

    Payout-driven: returns each Stripe payout (= one bank deposit) with the
    ticket charges behind it, a Cowork markdown brief, and an optional xlsx
    audit workbook. Read-only against Stripe. Points-Admin gated (contains PII).
    """

    authentication_classes = []
    permission_classes = [HasRooApiKey]

    def get(self, request):
        slack_user_id = str(request.query_params.get("slack_user_id") or "").strip()
        if not slack_user_id:
            return Response({"error": "slack_user_id is required"}, status=status.HTTP_400_BAD_REQUEST)

        if not is_points_admin(slack_user_id):
            return Response(
                {"error": "Only Points Admins (admin, committee, or portfolio_lead) can run reconciliation reports"},
                status=status.HTTP_403_FORBIDDEN,
            )

        window, error_response = self._resolve_window(request.query_params)
        if error_response:
            return error_response
        since, until = window

        include_workbook = self._parse_bool(request.query_params.get("include_workbook"), default=True)

        service = ReconciliationReportService()
        try:
            report = service.build_report(since=since, until=until, include_workbook=include_workbook)
        except StripeConfigurationError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        except StripeAPIError as exc:
            if exc.status_code == 429:
                return Response({"error": str(exc)}, status=status.HTTP_429_TOO_MANY_REQUESTS)
            return Response({"error": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)

        return Response(report)

    # ---- window parsing --------------------------------------------------

    def _resolve_window(self, params):
        """Return ((since, until), None) or (None, error_response).

        Precedence: explicit since/until (YYYY-MM-DD) override the rolling
        `days` window. `until` defaults to now; `since` defaults to
        until - days. Window is capped at MAX_WINDOW_DAYS.
        """
        now = datetime.now(timezone.utc)

        # Validate `days` unconditionally so a malformed value is a clear 400 even
        # when since/until take precedence over it.
        days, err = self._parse_days(params.get("days"))
        if err:
            return None, err

        until, err = self._parse_date_end(params.get("until"), default=now)
        if err:
            return None, err
        since, err = self._parse_date_start(params.get("since"), default=None)
        if err:
            return None, err

        if since is None:
            since = until - timedelta(days=days)

        if since >= until:
            return None, Response(
                {"error": "since must be before until"}, status=status.HTTP_400_BAD_REQUEST
            )
        # Compare on calendar days: an explicit end date is inflated to end-of-day,
        # so a raw timedelta would spuriously reject an exactly-MAX_WINDOW_DAYS span.
        if (until.date() - since.date()).days > MAX_WINDOW_DAYS:
            return None, Response(
                {"error": f"window too large; max {MAX_WINDOW_DAYS} days"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return (since, until), None

    @staticmethod
    def _parse_days(raw_value):
        if raw_value in (None, ""):
            return DEFAULT_WINDOW_DAYS, None
        try:
            days = int(raw_value)
        except (TypeError, ValueError):
            return None, Response({"error": "days must be an integer"}, status=status.HTTP_400_BAD_REQUEST)
        if days < 1:
            return None, Response({"error": "days must be at least 1"}, status=status.HTTP_400_BAD_REQUEST)
        return min(days, MAX_WINDOW_DAYS), None

    @staticmethod
    def _parse_date_start(raw_value, *, default):
        if raw_value in (None, ""):
            return default, None
        try:
            d = datetime.fromisoformat(str(raw_value).strip()).date()
        except ValueError:
            return None, Response({"error": "since must use YYYY-MM-DD"}, status=status.HTTP_400_BAD_REQUEST)
        return datetime.combine(d, time.min, tzinfo=timezone.utc), None

    @staticmethod
    def _parse_date_end(raw_value, *, default):
        if raw_value in (None, ""):
            return default, None
        try:
            d = datetime.fromisoformat(str(raw_value).strip()).date()
        except ValueError:
            return None, Response({"error": "until must use YYYY-MM-DD"}, status=status.HTTP_400_BAD_REQUEST)
        # inclusive end-of-day for the given date
        return datetime.combine(d, time.max, tzinfo=timezone.utc), None

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
