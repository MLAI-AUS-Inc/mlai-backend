"""HTTP surface for MLAI-hosted article directories."""

import logging

from django.conf import settings
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from integrations import http_client
from workflow_runs.models import ContentFactoryRun

from . import hosted_articles as hosted
from . import vibe_marketing_views as views
from .hosted_article_models import HostedArticlePublication, HostedArticleSite
from core.permissions import HasRooApiKey

logger = logging.getLogger(__name__)


def _error(exc: hosted.HostedPublishError) -> Response:
    return Response(exc.as_dict(), status=exc.status)


class HostedArticleSiteServiceView(APIView):
    """Content Factory reads a hosted site's profile and the articles its next deployment serves."""

    authentication_classes = []
    permission_classes = [HasRooApiKey]

    def get(self, request):
        domain = hosted.normalized_domain(request.query_params.get("domain"))
        site = HostedArticleSite.objects.filter(domain=domain).first()
        if site is None:
            return Response({"reason": "hosted_site_not_found", "detail": "No hosted site for this domain."}, status=404)
        return Response(hosted.site_render_state(site))

    def put(self, request):
        data = request.data if isinstance(request.data, dict) else {}
        try:
            site = hosted.record_site_deployment(domain=data.get("domain"), data=data)
        except hosted.HostedPublishError as exc:
            return _error(exc)
        return Response(hosted.site_render_state(site))


class HostedArticleSiteSourceServiceView(APIView):
    """Content Factory reads which repository commit a hosted site's next build checks out."""

    authentication_classes = []
    permission_classes = [HasRooApiKey]

    def get(self, request):
        domain = hosted.normalized_domain(request.query_params.get("domain"))
        site = HostedArticleSite.objects.filter(domain=domain).first()
        if site is None:
            return Response({"reason": "hosted_site_not_found", "detail": "No hosted site for this domain."}, status=404)
        try:
            return Response(hosted.site_source(site))
        except hosted.HostedPublishError as exc:
            return _error(exc)


class HostedArticlePublicationServiceView(APIView):
    """Content Factory reports rendering, upload and live-check progress for one publication."""

    authentication_classes = []
    permission_classes = [HasRooApiKey]

    def put(self, request, run_id):
        data = request.data if isinstance(request.data, dict) else {}
        try:
            publication = hosted.record_progress(domain=data.get("domain"), run_id=run_id, data=data)
        except hosted.HostedPublishError as exc:
            return _error(exc)
        return Response(hosted.publication_payload(publication))


def _dispatch_hosted_publish(publication: HostedArticlePublication, reviewed_revision: str) -> dict:
    """Ask Content Factory to render, upload and live-check the site with this article."""
    remote = views._content_factory_remote_config()
    if not remote["enabled"]:
        return {"queued": False, "error": views._content_factory_unavailable_message(remote)}
    site = publication.site
    try:
        response = http_client.post(
            f"{remote['base_url']}/api/runs/{publication.run_id}/hosted-publish",
            json={"domain": site.domain, "base_path": site.base_path, "publication_id": str(publication.pk),
                  "reviewed_revision": reviewed_revision},
            headers=views._content_factory_headers(),
            timeout=(3, 15),
        )
    except http_client.RequestException:
        return {"queued": False, "error": "Content Factory is unreachable."}
    if response.status_code in (200, 202):
        return {"queued": True}
    return {"queued": False, "error": f"Content Factory returned {response.status_code}."}


class HostedArticlePublishView(APIView):
    """The owner publishes a finished article to their MLAI-hosted blog, or reads its progress."""

    def _resolve(self, request, run_id):
        context, error = views._resolve_context_or_response(request, require_domain=False)
        if error:
            return None, None, None, error
        run = ContentFactoryRun.objects.filter(run_id=run_id).first()
        if run is None or not views._run_belongs_to_context(run, context):
            return None, None, None, Response({"detail": "Run not found."}, status=404)
        sites = HostedArticleSite.objects.filter(organization=context.organization)
        site = sites.filter(publications__run_id=run.run_id).first() or sites.filter(domain=hosted.normalized_domain(run.domain)).first()
        if site is None:
            return None, None, None, Response({"reason": "hosted_site_not_active",
                "detail": "Hosted publishing is not switched on for this website."}, status=409)
        return context, run, site, None

    def get(self, request, run_id):
        _context, run, site, error = self._resolve(request, run_id)
        if error:
            return error
        publication = site.publications.filter(run_id=run.run_id).first()
        if publication is None:
            return Response({"runId": run.run_id, "status": "not_published", "plannedUrl": ""})
        return Response(hosted.publication_payload(publication))

    def post(self, request, run_id):
        return Response({"reason": "hosting_retired", "detail": "MLAI no longer hosts blogs. Publish this article to your own website."}, status=410)
