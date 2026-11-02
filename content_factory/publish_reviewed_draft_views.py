"""Account-scoped, explicit-click entrypoint for publishing a saved draft."""
from django.conf import settings
from rest_framework.response import Response
from rest_framework.views import APIView

from community_chat.my_startup_views import MyStartupAccess
from founder_tools.idempotency import begin, finish, abandon, request_sha256
from founder_tools.services import get_founder_company_context
from workflow_runs.models import ContentFactoryRun
from .publish_reviewed_draft import PublishReviewedError, dismiss_offer, start_publish


class PublishReviewedDraftView(MyStartupAccess, APIView):
    """Publish only a run owned by the selected startup under current rights."""

    def post(self, request, run_id):
        """Keep source/user idempotency separate from the run's own single offer."""
        if not getattr(settings, 'ARTICLES_PUBLISH_REVIEWED_DRAFT_ENABLED', False):
            return Response({'detail': 'Not found.', 'reason': 'not_found'}, status=404)
        context = get_founder_company_context(request.user, company_id=self.company.pk)
        source = ContentFactoryRun.objects.select_related('organization').filter(
            run_id=run_id, organization=context.organization).first()
        if source is None:
            return Response({'detail': 'Not found.', 'reason': 'not_found'}, status=404)
        scope = ('publish_reviewed', source.run_id, request.user.pk)
        key = request.data.get('idempotencyKey') or request.headers.get('Idempotency-Key')
        digest = request_sha256(request.data)
        try:
            state, previous = begin(scope, key, digest)
        except ValueError:
            return Response({'detail': 'This request needs a valid save key.', 'reason': 'invalid_key'}, status=400)
        if state == 'replay':
            return Response(previous['body'], status=previous['status'], headers={'Idempotent-Replayed': 'true'})
        if state != 'new':
            return Response({'detail': 'This request is still being confirmed.', 'reason': 'operation_key_conflict'}, status=409)
        try:
            if request.data.get('action') == 'dismiss':
                dismiss_offer(source_run=source, user=request.user)
                body = {'status': 'dismissed'}
            else:
                run, created = start_publish(context=context, source_run=source, user=request.user)
                body = {'status': 'publishing', 'runId': run.run_id, 'alreadyStarted': not created}
        except PublishReviewedError as exc:
            abandon(scope, key, digest)
            return Response({'detail': exc.detail, 'reason': exc.reason}, status=exc.status)
        except Exception:
            abandon(scope, key, digest)
            # Keep the durable source reservation for reconciliation/retry.
            return Response({'detail': 'Publishing is still being confirmed. Please try again shortly.',
                             'reason': 'not_available'}, status=503)
        finish(scope, key, digest, status=200, body=body)
        return Response(body)
