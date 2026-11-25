"""Deliver committed Startup Team invitations with bounded retry and leasing."""
from datetime import timedelta

from django.db import connection, transaction
from django.db.models import Q
from django.utils import timezone

from system_email import api
from system_email.render import SystemEmailContextError
from system_email.transport_resend import SystemEmailError

from . import startup_email_invitations as invites, startup_team as team
from .models import StartupTeamInvitation as Invitation, StartupTeamInviteDelivery as Delivery
from .startup_invite_email import MESSAGE_KEY

LEASE = timedelta(minutes=2)
RETRY_WINDOW = timedelta(hours=23)
MAX_ATTEMPTS = 6


def _candidate(now):
    return Delivery.objects.filter(
        Q(state=Delivery.State.QUEUED, available_at__lte=now)
        | Q(state=Delivery.State.SENDING, leased_at__lte=now - LEASE),
    ).order_by("available_at", "pk")


@transaction.atomic
def claim(now):
    """Claim a due or abandoned delivery without holding locks during transport."""
    rows = _candidate(now)
    if connection.features.has_select_for_update_skip_locked:
        rows = rows.select_for_update(skip_locked=True)
    row = rows.first()
    if row:
        row.state = Delivery.State.SENDING
        row.leased_at = now
        row.attempts += 1
        row.save(update_fields=["state", "leased_at", "attempts"])
    return row


def _finish(row, state, *, available_at=None, retryable=False):
    changes = {"state": state, "leased_at": None, "retryable": retryable}
    if available_at:
        changes["available_at"] = available_at
    else:
        changes["context"] = {}
    Delivery.objects.filter(pk=row.pk, leased_at=row.leased_at).update(**changes)


def _current_invitation(row, now):
    invitation = Invitation.objects.select_related("company__organization").filter(pk=row.invitation_id).first()
    if not invitation or not invitation.company.organization_id:
        return None
    organization = invitation.company.organization
    with team._locked(organization) as (_, roster):
        current = Invitation.objects.select_related("company__organization", "inviter").filter(
            pk=invitation.pk, company__organization=organization, public_id=row.public_id, accepted_at__isnull=True,
        ).first()
        if current is None:
            return None
        try:
            invites._eligible(current, current.company, roster, now)
        except team.TeamError:
            return None
        return current


def deliver(row, now):
    """Recheck the invite before sending the same exact generation to Resend."""
    invitation = _current_invitation(row, now)
    if not invitation:
        _finish(row, Delivery.State.CANCELLED)
        return
    if now >= row.created_at + RETRY_WINDOW:
        _finish(row, Delivery.State.FAILED, retryable=True)
        return
    try:
        api.send(MESSAGE_KEY, to=invitation.email, context=row.context,
                 idempotency_key=invites.delivery_key(row.pk))
    except SystemEmailError as error:
        if error.retryable and row.attempts < MAX_ATTEMPTS:
            retry_at = now + timedelta(minutes=2 ** row.attempts)
            _finish(row, Delivery.State.QUEUED, available_at=retry_at, retryable=True)
        else:
            _finish(row, Delivery.State.FAILED, retryable=error.retryable)
    except SystemEmailContextError:
        _finish(row, Delivery.State.FAILED)
    else:
        _finish(row, Delivery.State.SENT)


def process_due(limit=25):
    """Drain a bounded batch on one worker tick."""
    if not invites.sending_enabled():
        return 0
    count = 0
    for _ in range(limit):
        now = timezone.now()
        row = claim(now)
        if row is None:
            break
        deliver(row, now)
        count += 1
    return count
