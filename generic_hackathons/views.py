import os
from uuid import uuid4

from django.db import IntegrityError
from django.shortcuts import get_object_or_404
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from core.models import Hackathon
from .models import (
    GenericHackathonAnnouncement,
    GenericHackathonResource,
    GenericHackathonSubmission,
    GenericHackathonTeam,
)
from .serializers import (
    GenericHackathonAnnouncementSerializer,
    GenericHackathonResourceSerializer,
    GenericHackathonSubmissionSerializer,
    GenericHackathonTeamSerializer,
)


GENERIC_HACKATHON_MAX_TEAM_MEMBERS = 6
MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024
ALLOWED_ATTACHMENT_CONTENT_TYPES = {
    'application/pdf',
    'application/zip',
    'application/x-zip-compressed',
    'application/vnd.ms-powerpoint',
    'application/vnd.openxmlformats-officedocument.presentationml.presentation',
    'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    'application/msword',
    'text/plain',
    'text/csv',
    'image/jpeg',
    'image/png',
    'image/webp',
    'video/mp4',
}


def _get_hackathon(slug):
    return get_object_or_404(Hackathon, slug=slug)


def _team_code_to_id(code):
    if not code:
        return None
    code = str(code).strip()
    if code.upper().startswith('TEAM'):
        try:
            return int(code[4:])
        except (TypeError, ValueError):
            return None
    return None


def _current_team(user, hackathon):
    return (
        GenericHackathonTeam.objects
        .filter(hackathon=hackathon, members=user)
        .prefetch_related('members')
        .first()
    )


def _switch_user_to_team(user, hackathon, team):
    for existing_team in GenericHackathonTeam.objects.filter(hackathon=hackathon, members=user):
        if existing_team.id != team.id:
            existing_team.members.remove(user)
    team.members.add(user)


def _validate_attachment(file_obj):
    if file_obj.size > MAX_ATTACHMENT_BYTES:
        return f"Attachment must be {MAX_ATTACHMENT_BYTES // (1024 * 1024)}MB or smaller."

    content_type = getattr(file_obj, 'content_type', '') or ''
    if content_type and content_type not in ALLOWED_ATTACHMENT_CONTENT_TYPES:
        return "Attachment type is not supported."

    return None


def _upload_attachment(file_obj, hackathon_slug, user_id):
    from core.firebase_utils import upload_file_to_storage

    _, ext = os.path.splitext(file_obj.name or '')
    safe_ext = ext.lower()[:20]
    destination = f"generic-hackathons/{hackathon_slug}/submissions/{user_id}/{uuid4()}{safe_ext}"
    return upload_file_to_storage(
        file_obj,
        destination,
        content_type=getattr(file_obj, 'content_type', None),
    )


class GenericHackathonTeamListCreateView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, slug):
        hackathon = _get_hackathon(slug)
        teams = (
            GenericHackathonTeam.objects
            .filter(hackathon=hackathon)
            .prefetch_related('members')
            .order_by('team_id')
        )
        serializer = GenericHackathonTeamSerializer(teams, many=True)
        return Response(serializer.data, status=status.HTTP_200_OK)

    def post(self, request, slug):
        hackathon = _get_hackathon(slug)
        team_name = (request.data.get('team_name') or request.data.get('name') or '').strip()
        avatar_url = request.data.get('avatar_url') or None

        if not team_name:
            return Response({'error': 'team_name is required'}, status=status.HTTP_400_BAD_REQUEST)

        team = GenericHackathonTeam.objects.filter(
            hackathon=hackathon,
            team_name__iexact=team_name,
        ).first()
        created = False
        if team is None:
            try:
                team = GenericHackathonTeam.objects.create(
                    hackathon=hackathon,
                    team_name=team_name,
                    avatar_url=avatar_url,
                )
                created = True
            except IntegrityError:
                return Response({'error': 'Team already exists.'}, status=status.HTTP_409_CONFLICT)
        elif avatar_url and not team.avatar_url:
            team.avatar_url = avatar_url
            team.save(update_fields=['avatar_url'])

        if not team.members.filter(id=request.user.id).exists() and team.members.count() >= GENERIC_HACKATHON_MAX_TEAM_MEMBERS:
            return Response(
                {'error': 'Team is full.', 'max_members': GENERIC_HACKATHON_MAX_TEAM_MEMBERS},
                status=status.HTTP_409_CONFLICT,
            )

        _switch_user_to_team(request.user, hackathon, team)

        return Response(
            {'created': created, 'team': GenericHackathonTeamSerializer(team).data},
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )


class GenericHackathonCurrentTeamView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, slug):
        hackathon = _get_hackathon(slug)
        team = _current_team(request.user, hackathon)
        if team is None:
            return Response(None, status=status.HTTP_200_OK)
        return Response(GenericHackathonTeamSerializer(team).data, status=status.HTTP_200_OK)


class GenericHackathonJoinTeamView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, slug):
        hackathon = _get_hackathon(slug)
        team_id = request.data.get('team_id')
        code = request.data.get('code')
        team = None

        if team_id is not None:
            team = GenericHackathonTeam.objects.filter(hackathon=hackathon, team_id=team_id).first()
        elif code:
            code_team_id = _team_code_to_id(code)
            if code_team_id is not None:
                team = GenericHackathonTeam.objects.filter(hackathon=hackathon, team_id=code_team_id).first()
            if team is None:
                team = GenericHackathonTeam.objects.filter(
                    hackathon=hackathon,
                    team_name__iexact=str(code).strip(),
                ).first()
        else:
            return Response({'error': 'team_id or code is required'}, status=status.HTTP_400_BAD_REQUEST)

        if team is None:
            return Response({'error': 'Team not found.'}, status=status.HTTP_404_NOT_FOUND)

        if not team.members.filter(id=request.user.id).exists() and team.members.count() >= GENERIC_HACKATHON_MAX_TEAM_MEMBERS:
            return Response(
                {'error': 'Team is full.', 'max_members': GENERIC_HACKATHON_MAX_TEAM_MEMBERS},
                status=status.HTTP_409_CONFLICT,
            )

        _switch_user_to_team(request.user, hackathon, team)
        return Response(GenericHackathonTeamSerializer(team).data, status=status.HTTP_200_OK)


class GenericHackathonSubmissionListCreateView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, slug):
        hackathon = _get_hackathon(slug)
        team = _current_team(request.user, hackathon)
        if team is None:
            return Response([], status=status.HTTP_200_OK)
        submissions = (
            GenericHackathonSubmission.objects
            .filter(hackathon=hackathon, team=team)
            .select_related('team', 'user')
            .prefetch_related('team__members')
        )
        return Response(
            GenericHackathonSubmissionSerializer(submissions, many=True).data,
            status=status.HTTP_200_OK,
        )

    def post(self, request, slug):
        hackathon = _get_hackathon(slug)
        team = _current_team(request.user, hackathon)
        if team is None:
            return Response(
                {'error': 'You must join or create a team before submitting.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        title = (request.data.get('title') or '').strip()
        summary = (request.data.get('summary') or '').strip()
        if not title:
            return Response({'error': 'title is required'}, status=status.HTTP_400_BAD_REQUEST)
        if not summary:
            return Response({'error': 'summary is required'}, status=status.HTTP_400_BAD_REQUEST)

        attachment_file = request.FILES.get('attachment')
        attachment_data = {}
        if attachment_file:
            validation_error = _validate_attachment(attachment_file)
            if validation_error:
                return Response({'error': validation_error}, status=status.HTTP_400_BAD_REQUEST)
            attachment_data = {
                'attachment_url': _upload_attachment(attachment_file, hackathon.slug, request.user.id),
                'attachment_name': attachment_file.name or '',
                'attachment_content_type': getattr(attachment_file, 'content_type', '') or '',
                'attachment_size': attachment_file.size,
            }

        submission = GenericHackathonSubmission.objects.create(
            hackathon=hackathon,
            team=team,
            user=request.user,
            title=title,
            summary=summary,
            repository_url=request.data.get('repository_url') or None,
            demo_url=request.data.get('demo_url') or None,
            slides_url=request.data.get('slides_url') or None,
            **attachment_data,
        )

        return Response(
            GenericHackathonSubmissionSerializer(submission).data,
            status=status.HTTP_201_CREATED,
        )


class GenericHackathonSubmissionDetailView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, slug, submission_id):
        hackathon = _get_hackathon(slug)
        team = _current_team(request.user, hackathon)
        if team is None:
            return Response({'error': 'Team not found.'}, status=status.HTTP_404_NOT_FOUND)
        submission = get_object_or_404(
            GenericHackathonSubmission.objects.select_related('team', 'user').prefetch_related('team__members'),
            id=submission_id,
            hackathon=hackathon,
            team=team,
        )
        return Response(GenericHackathonSubmissionSerializer(submission).data, status=status.HTTP_200_OK)


class GenericHackathonAnnouncementListView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, slug):
        hackathon = _get_hackathon(slug)
        announcements = (
            GenericHackathonAnnouncement.objects
            .filter(hackathon=hackathon)
            .select_related('author')
        )
        return Response(
            GenericHackathonAnnouncementSerializer(announcements, many=True).data,
            status=status.HTTP_200_OK,
        )


class GenericHackathonResourceListView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, slug):
        hackathon = _get_hackathon(slug)
        resources = GenericHackathonResource.objects.filter(hackathon=hackathon)
        return Response(
            GenericHackathonResourceSerializer(resources, many=True).data,
            status=status.HTTP_200_OK,
        )
