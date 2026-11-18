"""Staff-only Mentoring administration: invites, applications, mentors, sessions.

Thin ORM layer used by the Mentoring admin endpoints. Who may call it is decided in
community_chat.mentoring_views; nothing here trusts a caller-supplied identity.
Every write records the acting staff member and the time when the model has a field
for it (created_by, revoked_by, reviewed_by).
"""

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import transaction
from django.db.models import Count, Q
from django.utils import timezone

from . import invite_email
from .models import (
    INVITE_TTL_DAYS,
    LOCATION_VIDEO,
    PLACEHOLDER_EMAIL_SUFFIX,
    MentorInvite,
    MentorProfile,
    MentorSession,
    invite_link,
    is_placeholder_email,
)
from .service import (
    MEETING_CLAIM_TIMEOUT_SECONDS,
    MentoringError,
    _display_name,
    _is_stale_pending,
    _lock_keys,
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


def serialize_member(user, *, is_mentor=False, pending_invite=None):
    return {
        'id': user.pk,
        'name': _display_name(user),
        'email': user.email,
        'avatar_url': getattr(user, 'avatar_url', None) or None,
        'is_mentor': bool(is_mentor),
        'pending_invite': (
            {'id': pending_invite.pk, 'expires_at': _iso(pending_invite.expires_at)} if pending_invite else None
        ),
    }


EMAIL_STATUSES = ('sent', 'delivered', 'delayed', 'bounced', 'complained', 'failed', 'suppressed')


def _email_statuses(invites):
    """``{log id: SystemEmail status}`` for the invites' latest emails, in one query."""
    ids = [i.email_log_id for i in invites if getattr(i, 'email_log_id', None)]
    if not ids:
        return {}
    from system_email.models import SystemEmail
    return {row['id']: row['status'] for row in SystemEmail.objects.filter(pk__in=ids).values('id', 'status')}


def _invite_email_status(invite, statuses):
    log_id = getattr(invite, 'email_log_id', None)
    if not log_id:
        return 'none'
    status = statuses.get(log_id, 'sent')
    # "sending" means the provider accepted nothing yet; show it as a failed send.
    return status if status in EMAIL_STATUSES else 'failed'


def _invite_person(invite):
    if invite.user is not None:
        return _person(invite.user)
    return {'id': None, 'name': invite.email, 'email': invite.email}


def serialize_invite(invite, now=None, statuses=None):
    return {
        'id': invite.pk,
        'user': _invite_person(invite),
        'invited_email': getattr(invite, 'email', '') or (invite.user.email if invite.user is not None else ''),
        'has_account': invite.user is not None,
        'status': invite_status(invite, now),
        'created_at': _iso(invite.created_at),
        'expires_at': _iso(invite.expires_at),
        'used_at': _iso(invite.used_at),
        'revoked_at': _iso(invite.revoked_at),
        'created_by': _person(invite.created_by),
        'revoked_by': _person(invite.revoked_by),
        'emailed_at': _iso(getattr(invite, 'emailed_at', None)),
        'send_count': getattr(invite, 'send_count', 0) or 0,
        'email_status': _invite_email_status(invite, statuses or {}),
    }


def _normalise_email(value):
    """A lower-case, valid, non-placeholder address, or None."""
    if not isinstance(value, str):
        return None
    # The same canonical form core.User uses for every sign-in.
    address = value.strip().lower()
    if not address or len(address) > 320 or is_placeholder_email(address):
        return None
    try:
        validate_email(address)
    except ValidationError:
        return None
    return address


def _open_invites_for(user=None, email=''):
    """Unused, unrevoked invites for this person, whether made for the account or the address."""
    match = Q()
    if user is not None:
        match |= Q(user=user)
        if user.email:
            match |= Q(user__isnull=True, email__iexact=user.email)
    if email:
        match |= Q(email__iexact=email)
    return MentorInvite.objects.filter(match, used_at__isnull=True, revoked_at__isnull=True)


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
        """Matching members, without Slack placeholder rows (they can never sign in).

        ``email_candidate`` is the typed address when it is a valid email that no
        returned member owns, so staff can invite someone without an account.
        """
        text = (query or '').strip() if isinstance(query, str) else ''
        if len(text) < MIN_SEARCH_LENGTH:
            return {'members': [], 'email_candidate': None}
        User = get_user_model()
        match = Q(email__icontains=text) | Q(first_name__icontains=text) | Q(last_name__icontains=text)
        # A full name is first and last name together.
        parts = text.split()
        if len(parts) > 1:
            match |= Q(first_name__icontains=parts[0], last_name__icontains=' '.join(parts[1:]))
        users = list(
            User.objects.filter(match, is_active=True)
            .exclude(email__iendswith=PLACEHOLDER_EMAIL_SUFFIX).exclude(email='')
            .order_by('first_name', 'last_name', 'pk')[:MEMBER_LIMIT]
        )
        ids = [u.pk for u in users]
        mentors = set(MentorProfile.objects.filter(user_id__in=ids, is_active=True).values_list('user_id', flat=True))
        pending = {}
        for invite in MentorInvite.objects.filter(
            user_id__in=ids, used_at__isnull=True, revoked_at__isnull=True, expires_at__gt=timezone.now(),
        ).order_by('-created_at'):
            pending.setdefault(invite.user_id, invite)
        members = [serialize_member(u, is_mentor=u.pk in mentors, pending_invite=pending.get(u.pk)) for u in users]
        candidate = _normalise_email(text) if '@' in text else None
        if candidate:
            owner = User.objects.resolve_email(candidate)
            if owner is not None and owner.is_active:
                if owner.pk not in ids:
                    members.insert(0, serialize_member(
                        owner, is_mentor=MentorProfile.objects.filter(user=owner, is_active=True).exists(),
                        pending_invite=_open_invites_for(owner).filter(expires_at__gt=timezone.now()).first(),
                    ))
                candidate = None
            elif any(m['email'].lower() == candidate for m in members):
                candidate = None
        return {'members': members[:MEMBER_LIMIT], 'email_candidate': candidate}

    # ---- invites

    @staticmethod
    def _target(user_id=None, email=None):
        """(user or None, address) for an invite, or a MentoringError."""
        User = get_user_model()
        if user_id is not None:
            if type(user_id) is not int:
                raise MentoringError('invalid_user', 'user_id must be a member id.', 400)
            user = User.objects.filter(pk=user_id, is_active=True).first()
            if user is None:
                raise MentoringError('member_not_found', 'No such member.', 404)
            if not user.email or is_placeholder_email(user.email):
                raise MentoringError(
                    'no_real_email',
                    'That account has no real email address. Invite them by their email address instead.', 409,
                )
            address = user.email.strip().lower()
        elif email is not None:
            address = _normalise_email(email)
            if address is None:
                raise MentoringError('invalid_email', 'Enter a real email address.', 400)
            user = User.objects.resolve_email(address)
            if user is not None and not user.is_active:
                raise MentoringError('member_not_found', 'That account is not active.', 404)
        else:
            raise MentoringError('invalid_user', 'Choose a member or enter an email address.', 400)
        if user is not None and MentorProfile.objects.filter(user=user, is_active=True).exists():
            raise MentoringError('already_a_mentor', 'That member is already an active mentor.', 409)
        return user, address

    @staticmethod
    def _note(value):
        note = invite_email.clean_note(value)
        if note is None:
            raise MentoringError('invalid_note', f'Keep the note to {invite_email.NOTE_MAX_LENGTH} characters of plain text.', 400)
        return note

    @staticmethod
    def _deliver(invite, token, actor, note, *, send_email):
        """Email the invite, or return the link when not emailing or when the email fails."""
        data = serialize_invite(invite)
        if not send_email:
            # The link is returned here once; only its hash is stored.
            data['link'] = invite_link(token)
            data['email'] = {'status': 'not_sent'}
            return data
        result = invite_email.send(invite, token=token, inviter=actor, note=note)
        if result['status'] == 'sent':
            invite.emailed_at = timezone.now()
            invite.email_log_id = result['log_id']
            invite.send_count = (invite.send_count or 0) + 1
            invite.save(update_fields=['emailed_at', 'email_log_id', 'send_count'])
            data = serialize_invite(invite, statuses={invite.email_log_id: 'sent'})
            data['email'] = {'status': 'sent', 'to': result['to']}
            return data
        data['email'] = {'status': result['status'], 'to': result['to'], 'code': result.get('code', '')}
        data['link'] = invite_link(token)  # the email failed: staff can still copy the link
        return data

    @classmethod
    def create_invite(cls, actor, user_id=None, email=None, send_email=False, note=None):
        """Issue an invite for a member or an email address, then email it when asked.

        A new invite revokes the person's earlier unused ones, so only one link is
        ever live. ``send_email`` is ignored (a link is returned) while the invite
        email is off.
        """
        clean = cls._note(note)
        send_email = bool(send_email) and invite_email.available()
        with transaction.atomic():
            user, address = cls._target(user_id=user_id, email=email)
            now = timezone.now()
            _lock_keys(f'mentor-invite:{address}')
            if send_email:
                invite_email.reserve_rate_limit(address, actor, now=now)
            _open_invites_for(user, address).update(revoked_at=now, revoked_by=actor)
            invite, token = MentorInvite.issue(user, created_by=actor, email=address)
        return cls._deliver(invite, token, actor, clean, send_email=send_email)

    @classmethod
    def preview_invite(cls, actor, user_id=None, email=None, note=None):
        """The invite email as it would be sent now. Creates nothing."""
        if not invite_email.available():
            raise MentoringError('invite_email_off', 'Invite emails are switched off.', 409)
        clean = cls._note(note)
        user, address = cls._target(user_id=user_id, email=email)
        return invite_email.preview(
            invited_user=user, invited_email=address, inviter=actor,
            expires_at=timezone.now() + timedelta(days=INVITE_TTL_DAYS), note=clean,
        )

    @classmethod
    def resend_invite(cls, actor, invite_id, note=None):
        """A new link for a waiting or expired invite, emailed again. The old link stops working."""
        if not invite_email.available():
            raise MentoringError('invite_email_off', 'Invite emails are switched off.', 409)
        clean = cls._note(note)
        with transaction.atomic():
            invite = (
                MentorInvite.objects.select_for_update(of=('self',))
                .select_related('user', 'created_by', 'revoked_by').filter(pk=invite_id).first()
            )
            if invite is None:
                raise MentoringError('invite_not_found', 'No such invite.', 404)
            if invite.used_at is not None:
                raise MentoringError('invite_used', 'That invite was already used; review the application instead.', 409)
            if invite.revoked_at is not None:
                raise MentoringError('invite_revoked', 'That invite was revoked. Create a new one instead.', 409)
            if invite.user is not None and MentorProfile.objects.filter(user=invite.user, is_active=True).exists():
                raise MentoringError('already_a_mentor', 'That member is already an active mentor.', 409)
            invite_email.reserve_rate_limit(invite.email or getattr(invite.user, 'email', ''), actor, now=timezone.now())
            token = invite.rotate()
            invite.save(update_fields=['token_hash', 'expires_at'])
        return cls._deliver(invite, token, actor, clean, send_email=True)

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
        invites = list(query[:LIST_LIMIT])
        statuses = _email_statuses(invites)
        return [serialize_invite(i, now, statuses) for i in invites]

    @staticmethod
    def revoke_invite(actor, invite_id):
        # Nullable audit joins cannot be locked on PostgreSQL; lock the invite only.
        with transaction.atomic():
            invite = (
                MentorInvite.objects.select_for_update(of=('self',)).select_related('user', 'created_by', 'revoked_by')
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
        # Keep the profile locked without locking its nullable reviewer join.
        profile = (
            MentorProfile.objects.select_for_update(of=('self',)).select_related('user', 'reviewed_by')
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
        # Keep the profile locked without locking its nullable reviewer join.
        with transaction.atomic():
            profile = (
                MentorProfile.objects.select_for_update(of=('self',)).select_related('user', 'reviewed_by')
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
