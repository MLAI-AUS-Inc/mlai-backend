"""Vibe Mentoring endpoints for the authenticated MLAI Chat member.

Thin wrappers over mentoring.service.MentoringService. Identity comes only from
the session. Errors are 4xx JSON with a stable `code` and a `detail`, as in
CoworkingTodayView. The whole surface returns 404 while the mentor_booking
feature flag is off.
"""

from datetime import date

from django.conf import settings
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from mentoring.admin_service import MentoringAdminService
from mentoring.models import MentorSession
from mentoring.service import MentoringError, MentoringService, serialize_session

from .authentication import CommunityChatAccountAuthentication
from .permissions import account_chat_role, is_chat_admin
from .throttles import CommunityChatScopedThrottle


def mentor_booking_enabled():
    return bool(getattr(settings, 'MENTOR_BOOKING_ENABLED', False))


class MentoringBaseView(APIView):
    authentication_classes = (CommunityChatAccountAuthentication,)
    permission_classes = (IsAuthenticated,)
    throttle_classes = (CommunityChatScopedThrottle,)

    @property
    def community_chat_throttle_scope(self):
        if self.request.method in ('GET', 'HEAD', 'OPTIONS'):
            return 'community_chat_mentoring'
        return 'community_chat_mentoring_write'

    def initial(self, request, *args, **kwargs):
        # Flag first: while off, every caller (even anonymous) gets 404 and no
        # authentication or throttle work is done.
        if not mentor_booking_enabled():
            raise MentoringDisabled()
        super().initial(request, *args, **kwargs)

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        response['Cache-Control'] = 'private, no-store'
        return response

    def handle_exception(self, exc):
        if isinstance(exc, MentoringDisabled):
            return Response({'code': 'mentor_booking_disabled', 'detail': 'Mentor booking is not available.'}, status=404)
        if isinstance(exc, MentoringError):
            return Response({'code': exc.code, 'detail': exc.message}, status=exc.status_code)
        return super().handle_exception(exc)


class MentoringDisabled(Exception):
    pass


def _body(request):
    if not isinstance(request.data, dict):
        raise MentoringError('invalid_payload', 'A JSON object is required.', 400)
    return request.data


def _parse_day(value, name):
    if value in (None, ''):
        return None
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        raise MentoringError('invalid_range', f'{name} must be YYYY-MM-DD.', 400)


class MentorListView(MentoringBaseView):
    def get(self, request):
        return Response({'mentors': MentoringService.list_mentors(request.user)})


class MentorAvailabilityView(MentoringBaseView):
    def get(self, request, mentor_id):
        first = _parse_day(request.query_params.get('from'), 'from')
        last = _parse_day(request.query_params.get('to'), 'to')
        return Response(MentoringService.availability(mentor_id, first, last))


class MyMentorProfileView(MentoringBaseView):
    def get(self, request):
        return Response(MentoringService.get_profile(request.user))

    def put(self, request):
        return Response(MentoringService.update_profile(request.user, _body(request)))

    patch = put


class MentorApplicationCheckView(MentoringBaseView):
    """POST because the invite token must not travel in a URL or query string."""

    def post(self, request):
        return Response(MentoringService.check_invite(request.user, _body(request).get('token')))


class MentorApplicationView(MentoringBaseView):
    def post(self, request):
        return Response(MentoringService.submit_application(request.user, _body(request)), status=201)


class MentorSessionsView(MentoringBaseView):
    def get(self, request):
        role = request.query_params.get('role', 'founder')
        if role not in ('founder', 'mentor'):
            raise MentoringError('invalid_role', 'role must be founder or mentor.', 400)
        status = request.query_params.get('status') or None
        if status and status not in dict(MentorSession.STATUS_CHOICES):
            raise MentoringError('invalid_status', 'Unknown status.', 400)
        return Response({'sessions': MentoringService.list_sessions(request.user, role, status)})

    def post(self, request):
        data = _body(request)
        session, created = MentoringService.request_session(
            founder=request.user,
            mentor_id=data.get('mentor_id'),
            starts_at=data.get('starts_at'),
            brief=data.get('brief'),
            client_request_id=data.get('client_request_id'),
            expected_price=data.get('expected_price'),
        )
        return Response(serialize_session(session, request.user), status=201 if created else 200)


class MentorSessionActionView(MentoringBaseView):
    action = None

    def post(self, request, session_id):
        session = getattr(MentoringService, self.action)(session_id, request.user)
        return Response(serialize_session(session, request.user))


class MentorSessionAcceptView(MentorSessionActionView):
    action = 'accept'


class MentorSessionDeclineView(MentorSessionActionView):
    action = 'decline'


class MentorSessionCancelView(MentorSessionActionView):
    action = 'cancel'


class MentorSessionCompleteView(MentorSessionActionView):
    action = 'complete'


class MentorSessionMeetingRetryView(MentorSessionActionView):
    action = 'retry_meeting'


# ----------------------------------------------------------- staff-only admin

def is_mentoring_admin(request):
    """Staff by the rules the backend already uses: a Chat owner or admin on this
    verified device, or an active Django staff or superuser account."""
    if getattr(settings, 'STAFF_ADMIN_PANEL_ENABLED', False):
        from .staff_admin_permissions import viewer_role, sections_for
        level = sections_for(viewer_role(request)).get('mentoring')
        return level == 'edit' or (level == 'view' and request.method in ('GET', 'HEAD', 'OPTIONS'))
    user = request.user
    if not getattr(user, 'is_authenticated', False) or not getattr(user, 'is_active', False):
        return False
    if getattr(user, 'is_staff', False) or getattr(user, 'is_superuser', False):
        return True
    role = account_chat_role(
        user,
        getattr(request, 'community_chat_public_key', None),
        getattr(request, 'community_chat_installation_id', None),
    )
    return is_chat_admin(role)


class MentoringAdminView(MentoringBaseView):
    """Every method needs staff; anyone else gets 403 after the flag and auth checks."""

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        if not is_mentoring_admin(request):
            raise MentoringError('mentoring_admin_required', 'Mentoring admin is for MLAI staff.', 403)


class MentoringAdminAccessView(MentoringBaseView):
    """Tells the client whether to show the Mentoring admin tab. Never an error for members."""

    def get(self, request):
        return Response({'is_admin': is_mentoring_admin(request)})


class MentoringAdminMembersView(MentoringAdminView):
    def get(self, request):
        return Response({'members': MentoringAdminService.search_members(request.query_params.get('q', ''))})


class MentoringAdminInvitesView(MentoringAdminView):
    def get(self, request):
        status = request.query_params.get('status') or None
        return Response({'invites': MentoringAdminService.list_invites(status)})

    def post(self, request):
        return Response(MentoringAdminService.create_invite(request.user, _body(request).get('user_id')), status=201)


class MentoringAdminInviteRevokeView(MentoringAdminView):
    def post(self, request, invite_id):
        return Response(MentoringAdminService.revoke_invite(request.user, invite_id))


class MentoringAdminApplicationsView(MentoringAdminView):
    def get(self, request):
        state = request.query_params.get('state') or None
        return Response({'applications': MentoringAdminService.list_applications(state)})


class MentoringAdminApplicationView(MentoringAdminView):
    def get(self, request, profile_id):
        return Response(MentoringAdminService.get_application(profile_id))


class MentoringAdminApplicationApproveView(MentoringAdminView):
    def post(self, request, profile_id):
        return Response(MentoringAdminService.approve_application(request.user, profile_id))


class MentoringAdminApplicationRejectView(MentoringAdminView):
    def post(self, request, profile_id):
        return Response(MentoringAdminService.reject_application(request.user, profile_id))


class MentoringAdminMentorsView(MentoringAdminView):
    def get(self, request):
        return Response({'mentors': MentoringAdminService.list_mentors()})


class MentoringAdminMentorPauseView(MentoringAdminView):
    paused = True

    def post(self, request, profile_id):
        return Response(MentoringAdminService.set_paused(request.user, profile_id, self.paused))


class MentoringAdminMentorUnpauseView(MentoringAdminMentorPauseView):
    paused = False


class MentoringAdminSessionsView(MentoringAdminView):
    def get(self, request):
        return Response({'sessions': MentoringAdminService.list_sessions(
            request.query_params.get('status') or None,
            request.query_params.get('problem') or None,
        )})
