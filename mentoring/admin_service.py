"""Staff-only Mentoring administration: invites, applications, mentors, sessions.

Thin ORM layer used by the Mentoring admin endpoints. Who may call it is decided in
community_chat.mentoring_views; nothing here trusts a caller-supplied identity.
Every write records the acting staff member and the time when the model has a field
for it (created_by, revoked_by, reviewed_by).
"""

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Count, Q
from django.utils import timezone

from .models import (
    LOCATION_VIDEO,
    MentorInvite,
    MentorProfile,
    MentorSession,
    invite_link,
)
from .service import (
    MEETING_CLAIM_TIMEOUT_SECONDS,
    MentoringError,
    _display_name,
    _is_stale_pending,
    serialize_mentor,
)

INVITE_STATUSES = ('pending', 'used', 'expired', 'revoked')
APPLICATION_STATES = ('pending', 'rejected')
SESSION_PROBLEMS = ('meeting_failed', 'stale_pending', 'notification_failed')
LIST_LIMIT = 200
MEMBER_LIMIT = 10
MIN_SEARCH_LENGTH = 2


def invite_status(invite, now=None):
    if invite.revoked_at is not None:
        return 'revoked'
    if invite.used_at is not None:
        return 'used'
    return 'expired' if invite.expires_at <= (now or timezone.now()) else 'pending'


def _iso(value):
    return value.isoformat() if value else None


def _person(user):
    if user is None:
        return None
    return {'id': user.pk, 'name': _display_name(user), 'email': user.email}


def serialize_member(user):
    return {
        'id': user.pk,
        'name': _display_name(user),
        'email': user.email,
        'avatar_url': getattr(user, 'avatar_url', None) or None,
    }


def serialize_invite(invite, now=None):
    return {
        'id': invite.pk,
        'user': _person(invite.user),
        'status': invite_status(invite, now),
        'created_at': _iso(invite.created_at),
        'expires_at': _iso(invite.expires_at),
        'used_at': _iso(invite.used_at),
        'revoked_at': _iso(invite.revoked_at),
        'created_by': _person(invite.created_by),
        'revoked_by': _person(invite.revoked_by),
    }


def application_state(profile):
    return 'rejected' if profile.rejected_at else 'pending'


def serialize_application(profile, *, detail=False):
    data = {
        'id': profile.pk,
        'state': application_state(profile),
        'user': _person(profile.user),
        'headline': profile.headline,
        'session_minutes': profile.session_minutes,
        'price_roo': profile.price_roo,
        'location_type': profile.location_type,
        'submitted_at': _iso(profile.updated_at),
        'rejected_at': _iso(profile.rejected_at),
        'reviewed_by': _person(profile.reviewed_by),
    }
    if detail:
        data['profile'] = serialize_mentor(profile, include_private=True)
        data['profile']['availability'] = [
            {'weekday': r.weekday, 'start': r.start_time.strftime('%H:%M'), 'end': r.end_time.strftime('%H:%M')}
            for r in profile.availability_rules.all()
        ]
    return data


def serialize_admin_mentor(profile):
    return {
        'id': profile.pk,
        'user': _person(profile.user),
        'headline': profile.headline,
        'is_active': profile.is_active,
        'paused_at': _iso(profile.paused_at),
        'approved_at': _iso(profile.approved_at),
        'reviewed_by': _person(profile.reviewed_by),
        'price_roo': profile.price_roo,
        'session_minutes': profile.session_minutes,
        'location_type': profile.location_type,
        'sessions_count': getattr(profile, 'sessions_count', 0),
    }


def session_flags(session, now=None):
    flags = []
    if session.location_type == LOCATION_VIDEO and session.status in (
        MentorSession.STATUS_ACCEPTED, MentorSession.STATUS_COMPLETED,
    ):
        if session.meeting_status == MentorSession.MEETING_FAILED:
            flags.append('meeting_failed')
        elif _is_stale_pending(session, now):
            flags.append('stale_pending')
    if session.notification_status == MentorSession.NOTIFY_FAILED:
        flags.append('notification_failed')
    return flags


def serialize_admin_session(session, now=None):
    return {
        'id': str(session.pk),
        'status': session.status,
        'starts_at': _iso(session.starts_at),
        'ends_at': _iso(session.ends_at),
        'price_roo': session.price_roo,
        'location_type': session.location_type,
        'mentor': {'id': session.mentor_id, 'name': _display_name(session.mentor.user)},
        'founder': _person(session.founder),
        'meeting_status': session.meeting_status,
        'meeting_error': session.meeting_error,
        'notification_status': session.notification_status,
        'requested_at': _iso(session.requested_at),
        'responded_at': _iso(session.responded_at),
        'cancelled_at': _iso(session.cancelled_at),
        'flags': session_flags(session, now),
    }


def _applications_query():
    """Inactive profiles that came from a used invite and were never approved or paused."""
    return (
        MentorProfile.objects.select_related('user', 'reviewed_by')
        .filter(
            is_active=False, approved_at__isnull=True, paused_at__isnull=True,
            user__mentor_invites__used_at__isnull=False,
        )
        .distinct()
    )


class MentoringAdminService:
    # ---- members

    @staticmethod
    def search_members(query):
        text = (query or '').strip() if isinstance(query, str) else ''
        if len(text) < MIN_SEARCH_LENGTH:
            return []
        User = get_user_model()
        match = Q(email__icontains=text) | Q(first_name__icontains=text) | Q(last_name__icontains=text)
        # A full name is first and last name together.
        parts = text.split()
        if len(parts) > 1:
            match |= Q(first_name__icontains=parts[0], last_name__icontains=' '.join(parts[1:]))
        users = User.objects.filter(match, is_active=True).order_by('first_name', 'last_name', 'pk')[:MEMBER_LIMIT]
        return [serialize_member(u) for u in users]

    # ---- invites

    @staticmethod
    def create_invite(actor, user_id):
        if type(user_id) is not int:
            raise MentoringError('invalid_user', 'user_id must be a member id.', 400)
        User = get_user_model()
        with transaction.atomic():
            user = User.objects.filter(pk=user_id, is_active=True).first()
            if user is None:
                raise MentoringError('member_not_found', 'No such member.', 404)
            if MentorProfile.objects.filter(user=user, is_active=True).exists():
                raise MentoringError('already_a_mentor', 'That member is already an active mentor.', 409)
            invite, token = MentorInvite.issue(user, created_by=actor)
        data = serialize_invite(invite)
        # The link is returned here once; only its hash is stored.
        data['link'] = invite_link(token)
        return data

    @staticmethod
    def list_invites(status=None):
        if status and status not in INVITE_STATUSES:
            raise MentoringError('invalid_status', 'Unknown invite status.', 400)
        now = timezone.now()
        query = MentorInvite.objects.select_related('user', 'created_by', 'revoked_by')
        if status == 'revoked':
            query = query.filter(revoked_at__isnull=False)
        elif status == 'used':
            query = query.filter(revoked_at__isnull=True, used_at__isnull=False)
        elif status == 'expired':
            query = query.filter(revoked_at__isnull=True, used_at__isnull=True, expires_at__lte=now)
        elif status == 'pending':
            query = query.filter(revoked_at__isnull=True, used_at__isnull=True, expires_at__gt=now)
        return [serialize_invite(i, now) for i in query[:LIST_LIMIT]]

    @staticmethod
    def revoke_invite(actor, invite_id):
        with transaction.atomic():
            invite = (
                MentorInvite.objects.select_for_update().select_related('user', 'created_by', 'revoked_by')
                .filter(pk=invite_id).first()
            )
            if invite is None:
                raise MentoringError('invite_not_found', 'No such invite.', 404)
            if invite.revoked_at is not None:
                return serialize_invite(invite)  # revoking twice is a no-op
            if invite.used_at is not None:
                raise MentoringError('invite_used', 'That invite was already used; review the application instead.', 409)
            invite.revoked_at = timezone.now()
            invite.revoked_by = actor
            invite.save(update_fields=['revoked_at', 'revoked_by'])
        return serialize_invite(invite)

    # ---- applications

    @staticmethod
    def list_applications(state=None):
        if state and state not in APPLICATION_STATES:
            raise MentoringError('invalid_state', 'Unknown application state.', 400)
        query = _applications_query()
        if state == 'rejected':
            query = query.filter(rejected_at__isnull=False)
        elif state == 'pending':
            query = query.filter(rejected_at__isnull=True)
        return [serialize_application(p) for p in query.order_by('-updated_at', '-pk')[:LIST_LIMIT]]

    @staticmethod
    def get_application(profile_id):
        profile = _applications_query().filter(pk=profile_id).first()
        if profile is None:
            raise MentoringError('application_not_found', 'No such application.', 404)
        return serialize_application(profile, detail=True)

    @staticmethod
    def _lock_application(profile_id):
        profile = (
            MentorProfile.objects.select_for_update().select_related('user', 'reviewed_by')
            .filter(pk=profile_id).first()
        )
        if profile is None:
            raise MentoringError('application_not_found', 'No such application.', 404)
        if profile.is_active or profile.paused_at is not None or profile.approved_at is not None:
            raise MentoringError('not_an_application', 'That profile is not a pending application.', 409)
        return profile

    @classmethod
    def approve_application(cls, actor, profile_id):
        with transaction.atomic():
            profile = cls._lock_application(profile_id)
            profile.is_active = True
            profile.approved_at = timezone.now()
            profile.rejected_at = None
            profile.reviewed_by = actor
            profile.save(update_fields=['is_active', 'approved_at', 'rejected_at', 'reviewed_by', 'updated_at'])
        return serialize_application(profile)

    @classmethod
    def reject_application(cls, actor, profile_id):
        with transaction.atomic():
            profile = cls._lock_application(profile_id)
            profile.rejected_at = timezone.now()
            profile.reviewed_by = actor
            profile.save(update_fields=['rejected_at', 'reviewed_by', 'updated_at'])
        return serialize_application(profile)

    # ---- mentors

    @staticmethod
    def list_mentors():
        profiles = (
            MentorProfile.objects.select_related('user', 'reviewed_by')
            .filter(Q(is_active=True) | Q(paused_at__isnull=False) | Q(approved_at__isnull=False))
            .annotate(sessions_count=Count('sessions'))
            .order_by('user__first_name', 'user__last_name', 'pk')[:LIST_LIMIT]
        )
        return [serialize_admin_mentor(p) for p in profiles]

    @staticmethod
    def set_paused(actor, profile_id, paused):
        with transaction.atomic():
            profile = (
                MentorProfile.objects.select_for_update().select_related('user', 'reviewed_by')
                .filter(pk=profile_id).first()
            )
            if profile is None:
                raise MentoringError('mentor_not_found', 'No such mentor.', 404)
            if paused:
                if not profile.is_active:
                    raise MentoringError('not_active', 'That mentor is not active.', 409)
                profile.is_active = False
                profile.paused_at = timezone.now()
            else:
                if profile.is_active or profile.paused_at is None:
                    raise MentoringError('not_paused', 'That mentor is not paused.', 409)
                profile.is_active = True
                profile.paused_at = None
            profile.reviewed_by = actor
            profile.save(update_fields=['is_active', 'paused_at', 'reviewed_by', 'updated_at'])
        data = serialize_admin_mentor(profile)
        data['sessions_count'] = MentorSession.objects.filter(mentor=profile).count()
        return data

    # ---- sessions

    @staticmethod
    def list_sessions(status=None, problem=None):
        if status and status not in dict(MentorSession.STATUS_CHOICES):
            raise MentoringError('invalid_status', 'Unknown status.', 400)
        if problem and problem not in SESSION_PROBLEMS:
            raise MentoringError('invalid_problem', 'Unknown problem filter.', 400)
        now = timezone.now()
        query = MentorSession.objects.select_related('mentor__user', 'founder')
        if status:
            query = query.filter(status=status)
        video_booked = Q(
            location_type=LOCATION_VIDEO,
            status__in=(MentorSession.STATUS_ACCEPTED, MentorSession.STATUS_COMPLETED),
        )
        stale_before = now - timedelta(seconds=MEETING_CLAIM_TIMEOUT_SECONDS)
        if problem == 'meeting_failed':
            query = query.filter(video_booked, meeting_status=MentorSession.MEETING_FAILED)
        elif problem == 'stale_pending':
            query = query.filter(
                location_type=LOCATION_VIDEO, status=MentorSession.STATUS_ACCEPTED,
                meeting_status=MentorSession.MEETING_PENDING,
            ).filter(Q(meeting_claimed_at__isnull=True) | Q(meeting_claimed_at__lte=stale_before))
        elif problem == 'notification_failed':
            query = query.filter(notification_status=MentorSession.NOTIFY_FAILED)
        return [serialize_admin_session(s, now) for s in query.order_by('-starts_at', '-pk')[:LIST_LIMIT]]
