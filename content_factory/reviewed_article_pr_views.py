"""Founder consent and isolated service authorization for reviewed article draft PRs."""
from rest_framework.response import Response
from rest_framework.views import APIView

from core.permissions import HasRooApiKey
from workflow_runs.models import ContentFactoryRun
from . import reviewed_article_pr as reviewed
from . import reviewed_article_pr_worker as worker
from . import vibe_marketing_views as views
from .website_contract import WebsiteAuthorityError


class FounderReviewedArticlePRView(APIView):
    """Show the selected destination, then accept an explicit approval for these bytes."""

    def _resolve(self, request, run_id):
        if not reviewed.enabled():
            raise reviewed.Unavailable("not_found", 404)
        ctx, error = views._resolve_context_or_response(request, require_domain=False)
        if error is not None:
            return None, None, error
        run = ContentFactoryRun.objects.filter(organization_id=ctx.organization.pk, run_id=run_id).first()
        if not run or not views._run_belongs_to_context(run, ctx):
            raise reviewed.Unavailable("not_found", 404)
        if not reviewed.may_publish(request.user, ctx.organization):
            raise reviewed.Unavailable("not_permitted", 403)
        return ctx, run, None

    def get(self, request, run_id):
        try:
            ctx, run, error = self._resolve(request, run_id)
            if error is not None:
                return error
            outcome = reviewed.offer(ctx, run, request.user)
        except reviewed.Unavailable as exc:
            if exc.status in {403, 404}:
                return Response({"reason": str(exc)}, status=exc.status)
            outcome = {"available": False}
        except WebsiteAuthorityError:
            outcome = {"available": False}
        return Response(outcome, headers={"Cache-Control": "private, no-store"})

    def post(self, request, run_id):
        try:
            ctx, run, error = self._resolve(request, run_id)
            if error is not None:
                return error
            op = reviewed.reserve(ctx, run, request.user, request.data)
            worker.advance(op.pk)
            op.refresh_from_db()
            return Response(reviewed.summary(op), headers={"Cache-Control": "private, no-store"})
        except reviewed.Unavailable as exc:
            return Response({"reason": str(exc)}, status=exc.status)
        except WebsiteAuthorityError as exc:
            return Response(exc.as_dict(), status=exc.status)


class ReviewedArticlePRAuthorityView(APIView):
    """Private worker control, scoped to a single approved PR staging operation."""

    authentication_classes = []
    permission_classes = [HasRooApiKey]
    credential = False

    def post(self, request):
        try:
            if self.credential:
                return Response(worker.credential(request.data), headers={"Cache-Control": "no-store"})
            with reviewed.authority(request.data) as op:
                return Response({"authorized": True, **op.payload["wire"]}, headers={"Cache-Control": "no-store"})
        except reviewed.Unavailable as exc:
            return Response({"authorized": False, "reason": str(exc)}, status=exc.status)
        except WebsiteAuthorityError as exc:
            return Response(exc.as_dict(), status=exc.status)
