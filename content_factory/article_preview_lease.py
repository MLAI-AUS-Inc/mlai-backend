"""Short-lived read-only preview grants for clients without browser cookies."""
from types import SimpleNamespace
from urllib.parse import quote, unquote
import re

from django.core import signing
from django.http import HttpResponse
from django.utils.decorators import method_decorator
from django.views.decorators.clickjacking import xframe_options_exempt
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from . import vibe_marketing_views as views
from .website_connections import authority_guard
from .website_contract import WebsiteAuthorityError, connection_contract
from .portable_drafts import original_portable_run
from .website_contract import evidence_digest

SALT = "article-preview-read-only-v1"
LIFETIME = 900


def safe_preview_path(path):
    """Reject traversal before the granted run's service URL is constructed."""
    decoded = str(path or "")
    for _ in range(8):
        next_path = unquote(decoded)
        if next_path == decoded:
            break
        decoded = next_path
    else:
        return False
    return not (decoded.startswith("/") or "\\" in decoded or "\x00" in decoded
                or any(part in {".", ".."} for part in decoded.split("/")))



class ArticlePreviewLeaseView(APIView):
    """Mint a grant only after the existing company/run authorization check."""

    @views.guarded_owner_operation("preview", run_operation=True)
    def post(self, request, run_id):
        context, error = views._resolve_context_or_response(request, require_domain=False)
        if error is not None:
            return error
        run = views.get_object_or_404(views.ContentFactoryRun, run_id=run_id)
        if not views._run_belongs_to_context(run, context):
            return Response({"detail": "Run not found."}, status=404)
        portable = original_portable_run(run)
        token = signing.dumps({"run": run.run_id, "organization": context.organization.id,
                               "domain": context.organization.domain, "portable": portable,
                               "intent_digest": evidence_digest({key: (run.run_request or {}).get(key) for key in ("delivery_mode", "delivery_mode_confirmed", "source_run_id")}) if portable else None,
                               **connection_contract(run.run_request or {})}, salt=SALT, compress=True)
        prefix = f"/api/v1/vibe-marketing/article-preview/{quote(token, safe='')}/{quote(run.run_id, safe='')}/"
        return Response({"url": request.build_absolute_uri(prefix), "expiresIn": LIFETIME},
                        headers={"Cache-Control": "no-store"})


@method_decorator(xframe_options_exempt, name="dispatch")
class ArticlePreviewLeaseProxyView(views.VibeMarketingRunLivePreviewProxyView):
    """GET-only capability; it never authenticates the holder to other APIs."""
    authentication_classes = []
    permission_classes = [AllowAny]
    http_method_names = ["get", "head"]

    def _resolve_run(self, request, run_id):
        try:
            grant = signing.loads(self.kwargs["token"], salt=SALT, max_age=LIFETIME)
        except signing.BadSignature:
            return None, None, Response({"detail": "Preview access expired. Reload the preview."}, status=401)
        if grant.get("run") != run_id:
            return None, None, Response({"detail": "Preview not found."}, status=404)
        run = views.get_object_or_404(views.ContentFactoryRun, run_id=run_id)
        context = SimpleNamespace(organization=SimpleNamespace(id=grant["organization"], domain=grant["domain"]))
        if not views._run_belongs_to_context(run, context):
            return None, None, Response({"detail": "Preview not found."}, status=404)
        if grant.get("portable") is True:
            if not original_portable_run(run) or grant.get("intent_digest") != evidence_digest({key: (run.run_request or {}).get(key) for key in ("delivery_mode", "delivery_mode_confirmed", "source_run_id")}):
                return None, None, Response({"code": "portable_preview_changed", "detail": "Preview access expired."}, status=409)
            return context, run, None
        try:
            with authority_guard(grant, action="read"):
                if connection_contract(grant) != connection_contract(run.run_request or {}):
                    raise WebsiteAuthorityError("website_connection_changed", "Preview access expired.")
        except WebsiteAuthorityError as exc:
            return None, None, Response(exc.as_dict(), status=exc.status)
        return context, run, None

    def get(self, request, run_id, token, proxy_path=""):
        if not safe_preview_path(proxy_path):
            return Response({"detail": "Preview not found."}, status=404)
        response = (views.VibeMarketingRunLivePreviewResourceView._proxy(self, request, run_id)
                    if proxy_path == "__resource" else self._proxy(request, run_id, proxy_path))
        if isinstance(response, HttpResponse):
            content_type = response.get("Content-Type", "")
            if not isinstance(response, Response) and any(
                kind in content_type for kind in ("text/", "javascript", "json")
            ):
                prefix = f"/api/v1/vibe-marketing/article-preview/{quote(token, safe='')}/{quote(run_id, safe='')}/"
                body = response.content
                for original in (f"/api/v1/vibe-marketing/runs/{run_id}/live-preview/proxy/",
                                 f"/api/v1/my-startup/vibe-marketing/runs/{run_id}/live-preview/proxy/"):
                    body = body.replace(original.encode(), prefix.encode())
                for original in (f"/api/v1/vibe-marketing/runs/{run_id}/live-preview/resource",
                                 f"/api/v1/my-startup/vibe-marketing/runs/{run_id}/live-preview/resource"):
                    body = body.replace(original.encode(), (prefix + "__resource").encode())
                response.content = body
            # Grants must never leak to remote images, analytics or navigations.
            response["Referrer-Policy"] = "no-referrer"
            response["Cache-Control"] = "private, no-store"
            response["Access-Control-Allow-Origin"] = "*"
            response["Cross-Origin-Resource-Policy"] = "cross-origin"
            response["Content-Security-Policy"] = "frame-ancestors 'self' https://chat.mlai.au https://mlai.au https://www.mlai.au tauri: http://tauri.localhost https://tauri.localhost; object-src 'none'; base-uri 'self'; sandbox allow-scripts"
            for header in ("Set-Cookie", "Location", "Content-Length"):
                if header in response:
                    del response[header]
        return response

    def head(self, request, run_id, token, proxy_path=""):
        return self.get(request, run_id, token, proxy_path)
