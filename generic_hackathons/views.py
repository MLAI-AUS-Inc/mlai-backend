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
    GenericHackathonJoinRequest,
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
GENERIC_HACKATHON_MIN_TEAM_MEMBERS = 2
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
    # The creator of a team (its first member) becomes its leader; this also re-seeds a leader for
    # a team that was left leaderless (e.g. after a disband).
    if team.leader_id is None:
        team.leader = user
        team.save(update_fields=['leader'])


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
        serializer = GenericHackathonTeamSerializer(teams, many=True, context={'request': request})
        return Response(serializer.data, status=status.HTTP_200_OK)

    def post(self, request, slug):
        hackathon = _get_hackathon(slug)
        team_name = (request.data.get('team_name') or request.data.get('name') or '').strip()
        avatar_url = request.data.get('avatar_url') or None

        if not team_name:
            return Response({'error': 'team_name is required'}, status=status.HTTP_400_BAD_REQUEST)

        existing = GenericHackathonTeam.objects.filter(
            hackathon=hackathon,
            team_name__iexact=team_name,
        ).first()
        if existing is not None:
            # You can only "create" a team you already belong to (idempotent save). Otherwise the
            # name is taken and you must REQUEST to join -- creating must not be an instant-join
            # backdoor around the approval flow.
            if existing.members.filter(id=request.user.id).exists():
                if avatar_url and not existing.avatar_url:
                    existing.avatar_url = avatar_url
                    existing.save(update_fields=['avatar_url'])
                return Response(
                    {'created': False, 'team': GenericHackathonTeamSerializer(existing, context={'request': request}).data},
                    status=status.HTTP_200_OK,
                )
            return Response(
                {'error': f'A team named "{existing.team_name}" already exists. Search for it to request to join.'},
                status=status.HTTP_409_CONFLICT,
            )

        if _current_team(request.user, hackathon) is not None:
            return Response(
                {'error': 'Leave your current team before creating a new one.'},
                status=status.HTTP_409_CONFLICT,
            )

        try:
            team = GenericHackathonTeam.objects.create(
                hackathon=hackathon,
                team_name=team_name,
                avatar_url=avatar_url,
            )
        except IntegrityError:
            return Response({'error': 'Team already exists.'}, status=status.HTTP_409_CONFLICT)

        _switch_user_to_team(request.user, hackathon, team)
        return Response(
            {'created': True, 'team': GenericHackathonTeamSerializer(team, context={'request': request}).data},
            status=status.HTTP_201_CREATED,
        )


class GenericHackathonCurrentTeamView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, slug):
        hackathon = _get_hackathon(slug)
        team = _current_team(request.user, hackathon)
        if team is None:
            return Response(None, status=status.HTTP_200_OK)
        return Response(GenericHackathonTeamSerializer(team, context={'request': request}).data, status=status.HTTP_200_OK)


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

        if team.members.filter(id=request.user.id).exists():
            return Response({'error': "You're already on this team."}, status=status.HTTP_400_BAD_REQUEST)

        # Must leave first: no requesting elsewhere while active on a team (prevents stream-hopping).
        if _current_team(request.user, hackathon) is not None:
            return Response(
                {'error': 'Leave your current team before requesting to join another.'},
                status=status.HTTP_409_CONFLICT,
            )

        if team.members.count() >= GENERIC_HACKATHON_MAX_TEAM_MEMBERS:
            return Response(
                {'error': 'Team is full.', 'max_members': GENERIC_HACKATHON_MAX_TEAM_MEMBERS},
                status=status.HTTP_409_CONFLICT,
            )

        join_request, created = GenericHackathonJoinRequest.objects.get_or_create(team=team, user=request.user)
        return Response(
            {
                'pending': True,
                'request_id': join_request.id,
                'team_id': team.team_id,
                'team_name': team.team_name,
            },
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )


class GenericHackathonLeaveTeamView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, slug):
        hackathon = _get_hackathon(slug)
        team = _current_team(request.user, hackathon)
        if team is None:
            return Response({'error': 'You are not on a team.'}, status=status.HTTP_400_BAD_REQUEST)

        member_count = team.members.count()
        # Per design: the leader must hand off (or disband) before leaving a populated team.
        if team.leader_id == request.user.id and member_count > 1:
            return Response(
                {'error': 'Transfer leadership to a teammate before leaving, or disband the team.'},
                status=status.HTTP_409_CONFLICT,
            )
        # Min-2 guard: don't let a leave drop the team below the playable size.
        if member_count <= GENERIC_HACKATHON_MIN_TEAM_MEMBERS:
            return Response(
                {
                    'error': (
                        f'Your team needs at least {GENERIC_HACKATHON_MIN_TEAM_MEMBERS} members — '
                        'leaving would drop it below that. Ask the leader to disband instead.'
                    ),
                    'min_members': GENERIC_HACKATHON_MIN_TEAM_MEMBERS,
                },
                status=status.HTTP_409_CONFLICT,
            )

        team.members.remove(request.user)
        return Response({'left': True}, status=status.HTTP_200_OK)


class GenericHackathonTransferLeadView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, slug):
        hackathon = _get_hackathon(slug)
        team = _current_team(request.user, hackathon)
        if team is None:
            return Response({'error': 'You are not on a team.'}, status=status.HTTP_400_BAD_REQUEST)
        if team.leader_id != request.user.id:
            return Response(
                {'error': 'Only the team leader can transfer leadership.'},
                status=status.HTTP_403_FORBIDDEN,
            )

        try:
            new_leader_id = int(request.data.get('member_id') or request.data.get('user_id') or 0)
        except (TypeError, ValueError):
            new_leader_id = 0
        if not new_leader_id:
            return Response({'error': 'member_id is required.'}, status=status.HTTP_400_BAD_REQUEST)
        if new_leader_id == request.user.id:
            return Response({'error': 'You are already the leader.'}, status=status.HTTP_400_BAD_REQUEST)

        new_leader = team.members.filter(id=new_leader_id).first()
        if new_leader is None:
            return Response({'error': 'That user is not on your team.'}, status=status.HTTP_400_BAD_REQUEST)

        team.leader = new_leader
        team.save(update_fields=['leader'])
        return Response(GenericHackathonTeamSerializer(team, context={'request': request}).data, status=status.HTTP_200_OK)


class GenericHackathonDisbandTeamView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, slug):
        hackathon = _get_hackathon(slug)
        team = _current_team(request.user, hackathon)
        if team is None:
            return Response({'error': 'You are not on a team.'}, status=status.HTTP_400_BAD_REQUEST)
        if team.leader_id != request.user.id:
            return Response(
                {'error': 'Only the team leader can disband the team.'},
                status=status.HTTP_403_FORBIDDEN,
            )

        # Empty the team (frees every member) but keep the record so FK'd submissions survive.
        team.members.clear()
        team.leader = None
        team.save(update_fields=['leader'])
        return Response({'disbanded': True}, status=status.HTTP_200_OK)


def _serialize_join_request(req):
    user = req.user
    return {
        'id': req.id,
        'user': {
            'id': user.id,
            'full_name': user.full_name,
            'email': user.email,
            'avatar_url': user.avatar_url,
        },
        'team_id': req.team.team_id,
        'team_name': req.team.team_name,
        'created_at': req.created_at.isoformat(),
    }


class GenericHackathonJoinRequestsView(APIView):
    """`incoming` = requests to the team you lead; `outgoing` = your own pending requests."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, slug):
        hackathon = _get_hackathon(slug)
        led_team = GenericHackathonTeam.objects.filter(hackathon=hackathon, leader=request.user).first()
        incoming = []
        if led_team is not None:
            incoming = [
                _serialize_join_request(req)
                for req in led_team.join_requests.select_related('user', 'team').all()
            ]
        outgoing = [
            _serialize_join_request(req)
            for req in (
                GenericHackathonJoinRequest.objects
                .filter(user=request.user, team__hackathon=hackathon)
                .select_related('user', 'team')
            )
        ]
        return Response({'incoming': incoming, 'outgoing': outgoing}, status=status.HTTP_200_OK)


class GenericHackathonAcceptRequestView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, slug, request_id):
        hackathon = _get_hackathon(slug)
        join_request = (
            GenericHackathonJoinRequest.objects
            .filter(id=request_id, team__hackathon=hackathon)
            .select_related('team', 'user')
            .first()
        )
        if join_request is None:
            return Response({'error': 'Request not found.'}, status=status.HTTP_404_NOT_FOUND)

        team = join_request.team
        if team.leader_id != request.user.id:
            return Response(
                {'error': 'Only the team leader can accept join requests.'},
                status=status.HTTP_403_FORBIDDEN,
            )

        applicant = join_request.user
        if team.members.filter(id=applicant.id).exists():
            join_request.delete()
            return Response(GenericHackathonTeamSerializer(team, context={'request': request}).data, status=status.HTTP_200_OK)
        if _current_team(applicant, hackathon) is not None:
            join_request.delete()
            return Response(
                {'error': 'That person already joined another team.'},
                status=status.HTTP_409_CONFLICT,
            )
        if team.members.count() >= GENERIC_HACKATHON_MAX_TEAM_MEMBERS:
            return Response(
                {'error': 'Team is full.', 'max_members': GENERIC_HACKATHON_MAX_TEAM_MEMBERS},
                status=status.HTTP_409_CONFLICT,
            )

        team.members.add(applicant)
        # The applicant now has a team -- clear every pending request they have in this hackathon.
        GenericHackathonJoinRequest.objects.filter(user=applicant, team__hackathon=hackathon).delete()
        return Response(GenericHackathonTeamSerializer(team, context={'request': request}).data, status=status.HTTP_200_OK)


class GenericHackathonRejectRequestView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, slug, request_id):
        hackathon = _get_hackathon(slug)
        join_request = (
            GenericHackathonJoinRequest.objects
            .filter(id=request_id, team__hackathon=hackathon)
            .select_related('team')
            .first()
        )
        if join_request is None:
            return Response({'error': 'Request not found.'}, status=status.HTTP_404_NOT_FOUND)
        if join_request.team.leader_id != request.user.id:
            return Response(
                {'error': 'Only the team leader can reject join requests.'},
                status=status.HTTP_403_FORBIDDEN,
            )
        join_request.delete()
        return Response({'rejected': True}, status=status.HTTP_200_OK)


class GenericHackathonCancelRequestView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, slug, request_id):
        hackathon = _get_hackathon(slug)
        deleted, _ = (
            GenericHackathonJoinRequest.objects
            .filter(id=request_id, team__hackathon=hackathon, user=request.user)
            .delete()
        )
        if not deleted:
            return Response({'error': 'Request not found.'}, status=status.HTTP_404_NOT_FOUND)
        return Response({'cancelled': True}, status=status.HTTP_200_OK)


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
