"""HTTP surface for MLAI-hosted article directories."""

import logging

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
            return Response({"code": "hosted_site_not_found", "detail": "No hosted site for this domain."}, status=404)
        return Response(hosted.site_render_state(site))

    def put(self, request):
        data = request.data if isinstance(request.data, dict) else {}
        try:
            site = hosted.record_site_deployment(domain=data.get("domain"), data=data)
        except hosted.HostedPublishError as exc:
            return _error(exc)
        return Response(hosted.site_render_state(site))


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
        site = hosted.active_site(context.organization)
        if site is None:
            return None, None, None, Response({"code": "hosted_site_not_active",
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
        """Approve the reviewed revision exactly as the repository flow does, then publish it."""
        from .article_review_views import check_approval_comments, record_review_approval, remote_review
        from .portable_drafts import original_portable_run
        context, run, site, error = self._resolve(request, run_id)
        if error:
            return error
        if run.workflow not in views.ARTICLE_WORKFLOWS or not original_portable_run(run):
            return Response({"code": "not_a_hosted_article", "detail": "Only articles written for your hosted blog can be published there."}, status=400)
        if views._latest_review_ready_component_revision(run, context) is not None:
            return Response({"code": "newer_revision", "detail": "A newer draft is ready. Open that revision before publishing."}, status=409)
        data = request.data if isinstance(request.data, dict) else {}
        review = remote_review(run)
        if isinstance(review, Response):
            return review
        reviewed = str(data.get("reviewedRevision") or "")
        if not reviewed or reviewed != str(review.get("revision") or "") or review.get("previewPending"):
            return Response({"code": "review_changed", "detail": "The article changed since you reviewed it. Review it again before publishing."}, status=409)
        blocked = check_approval_comments(run, data)
        if blocked is not None:
            return blocked
        try:
            publication = hosted.request_publication(site=site, run=run, user=getattr(context.profile, "user", None))
        except hosted.HostedPublishError as exc:
            return _error(exc)
        record_review_approval(run, context, data)
        dispatch = _dispatch_hosted_publish(publication, reviewed)
        if not dispatch["queued"]:
            hosted.record_progress(domain=site.domain, run_id=run.run_id,
                                   data={"status": "failed", "error_code": "hosted_dispatch_failed", "error": dispatch["error"]})
            publication.refresh_from_db()
            logger.warning("hosted_publish_dispatch_failed run_id=%s", run.run_id)
            return Response(hosted.publication_payload(publication), status=status.HTTP_503_SERVICE_UNAVAILABLE)
        return Response(hosted.publication_payload(publication), status=status.HTTP_202_ACCEPTED)
