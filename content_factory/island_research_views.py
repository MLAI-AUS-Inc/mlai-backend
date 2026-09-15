"""Authenticated paid research and free adoption of its measured results."""
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .island_research import validate_research_brief, research_request_key, proposal_for_adoption, adopt_researched_island


class ContentIslandResearchView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        from . import vibe_marketing_views as views
        context, error = views._resolve_context_or_response(request)
        if error is not None:
            return error
        try:
            brief = validate_research_brief(request.data)
            key = research_request_key(context.organization.pk, request.user.pk, request.data.get("clientRequestId"), brief)
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=400)
        # Preserve a previously queued result, including retries after a lost response.
        from workflow_runs.models import ContentFactoryRun
        existing = ContentFactoryRun.objects.filter(domain=context.organization.domain,
            run_request__client_request_id=key).first()
        if existing:
            return Response(views._run_start_payload(existing), status=202)
        config = views._get_config(context.organization)
        payload = {"domain": context.organization.domain, "client_request_id": key,
                   "slack_user_id": views.founder_actor_id_for_user(request.user),
                   "request_source": views.CONTENT_FACTORY_REQUEST_SOURCE, "island_research_brief": brief}
        charged_user, article_request, error = views._charge_roo_points_for_content_island_topic_generation(
            request, context=context, payload=payload)
        if error is not None:
            return error
        run = views._queue_content_factory_run(endpoint="island-research", workflow="island_refresh",
            context=context, config=config, payload=payload,
            billing_refund_context={"kind": views.CONTENT_FACTORY_ACTION_CONTENT_ISLAND_TOPIC_GENERATION,
                "charged_user": charged_user, "article_request": article_request,
                "reason": "Island research could not be queued."})
        return Response(views._run_start_payload(run), status=202 if run.status != "blocked" else 503)


class ContentIslandResearchAdoptView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, run_id):
        from workflow_runs.models import ContentFactoryRun
        from .custom_islands import custom_island_pillar
        from .vibe_marketing_views import _resolve_context_or_response, _run_belongs_to_context
        context, error = _resolve_context_or_response(request)
        if error is not None:
            return error
        if not isinstance(request.data, dict):
            return Response({"detail": "Choose a researched island."}, status=400)
        run = ContentFactoryRun.objects.filter(run_id=run_id).first()
        if run is None or not _run_belongs_to_context(run, context):
            return Response({"detail": "Research not found."}, status=404)
        try:
            proposal = proposal_for_adoption(run, request.data.get("proposalId"))
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=400)
        island, created = adopt_researched_island(context.organization, run, proposal)
        return Response({"island": custom_island_pillar(island), "created": created}, status=201 if created else 200)
