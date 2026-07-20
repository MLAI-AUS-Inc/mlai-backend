from __future__ import annotations

import re

from django.conf import settings
from django.db import IntegrityError, transaction
from rest_framework import status
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle
from rest_framework.views import APIView

from core.permissions import HasAPIKey, HasRooApiKey, IsHealthHackAdmin
from core.slack_users import (
    resolve_existing_user,
    resolve_or_create_user,
    validate_slack_id,
)

from .models import Announcement, HospitalCompetitionRound
from .rounds import active_hospital_announcements
from .serializers import AnnouncementSerializer


MAX_ANNOUNCEMENT_BODY_LENGTH = 20_000
SLACK_CHANNEL_ID_PATTERN = re.compile(r"^[CGD][A-Z0-9]{8,20}$")
SLACK_MESSAGE_TS_PATTERN = re.compile(r"^\d{10,20}\.\d{1,12}$")


class HealthHackAnnouncementListCreateView(APIView):
    """Admin list plus Roo-authenticated HealthHack announcement creation."""

    throttle_classes = [AnonRateThrottle]

    def get_permissions(self):
        if self.request.method == "POST":
            return [(HasAPIKey | HasRooApiKey)()]
        return [IsHealthHackAdmin()]

    def get(self, request):
        announcements = active_hospital_announcements().select_related("author")
        return Response(
            AnnouncementSerializer(announcements, many=True).data,
            status=status.HTTP_200_OK,
        )

    def post(self, request):
        title = str(request.data.get("title") or "").strip()
        body = str(request.data.get("body") or "").strip()
        if not title or not body:
            return Response(
                {"detail": "title and body are required"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if len(title) > Announcement._meta.get_field("title").max_length:
            return Response(
                {"detail": "title must be 255 characters or fewer"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if len(body) > MAX_ANNOUNCEMENT_BODY_LENGTH:
            return Response(
                {"detail": f"body must be {MAX_ANNOUNCEMENT_BODY_LENGTH} characters or fewer"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        requester_slack_id = str(request.data.get("requester_slack_id") or "").strip()
        requester_valid, requester_error = validate_slack_id(requester_slack_id)
        if not requester_valid:
            return Response(
                {"detail": f"requester_slack_id is invalid: {requester_error}"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        requester = resolve_existing_user(requester_slack_id)
        allowed_slack_ids = set(
            getattr(settings, "HEALTHHACK_ANNOUNCEMENT_ADMIN_IDS", []) or []
        )
        requester_allowed = bool(
            requester
            and requester.is_active
            and (requester.is_superuser or requester_slack_id in allowed_slack_ids)
        )
        if not requester_allowed:
            return Response(
                {"detail": "Only authorised HealthHack organisers can create announcements."},
                status=status.HTTP_403_FORBIDDEN,
            )

        author_slack_id = str(
            request.data.get("author_slack_id")
            or request.data.get("slack_user_id")
            or ""
        ).strip()
        author = requester
        if author_slack_id:
            author_valid, author_error = validate_slack_id(author_slack_id)
            if not author_valid:
                return Response(
                    {"detail": f"author_slack_id is invalid: {author_error}"},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            author = resolve_or_create_user(author_slack_id)
            if author is None:
                return Response(
                    {"detail": "Could not resolve the announcement author."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        source_channel_id = str(request.data.get("source_channel_id") or "").strip() or None
        source_message_ts = str(request.data.get("source_message_ts") or "").strip() or None
        if bool(source_channel_id) != bool(source_message_ts):
            return Response(
                {
                    "detail": (
                        "source_channel_id and source_message_ts must be provided together"
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        if source_channel_id and not SLACK_CHANNEL_ID_PATTERN.match(source_channel_id):
            return Response(
                {"detail": "source_channel_id is invalid"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if source_message_ts and not SLACK_MESSAGE_TS_PATTERN.match(source_message_ts):
            return Response(
                {"detail": "source_message_ts is invalid"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        competition_round = HospitalCompetitionRound.get_active()
        defaults = {
            "title": title,
            "body": body,
            "author": author,
            "requester": requester,
            "round": competition_round,
        }
        if source_channel_id and source_message_ts:
            announcement, created = self._get_or_create_from_slack_source(
                source_channel_id=source_channel_id,
                source_message_ts=source_message_ts,
                competition_round=competition_round,
                defaults=defaults,
            )
            if not created and (
                announcement.title != title
                or announcement.body != body
                or announcement.requester_id != requester.id
            ):
                return Response(
                    {
                        "detail": (
                            "This Slack message is already linked to a different announcement."
                        )
                    },
                    status=status.HTTP_409_CONFLICT,
                )
        else:
            announcement = Announcement.objects.create(**defaults)
            created = True

        payload = dict(AnnouncementSerializer(announcement).data)
        payload["created"] = created
        return Response(
            payload,
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )

    @staticmethod
    def _get_or_create_from_slack_source(
        *,
        source_channel_id: str,
        source_message_ts: str,
        competition_round: HospitalCompetitionRound,
        defaults: dict,
    ):
        try:
            with transaction.atomic():
                return Announcement.objects.get_or_create(
                    round=competition_round,
                    source_channel_id=source_channel_id,
                    source_message_ts=source_message_ts,
                    defaults=defaults,
                )
        except IntegrityError:
            return (
                Announcement.objects.get(
                    round=competition_round,
                    source_channel_id=source_channel_id,
                    source_message_ts=source_message_ts,
                ),
                False,
            )
