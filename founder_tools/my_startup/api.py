"""Chat-only adapters for the existing founder-owned business operations."""

from uuid import UUID

from django.http import HttpResponseBadRequest
from rest_framework.exceptions import AuthenticationFailed
from rest_framework.permissions import IsAuthenticated

from community_chat.account_sessions import ACCESS_TOKEN_PREFIX
from community_chat.authentication import CommunityChatAccountAuthentication

from .links import API_PREFIX, rewrite_payload


class MyStartupAuthentication(CommunityChatAccountAuthentication):
    """Reject explicit foreign credentials instead of falling back to cookies."""

    def authenticate(self, request):
        header = request.headers.get("Authorization", "")
        if header and not header.startswith(f"Bearer {ACCESS_TOKEN_PREFIX}"):
            raise AuthenticationFailed("Use your MLAI Chat account session.")
        return super().authenticate(request)


class MyStartupViewMixin:
    authentication_classes = (MyStartupAuthentication,)

    def dispatch(self, request, *args, **kwargs):
        # Preview subresources have no fetch headers: pin their startup in the
        # URL, then let the original view perform its existing ownership check.
        scoped_company = kwargs.pop("startup_company_id", None)
        if scoped_company:
            requested = request.GET.get("company_id") or request.GET.get("companyId")
            if requested and str(requested) != str(scoped_company):
                return HttpResponseBadRequest("Conflicting startup identity.")
            query = request.GET.copy()
            query["company_id"] = str(scoped_company)
            request.GET = query
            request.META["QUERY_STRING"] = query.urlencode()
        return super().dispatch(request, *args, **kwargs)

    def get_permissions(self):
        return [IsAuthenticated(), *super().get_permissions()]

    def finalize_response(self, request, response, *args, **kwargs):
        company_id = (
            request.query_params.get("company_id")
            or request.query_params.get("companyId")
            or ""
        )
        try:
            company_id = str(UUID(str(company_id))) if company_id else ""
        except ValueError:
            company_id = ""
        if hasattr(response, "data"):
            response.data = rewrite_payload(response.data, company_id=company_id)
        elif (
            company_id
            and kwargs.get("run_id")
            and not getattr(response, "streaming", False)
        ):
            content_type = response.get("Content-Type", "").lower()
            if any(
                kind in content_type for kind in ("text/", "javascript", "json", "xml")
            ):
                run_id = kwargs["run_id"]
                old = f"/api/v1/vibe-marketing/runs/{run_id}/live-preview/".encode()
                new = f"{API_PREFIX}companies/{company_id}/vibe-marketing/runs/{run_id}/live-preview/".encode()
                response.content = response.content.replace(old, new)
                # The representation was rewritten; upstream validators no
                # longer describe these bytes.
                for header in ("ETag", "Content-MD5"):
                    if header in response:
                        del response[header]
        response["Cache-Control"] = "private, no-store"
        response["Referrer-Policy"] = "no-referrer"
        return super().finalize_response(request, response, *args, **kwargs)


def startup_view(view_class, initkwargs=None):
    """Reuse a reviewed APIView without mutating its legacy authentication."""
    adapted = type(
        f"MyStartup{view_class.__name__}",
        (MyStartupViewMixin, view_class),
        {
            "__module__": __name__,
            "__doc__": f"Chat account adapter for {view_class.__name__}.",
        },
    )
    return adapted.as_view(**(initkwargs or {}))
