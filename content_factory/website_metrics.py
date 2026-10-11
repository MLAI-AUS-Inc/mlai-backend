"""Company-scoped health counters for the founder's advanced website panel."""
from datetime import timedelta

from django.utils import timezone
from rest_framework.response import Response
from rest_framework.views import APIView

from .one_click_metrics import one_click_outcomes
from .website_views import _context


class WebsiteMetricsView(APIView):
    """Read the selected founder company's seven-day one-click outcomes."""

    def get(self, request):
        context, _config, error = _context(request)
        if error:
            return error
        now = timezone.now()
        outcomes = one_click_outcomes(since=now - timedelta(days=7), domain=context.organization.domain)
        return Response({"websiteHealth": {"windowHours": 168, "asOf": now.isoformat(), "oneClick": outcomes}})
