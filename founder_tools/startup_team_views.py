"""Authenticated Chat facade for team membership; tokens stay in request bodies."""
from rest_framework.exceptions import NotFound
from rest_framework.response import Response
from rest_framework.views import APIView

from community_chat.my_startup_views import MyStartupAccess
from . import startup_team as teams


class _TeamAccess(MyStartupAccess, APIView):
    """Reuse exact Origin/authentication, account throttles and private responses."""
    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        if not teams.enabled():
            raise NotFound('Not found.')

    def handle_exception(self, exc):
        if isinstance(exc, teams.TeamError):
            return Response({'detail': exc.detail, 'reason': exc.reason}, status=exc.status)
        return super().handle_exception(exc)


class TeamView(_TeamAccess):
    """Read owner management fields or a member's names-only team list."""
    def get(self, request):
        return Response(teams.team_payload(organization=self.company.organization, user=request.user))


class JoinRequestView(_TeamAccess):
    """Exchange a user-bound opaque reference for a status-only join request."""
    requires_company = False

    def post(self, request):
        return Response(teams.request_join(user=request.user, join_reference=request.data.get('joinReference')), status=202)


class JoinRequestDecisionView(_TeamAccess):
    """The current pinned owner explicitly approves or declines one request."""
    def post(self, request, request_id):
        return Response(teams.decide_request(organization=self.company.organization, user=request.user,
            request_id=request_id, decision=request.data.get('decision')))


class TeamMemberView(_TeamAccess):
    """Owner permissions or immediate owner removal/member self-leave."""
    def patch(self, request, member_id):
        return Response(teams.update_member(organization=self.company.organization, user=request.user,
            member_id=member_id, rights=request.data.get('rights')))

    def delete(self, request, member_id):
        return Response(teams.update_member(organization=self.company.organization, user=request.user,
            member_id=member_id, remove=True))


class InviteLinkView(_TeamAccess):
    """Return one fragment invite or revoke the previous hash."""
    def post(self, request):
        return Response(teams.invite_link(organization=self.company.organization, user=request.user,
            action=request.data.get('action')))


class InviteJoinView(_TeamAccess):
    """Join with a token posted in the body and a verified signed-in account."""
    requires_company = False

    def post(self, request):
        return Response(teams.join_invite(user=request.user, token=request.data.get('token')), status=202)
