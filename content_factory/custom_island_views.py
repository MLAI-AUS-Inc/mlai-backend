"""Authenticated, organisation-scoped custom island creation and removal. No research charge."""
from django.db import transaction
from django.utils import timezone
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .custom_islands import (
    custom_island_description, custom_island_pillar, custom_island_slug, validate_custom_island,
)


class CustomContentIslandView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        from organizations.models import Organization
        from .models import ContentIsland, ContentIslandOrigin, ContentIslandStatus
        from .vibe_marketing_views import _content_islands_enabled, _resolve_context_or_response

        context, error = _resolve_context_or_response(request)
        if error is not None:
            return error
        try:
            brief = validate_custom_island(request.data)
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=400)
        with transaction.atomic():
            org = Organization.objects.select_for_update().get(pk=context.organization.pk)
            if _content_islands_enabled() and not ContentIsland.objects.filter(
                organization=org, status=ContentIslandStatus.VISIBLE,
            ).exists():
                # Preserve the older cluster/strategy cards when the first custom
                # island makes the persistent graph available for this company.
                from .content_islands import seed_islands_from_bootstrap_pillars
                seed_islands_from_bootstrap_pillars(org)
            island, created = ContentIsland.objects.get_or_create(
                organization=org, slug=custom_island_slug(brief), defaults={
                    "name": brief["name"], "description": custom_island_description(brief),
                    "pillar_keyword": brief["keyword"], "origin": ContentIslandOrigin.MANUAL,
                    "status": ContentIslandStatus.VISIBLE, "promoted_at": timezone.now(),
                    "icon_key": "default", "color_key": "purple",
                },
            )
            if not created and island.removed_at is not None:
                # Creating the same theme again is an explicit request to bring it back.
                island.status, island.archived_at, island.removed_at = ContentIslandStatus.VISIBLE, None, None
                island.promoted_at = timezone.now()
                island.save(update_fields=["status", "archived_at", "removed_at", "promoted_at", "updated_at"])
        return Response({"island": custom_island_pillar(island), "created": created},
                        status=201 if created else 200)


class ContentIslandRemoveView(APIView):
    """Remove one island from the founder's map. Repeating the request is harmless."""
    permission_classes = [IsAuthenticated]

    def post(self, request, slug):
        from .island_removal import remove_island
        from .vibe_marketing_views import _resolve_context_or_response

        context, error = _resolve_context_or_response(request)
        if error is not None:
            return error
        island = remove_island(context.organization, slug)
        if island is None:
            return Response({"detail": "Island not found."}, status=404)
        return Response({"removed": island.slug}, status=200)
