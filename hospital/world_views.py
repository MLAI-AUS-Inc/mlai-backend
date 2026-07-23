"""Admin-only world-state endpoint for the closed HealthHack visualisation.

Serves a render-ready entity list (cubes = teams placed by rank, spheres =
recent submissions) that the health-hack frontend polls every 5 seconds.
Exposes team names and scores only — never member names or emails.
"""
import hashlib
from datetime import timedelta

from django.core.cache import cache
from django.db.models import OuterRef, Subquery
from django.utils import timezone
from rest_framework.response import Response
from rest_framework.views import APIView

from core.permissions import IsHealthHackAdmin

from .models import HospitalCompetitionRound, Submission, Team

WORLD_CACHE_KEY = "hospital_world_state"
# The build is now O(teams) not O(submissions), so freshness — not cost —
# sets this. 10s keeps the leaderboard lively while capping world rebuilds
# at worker_count/10s no matter how many players poll every ~5s.
WORLD_CACHE_SECONDS = 10
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
    permission_classes = [IsHealthHackAdmin]

    def get(self, request):
        payload = cache.get(WORLD_CACHE_KEY)
        if payload is None:
            payload = self._build()
            cache.set(WORLD_CACHE_KEY, payload, WORLD_CACHE_SECONDS)
        return Response(payload)

    def _build(self):
        # Best submission per team via a correlated subquery: ONE row per
        # team (≤100 teams), never the whole submission table. Both the
        # ranked cubes and the "not yet submitted" cubes come from this
        # single Team query — a team with no submissions surfaces as
        # best_score=None. `feedback` never leaves the database.
        #
        # Before 2026-07-13 this iterated every Submission as a full model
        # instance, dragging each row's multi-KB feedback JSON (37k rows
        # ≈ 600MB) into memory on every cache miss and melting the workers.
        # This view is polled every ~5s per connected player.
        best = (
            Submission.objects.filter(
                round__status=HospitalCompetitionRound.STATUS_ACTIVE,
                team=OuterRef("pk"),
            )
            .order_by("-score", "submitted_at")
        )
        teams = list(
            Team.objects.filter(
                round__status=HospitalCompetitionRound.STATUS_ACTIVE,
            ).annotate(
                best_score=Subquery(best.values("score")[:1]),
                best_submitted=Subquery(best.values("submitted_at")[:1]),
            )
            .order_by("team_id")
            .values("team_id", "team_name", "best_score", "best_submitted")
        )

        ranked = sorted(
            (t for t in teams if t["best_score"] is not None),
            key=lambda t: (-t["best_score"], t["best_submitted"]),
        )
        scores = [t["best_score"] for t in ranked] or [0.0]
        lo = min(scores)
        span = (max(scores) - lo) or 1.0

        entities = []
        for rank, team in enumerate(ranked, start=1):
            lat, lon = rank_to_lat_lon(rank)
            entities.append({
                "id": "team-%s" % team["team_id"],
                "kind": "cube",
                "label": "#%d %s" % (rank, team["team_name"]),
                "lat": lat,
                "lon": lon,
                "size": round(1.0 + 2.0 * (team["best_score"] - lo) / span, 2),
                "color": PALETTE[(team["team_id"] or 0) % len(PALETTE)],
                "meta": {"score": round(team["best_score"], 4), "rank": rank},
            })

        # Teams that have not submitted yet still get a spot on the planet,
        # in team_id order after the ranked ones.
        rank = len(ranked)
        for team in teams:
            if team["best_score"] is not None:
                continue
            rank += 1
            lat, lon = rank_to_lat_lon(rank)
            entities.append({
                "id": "team-%s" % team["team_id"],
                "kind": "cube",
                "label": team["team_name"],
                "lat": lat,
                "lon": lon,
                "size": 1.0,
                "color": PALETTE[(team["team_id"] or 0) % len(PALETTE)],
                "meta": {"score": None, "rank": None},
            })

        cutoff = timezone.now() - RECENT_SUBMISSION_WINDOW
        for sub in (
            Submission.objects.filter(
                round__status=HospitalCompetitionRound.STATUS_ACTIVE,
                submitted_at__gte=cutoff,
            )
            .order_by("-submitted_at")
            .values("id", "accuracy", "team__team_name")[:RECENT_SUBMISSION_LIMIT]
        ):
            digest = int(hashlib.md5(str(sub["id"]).encode()).hexdigest()[:8], 16)
            entities.append({
                "id": "sub-%s" % sub["id"],
                "kind": "sphere",
                "lat": float(digest % 120) - 60.0,
                "lon": float((digest // 120) % 360) - 180.0,
                "altitude": 8,
                "size": 0.9,
                "spin": True,
                "color": "#ffffff",
                "meta": {
                    "team": sub["team__team_name"],
                    "accuracy": round(sub["accuracy"], 4),
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
