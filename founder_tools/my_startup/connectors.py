"""Start browser OAuth with the already authenticated Chat identity."""

from urllib.parse import urlencode, urlsplit

from django.http import QueryDict
from integrations.models import ExternalServiceConnection
from integrations.api_views_connectors import _org_scope_or_response
from integrations.services.external_connectors import disconnect_external_connection
from rest_framework.response import Response
from rest_framework.views import APIView

from integrations.views import connector_connect, google_connect
from .api import MyStartupViewMixin
from .links import frontend_origin


def safe_startup_return(value):
    """Accept only a normal My startup route on the configured Chat origin."""
    try:
        parsed = urlsplit(str(value or ""))
        origin = urlsplit(frontend_origin())
        if (parsed.scheme, parsed.netloc) != (origin.scheme, origin.netloc):
            return None
        if parsed.username or parsed.password or "\\" in value or parsed.fragment:
            return None
        if parsed.path != "/my-startup" and not parsed.path.startswith("/my-startup/"):
            return None
        if (
            any(part in {".", ".."} for part in parsed.path.split("/"))
            or "%" in parsed.path
        ):
            return None
        return parsed.geturl()
    except (TypeError, ValueError):
        return None


class MyStartupConnectorConnectView(MyStartupViewMixin, APIView):
    """Return provider authorization without consulting a legacy account cookie."""

    def post(self, request, provider):
        if provider not in {"google", "google_analytics", "slack"}:
            return Response({"detail": "Unsupported marketing connector."}, status=404)
        next_url = safe_startup_return(request.data.get("next"))
        if not next_url:
            return Response(
                {"detail": "Choose a valid My startup return URL."}, status=400
            )
        company_id = request.query_params.get("company_id") or request.data.get(
            "company_id"
        )
        if not company_id:
            return Response({"detail": "A startup is required."}, status=400)
        raw = request._request
        # DRF selected the Chat account. The existing OAuth views see exactly
        # that account, regardless of an unrelated JWT or Django session.
        raw.user = request.user
        raw.GET = QueryDict(
            urlencode(
                {
                    "next": next_url,
                    "company_id": company_id,
                    "scope": "website_baseline",
                }
            )
        )
        result = (
            google_connect(raw)
            if provider == "google"
            else connector_connect(raw, provider)
        )
        if result.status_code in {301, 302, 303}:
            return Response({"authUrl": result["Location"]})
        return Response(
            {"detail": result.content.decode("utf-8", errors="replace")[:500]},
            status=result.status_code,
        )


class MyStartupConnectorDisconnectView(MyStartupViewMixin, APIView):
    """Disconnect only a marketing connection belonging to the selected startup."""

    def delete(self, request, connection_id):
        if not (
            request.query_params.get("company_id")
            or request.query_params.get("companyId")
        ):
            return Response({"detail": "A startup is required."}, status=400)
        scope, error = _org_scope_or_response(request)
        if error is not None:
            return error
        if (
            scope is None
            or not ExternalServiceConnection.objects.filter(
                pk=connection_id,
                user=request.user,
                organization=scope,
                provider__in=("google_analytics", "slack"),
            ).exists()
        ):
            return Response({"detail": "Connection not found."}, status=404)
        if not disconnect_external_connection(request.user, connection_id):
            return Response({"detail": "Connection not found."}, status=404)
        return Response({"status": "disconnected"})
