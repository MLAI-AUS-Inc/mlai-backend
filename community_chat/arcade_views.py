"""Authenticated MLAI Arcade round reads, pulls and dismissal receipts."""

from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import serializers
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from . import arcade, level_up
from .authentication import CommunityChatAccountAuthentication
from .throttles import CommunityChatScopedThrottle


class ArcadeView(APIView):
    authentication_classes = (CommunityChatAccountAuthentication,)
    permission_classes = (IsAuthenticated,)
    throttle_classes = (CommunityChatScopedThrottle,)
    community_chat_throttle_scope = "community_chat_home"

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        response["Cache-Control"] = "private, no-store"
        return response


class ArcadeRoundsView(ArcadeView):
    def get(self, request):
        rows = arcade.rounds_for(request.user).filter(status__in=("ready", "revealed")).order_by("created_at", "id")
        trigger = request.query_params.get("trigger")
        if trigger:
            if trigger not in ("pulse", "level"):
                return Response({"detail": "Unknown round trigger."}, status=400)
            rows = rows.filter(source__trigger=trigger)
        return Response({"rounds": [arcade.round_payload(row) for row in rows[:50]]})


class ArcadeSpinView(ArcadeView):
    def post(self, request, round_id):
        return Response(arcade.spin(request.user, round_id))


class ArcadeAcknowledgeView(ArcadeView):
    def post(self, request, round_id):
        rows = arcade.rounds_for(request.user).filter(pk=round_id)
        if not rows.filter(status="revealed").update(status="acknowledged", updated_at=timezone.now()):
            if get_object_or_404(rows).status != "acknowledged":
                return Response({"detail": "Reveal this prize before dismissing it."}, status=409)
        return Response(status=204)


class LevelUpView(ArcadeView):
    """Read the confirmed ladder position, celebration, and all pending plays."""

    def get(self, request):
        return Response(level_up.summary(request.user))


class CelebrationInput(serializers.Serializer):
    round_ids = serializers.ListField(
        child=serializers.UUIDField(), min_length=1, max_length=200,
    )


class LevelUpAcknowledgeView(ArcadeView):
    """Mark the selected crossings seen independently of their Arcade results."""

    def post(self, request):
        data = CelebrationInput(data=request.data)
        data.is_valid(raise_exception=True)
        level_up.acknowledge(request.user, data.validated_data["round_ids"])
        return Response(status=204)
