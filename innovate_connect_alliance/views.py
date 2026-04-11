import mimetypes
import os
import re
from pathlib import Path

from django.contrib.auth import get_user_model
from django.db import IntegrityError
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.text import slugify
from rest_framework import generics, permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from core.firebase_utils import upload_file_to_storage

from .models import Announcement, Team, VideoSubmission
from .serializers import AnnouncementSerializer

User = get_user_model()

TEAM_CODE_PATTERN = re.compile(r"^TEAM(?P<team_id>\d+)$", re.IGNORECASE)
MIN_TEAM_MEMBERS = 2
MAX_TEAM_MEMBERS = 6
MAX_VIDEO_SIZE_BYTES = 250 * 1024 * 1024


def _extract_team_id_from_code(code):
    if code is None:
        return None

    match = TEAM_CODE_PATTERN.match(str(code).strip())
    if not match:
        return None

    try:
        return int(match.group("team_id"))
    except (TypeError, ValueError):
        return None


def _member_payload(member):
    return {
        "id": member.id,
        "full_name": member.full_name,
        "email": member.email,
        "avatar_url": member.avatar_url,
        "role": member.role,
        "personas": member.personas,
    }


def _team_payload(team):
    member_count = team.members.count()
    members = team.members.all()
    return {
        "id": team.id,
        "name": team.team_name,
        "team_name": team.team_name,
        "team_id": team.team_id,
        "code": f"TEAM{team.team_id}" if team.team_id is not None else None,
        "avatar_url": team.avatar_url,
        "member_count": member_count,
        "is_valid_team_size": MIN_TEAM_MEMBERS <= member_count <= MAX_TEAM_MEMBERS,
        "members": [_member_payload(member) for member in members],
    }


def _submission_payload(submission):
    return {
        "submission_id": submission.id,
        "participant_name": submission.participant_name,
        "title": submission.title,
        "notes": submission.notes,
        "video_url": submission.video_url,
        "original_filename": submission.original_filename,
        "content_type": submission.content_type,
        "file_size_bytes": submission.file_size_bytes,
        "submitted_at": submission.submitted_at.isoformat(),
        "team": {
            "team_id": submission.team.team_id,
            "team_name": submission.team.team_name,
            "code": f"TEAM{submission.team.team_id}",
            "avatar_url": submission.team.avatar_url,
        },
    }


def _resolve_video_content_type(uploaded_file):
    content_type = (uploaded_file.content_type or "").strip()
    if content_type.startswith("video/"):
        return content_type

    guessed_type, _ = mimetypes.guess_type(uploaded_file.name)
    if guessed_type and guessed_type.startswith("video/"):
        return guessed_type

    return ""


def _store_submission_video(*, user, team, video_file):
    content_type = _resolve_video_content_type(video_file)
    if not content_type:
        raise ValueError("Uploaded file must be a video.")

    if video_file.size > MAX_VIDEO_SIZE_BYTES:
        raise ValueError(
            f"Video exceeds maximum size of {MAX_VIDEO_SIZE_BYTES // (1024 * 1024)} MB."
        )

    filename = video_file.name or "submission-video"
    stem = slugify(Path(filename).stem) or "submission-video"
    extension = Path(filename).suffix.lower()
    if not extension:
        extension = mimetypes.guess_extension(content_type) or ".mp4"

    timestamp = timezone.now().strftime("%Y%m%d%H%M%S")
    storage_path = os.path.join(
        "innovate-connect-alliance",
        "videos",
        f"team-{team.team_id}",
        f"user-{user.id}",
        f"{timestamp}-{stem}{extension}",
    )

    video_url = upload_file_to_storage(
        video_file,
        storage_path,
        content_type=content_type,
    )
    return video_url, storage_path, content_type


class TeamListView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        teams = Team.objects.all().order_by("team_id").prefetch_related("members")
        member_id = request.query_params.get("member_id")
        if member_id and member_id not in ("undefined", "null"):
            try:
                member_id = int(member_id)
            except (TypeError, ValueError):
                return Response(
                    {"detail": "member_id must be an integer"},
                    status=status.HTTP_400_BAD_REQUEST,
                )
        else:
            member_id = None

        if member_id is not None:
            teams = teams.filter(members__id=member_id).distinct()
            return Response([_team_payload(team) for team in teams], status=status.HTTP_200_OK)

        return Response([_team_payload(team) for team in teams], status=status.HTTP_200_OK)

    def post(self, request):
        team_name = (request.data.get("team_name") or request.data.get("name") or "").strip()
        requested_code = request.data.get("code")
        requested_avatar_url = request.data.get("avatar_url")

        if not team_name:
            return Response({"error": "team_name is required"}, status=status.HTTP_400_BAD_REQUEST)

        existing_team = Team.objects.filter(team_name__iexact=team_name).first()
        created = False

        if existing_team:
            team = existing_team
            if requested_avatar_url and not team.avatar_url:
                team.avatar_url = requested_avatar_url
                team.save(update_fields=["avatar_url"])
        else:
            create_kwargs = {"team_name": team_name}
            requested_team_id = _extract_team_id_from_code(requested_code)
            if requested_code and requested_team_id is None:
                return Response(
                    {"error": "Invalid team code. Expected format like TEAM12."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if requested_team_id is not None:
                create_kwargs["team_id"] = requested_team_id
            if requested_avatar_url:
                create_kwargs["avatar_url"] = requested_avatar_url

            try:
                team = Team.objects.create(**create_kwargs)
                created = True
            except IntegrityError:
                return Response(
                    {"error": "Team already exists with that ID/code."},
                    status=status.HTTP_409_CONFLICT,
                )

        user = request.user
        if not team.members.filter(id=user.id).exists() and team.members.count() >= MAX_TEAM_MEMBERS:
            return Response(
                {"error": "Team is full.", "max_members": MAX_TEAM_MEMBERS},
                status=status.HTTP_409_CONFLICT,
            )

        for current_team in user.innovate_connect_alliance_teams.all():
            current_team.members.remove(user)

        team.members.add(user)
        if not user.has_team:
            user.has_team = True
            user.save(update_fields=["has_team"])

        return Response(
            {"created": created, "team": _team_payload(team)},
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )


class JoinTeamView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        team_id = request.data.get("team_id")
        code = request.data.get("code")

        if team_id is None and not code:
            return Response({"error": "team_id or code is required"}, status=status.HTTP_400_BAD_REQUEST)

        lookup_team_id = team_id
        team = None
        if lookup_team_id is None and code:
            lookup_team_id = _extract_team_id_from_code(code)
            if lookup_team_id is not None:
                team = Team.objects.filter(team_id=lookup_team_id).first()
            else:
                team = Team.objects.filter(team_name__iexact=str(code).strip()).first()

            if team is None:
                return Response({"error": "Team not found."}, status=status.HTTP_404_NOT_FOUND)
        elif lookup_team_id is not None:
            team = get_object_or_404(Team, team_id=lookup_team_id)

        user = request.user
        if not team.members.filter(id=user.id).exists() and team.members.count() >= MAX_TEAM_MEMBERS:
            return Response(
                {
                    "error": f"Team '{team.team_name}' is full.",
                    "max_members": MAX_TEAM_MEMBERS,
                },
                status=status.HTTP_409_CONFLICT,
            )

        for current_team in user.innovate_connect_alliance_teams.all():
            current_team.members.remove(user)

        team.members.add(user)
        if not user.has_team:
            user.has_team = True
            user.save(update_fields=["has_team"])

        return Response(
            {
                "message": f"Joined team {team.team_name}",
                "team_id": team.team_id,
                "team_name": team.team_name,
                "code": f"TEAM{team.team_id}",
                "member_count": team.members.count(),
                "min_members": MIN_TEAM_MEMBERS,
                "max_members": MAX_TEAM_MEMBERS,
            },
            status=status.HTTP_200_OK,
        )


class SubmissionListCreateView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        submissions = (
            VideoSubmission.objects.filter(user=request.user)
            .select_related("team")
            .order_by("-submitted_at")
        )
        return Response([_submission_payload(submission) for submission in submissions], status=status.HTTP_200_OK)

    def post(self, request):
        user = request.user
        team = user.innovate_connect_alliance_teams.first()
        if not team:
            return Response(
                {
                    "detail": "You must join an Innovate Connect Alliance team before submitting.",
                    "min_members": MIN_TEAM_MEMBERS,
                    "max_members": MAX_TEAM_MEMBERS,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        team_size = team.members.count()
        if team_size < MIN_TEAM_MEMBERS or team_size > MAX_TEAM_MEMBERS:
            return Response(
                {
                    "detail": f"Team size must be between {MIN_TEAM_MEMBERS} and {MAX_TEAM_MEMBERS} members.",
                    "team_id": team.team_id,
                    "team_name": team.team_name,
                    "member_count": team_size,
                    "min_members": MIN_TEAM_MEMBERS,
                    "max_members": MAX_TEAM_MEMBERS,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        title = (request.data.get("title") or "").strip()
        notes = (request.data.get("notes") or "").strip()
        video_file = request.FILES.get("video")

        if not title:
            return Response({"detail": "title is required"}, status=status.HTTP_400_BAD_REQUEST)

        if video_file is None:
            return Response({"detail": "video is required"}, status=status.HTTP_400_BAD_REQUEST)

        try:
            video_url, storage_path, content_type = _store_submission_video(
                user=user,
                team=team,
                video_file=video_file,
            )
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as exc:
            return Response(
                {"detail": f"Failed to upload video: {exc}"},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        submission = VideoSubmission.objects.create(
            user=user,
            team=team,
            participant_name=user.full_name or "Anonymous",
            title=title,
            notes=notes or None,
            video_url=video_url,
            storage_path=storage_path,
            original_filename=video_file.name,
            content_type=content_type,
            file_size_bytes=video_file.size,
        )
        return Response(_submission_payload(submission), status=status.HTTP_201_CREATED)


class LatestSubmissionView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        submission = (
            VideoSubmission.objects.filter(user=request.user)
            .select_related("team")
            .order_by("-submitted_at")
            .first()
        )
        if not submission:
            return Response({"detail": "No submission found."}, status=status.HTTP_404_NOT_FOUND)

        return Response(_submission_payload(submission), status=status.HTTP_200_OK)


class RecentSubmissionView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        team = request.user.innovate_connect_alliance_teams.first()
        if not team:
            return Response({"detail": "User is not part of any team."}, status=status.HTTP_400_BAD_REQUEST)

        submissions = (
            VideoSubmission.objects.filter(team=team)
            .select_related("team")
            .order_by("-submitted_at")[:5]
        )
        return Response([_submission_payload(submission) for submission in submissions], status=status.HTTP_200_OK)


class SubmissionDetailView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, submission_id):
        submission = get_object_or_404(
            VideoSubmission.objects.select_related("team"),
            id=submission_id,
            user=request.user,
        )
        return Response(_submission_payload(submission), status=status.HTTP_200_OK)


class AnnouncementListView(generics.ListAPIView):
    queryset = Announcement.objects.all()
    serializer_class = AnnouncementSerializer
    permission_classes = [permissions.IsAuthenticated]
