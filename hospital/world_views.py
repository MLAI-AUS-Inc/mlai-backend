"""Public world-state endpoint for the health-hack 3D visualisation.

Serves a render-ready entity list (cubes = teams placed by rank, spheres =
recent submissions) that the health-hack frontend polls every 5 seconds.
Exposes team names and scores only — never member names or emails.
"""
import hashlib
from datetime import timedelta

from django.core.cache import cache
from django.utils import timezone
from rest_framework import permissions
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Submission, Team

WORLD_CACHE_KEY = "hospital_world_state"
WORLD_CACHE_SECONDS = 3
WORLD_RADIUS = 30
RECENT_SUBMISSION_WINDOW = timedelta(minutes=15)
RECENT_SUBMISSION_LIMIT = 20

PALETTE = [
    "#ff6b6b", "#4ecdc4", "#ffd93d", "#6c5ce7", "#ff8fab",
    "#00b894", "#fd79a8", "#74b9ff", "#e17055", "#a29bfe",
]


def rank_to_lat_lon(rank):
    """Rank 1 near the north pole, golden-angle spiral downwards.

    Mirrored by the frontend mock in health-hack lib/net/mock.ts.
    """
    lat = max(80.0 - (rank - 1) * 12.0, -60.0)
    lon = (((rank - 1) * 137.5) % 360.0) - 180.0
    return lat, lon


class WorldStateView(APIView):
    permission_classes = [permissions.AllowAny]
    authentication_classes = []

    def get(self, request):
        payload = cache.get(WORLD_CACHE_KEY)
        if payload is None:
            payload = self._build()
            cache.set(WORLD_CACHE_KEY, payload, WORLD_CACHE_SECONDS)
        return Response(payload)

    def _build(self):
        best_by_team = {}
        for sub in (
            Submission.objects.select_related("team")
            .filter(team__isnull=False)
            .order_by("-score", "submitted_at")
        ):
            best_by_team.setdefault(sub.team_id, sub)

        ranked = sorted(
            best_by_team.values(), key=lambda s: (-s.score, s.submitted_at)
        )
        scores = [s.score for s in ranked] or [0.0]
        lo = min(scores)
        span = (max(scores) - lo) or 1.0

        entities = []
        for rank, sub in enumerate(ranked, start=1):
            lat, lon = rank_to_lat_lon(rank)
            entities.append({
                "id": "team-%s" % sub.team.team_id,
                "kind": "cube",
                "label": "#%d %s" % (rank, sub.team.team_name),
                "lat": lat,
                "lon": lon,
                "size": round(1.0 + 2.0 * (sub.score - lo) / span, 2),
                "color": PALETTE[(sub.team.team_id or 0) % len(PALETTE)],
                "meta": {"score": round(sub.score, 4), "rank": rank},
            })

        # Teams that have not submitted yet still get a spot on the planet.
        rank = len(ranked)
        for team in (
            Team.objects.exclude(pk__in=best_by_team.keys()).order_by("team_id")
        ):
            rank += 1
            lat, lon = rank_to_lat_lon(rank)
            entities.append({
                "id": "team-%s" % team.team_id,
                "kind": "cube",
                "label": team.team_name,
                "lat": lat,
                "lon": lon,
                "size": 1.0,
                "color": PALETTE[(team.team_id or 0) % len(PALETTE)],
                "meta": {"score": None, "rank": None},
            })

        cutoff = timezone.now() - RECENT_SUBMISSION_WINDOW
        for sub in (
            Submission.objects.filter(submitted_at__gte=cutoff)
            .select_related("team")
            .order_by("-submitted_at")[:RECENT_SUBMISSION_LIMIT]
        ):
            digest = int(hashlib.md5(str(sub.id).encode()).hexdigest()[:8], 16)
            entities.append({
                "id": "sub-%s" % sub.id,
                "kind": "sphere",
                "lat": float(digest % 120) - 60.0,
                "lon": float((digest // 120) % 360) - 180.0,
                "altitude": 8,
                "size": 0.9,
                "spin": True,
                "color": "#ffffff",
                "meta": {
                    "team": sub.team.team_name if sub.team else None,
                    "accuracy": round(sub.accuracy, 4),
                },
            })

        # Stamped at build time (not latest submission) so additions AND
        # removals — e.g. a sphere ageing out of the 15-minute window —
        # always change updated_at. Within the cache window clients see an
        # identical payload and skip reconciling.
        return {
            "updated_at": timezone.now().isoformat(),
            "world": {"radius": WORLD_RADIUS},
            "entities": entities,
        }
