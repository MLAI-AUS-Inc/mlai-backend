"""Owner-only Antiburn syncing, independent of public leaderboard visibility."""

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import TokenUsageAccount
from .throttles import CommunityChatScopedThrottle
from .token_insights import WINDOWS, current_report, validate_report
from .usage_views import ACCOUNT_AUTHENTICATION_CLASSES


class TokenInsightsView(APIView):
    """Read, consent to, and publish only the authenticated member's own tips."""

    authentication_classes = ACCOUNT_AUTHENTICATION_CLASSES
    permission_classes = [IsAuthenticated]
    throttle_classes = [CommunityChatScopedThrottle]
    community_chat_throttle_scope = "token_usage_insights"

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        response["Cache-Control"] = "private, no-store"
        return response

    def get(self, request):
        window = request.query_params.get("window", "today")
        if window not in WINDOWS:
            return Response({"error": "Invalid window."}, status=400)
        account = TokenUsageAccount.objects.filter(user=request.user).first()
        enabled = account is not None and account.insights_enabled
        zone = settings.TOKEN_USAGE_LEADERBOARD_TIME_ZONE
        report = current_report(account.insights_reports, window, zone, now=timezone.now()) if enabled else None
        return Response({"enabled": enabled, "timezone": zone, "report": report})

    def patch(self, request):
        if not isinstance(request.data, dict) or set(request.data) != {"enabled"} or type(request.data["enabled"]) is not bool:
            return Response({"error": "enabled must be a boolean."}, status=400)
        with transaction.atomic():
            account = TokenUsageAccount.objects.select_for_update().filter(user=request.user).first()
            if account is None:
                return Response({"error": "Connect token reporting first."}, status=409)
            account.insights_enabled = request.data["enabled"]
            if not account.insights_enabled:
                account.insights_reports = {}
            account.save(update_fields=["insights_enabled", "insights_reports", "updated_at"])
        return Response({"enabled": account.insights_enabled})

    def post(self, request):
        zone = settings.TOKEN_USAGE_LEADERBOARD_TIME_ZONE
        if not isinstance(request.data, dict) or set(request.data) != {"report", "timezone"} or request.data["timezone"] != zone:
            return Response({"error": "Expected an aggregate report in the leaderboard timezone."}, status=400)
        try:
            report = validate_report(request.data["report"], now=timezone.now())
        except (ValueError, TypeError):
            return Response({"error": "Invalid aggregate report."}, status=400)
        with transaction.atomic():
            account = TokenUsageAccount.objects.select_for_update().filter(user=request.user).first()
            if account is None or not account.insights_enabled:
                return Response({"error": "Private tip syncing is disabled."}, status=409)
            reports = dict(account.insights_reports)
            previous = reports.get(report["window"], {}).get("report", {})
            if previous.get("computedAt", "") > report["computedAt"]:
                return Response({"accepted": False})
            reports[report["window"]] = {"timezone": zone, "report": report}
            account.insights_reports = reports
            account.save(update_fields=["insights_reports", "updated_at"])
        return Response({"accepted": True})
