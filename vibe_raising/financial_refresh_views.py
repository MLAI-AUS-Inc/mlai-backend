"""Founder-only asynchronous financial previews; no generation or publication."""
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import UserRateThrottle
from rest_framework.views import APIView

from startup_updates.financial_refresh import refresh_status, start_refresh


class FinancialRefreshThrottle(UserRateThrottle):
    scope = "pulse_financial_refresh"
    rate = "12/hour"


class FinancialRefreshPollThrottle(UserRateThrottle):
    scope = "pulse_financial_refresh_poll"
    rate = "60/minute"


class FinancialRefreshBase(APIView):
    permission_classes = [IsAuthenticated]

    def company_context(self, request):
        from vibe_raising.views import _company_id_from_request, _get_founder_company_context_or_response
        if not _company_id_from_request(request):
            return None, Response({"companyId": "Choose a startup before refreshing financials."}, status=400)
        return _get_founder_company_context_or_response(request)

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        response["Cache-Control"] = "private, no-store"
        return response


class FinancialRefreshView(FinancialRefreshBase):
    throttle_classes = [FinancialRefreshThrottle]

    def post(self, request):
        context, error = self.company_context(request)
        if error is not None:
            return error
        return Response(start_refresh(company=context["company"], user=request.user, data=request.data), status=202)


class FinancialRefreshStatusView(FinancialRefreshBase):
    throttle_classes = [FinancialRefreshPollThrottle]

    def get(self, request, refresh_id):
        context, error = self.company_context(request)
        if error is not None:
            return error
        return Response(refresh_status(company=context["company"], user=request.user, refresh_id=refresh_id))
