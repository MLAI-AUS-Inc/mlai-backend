"""Preserve funded mentoring obligations during the existing account cascade."""
from django.db import connection
from django.db.models import Q
from django.utils import timezone

from .models import MentorBookingAttempt, MentorSession, MentorTimeReservation
from .service import MentoringError, _lock_users


def prepare_account_deletion(user):
    """Prune settled dependent rows inside the caller's account-delete transaction.

    The caller must keep this transaction open through user.delete(). New
    bookings lock the same user row, so no funded session can enter between this
    inspection and the existing cascade. External operations are never invoked.
    """
    if not connection.in_atomic_block:
        raise RuntimeError('Mentoring teardown must share the account deletion transaction')
    _lock_users(user.pk)
    sessions = MentorSession.objects.filter(Q(founder=user) | Q(mentor__user=user))
    ids = list(sessions.values_list('pk', flat=True))
    funded = sessions.filter(
        Q(status__in=(MentorSession.STATUS_ACCEPTED, MentorSession.STATUS_PROVISIONING))
        | Q(charge_ledger_entry__isnull=False, release_ledger_entry__isnull=True, payout_ledger_entry__isnull=True),
    ).exists()
    unresolved = MentorBookingAttempt.objects.filter(session_id__in=ids).filter(
        Q(status__in=('provisioning', 'reconciling')) | Q(cleanup_pending=True)
        | Q(lease_until__gt=timezone.now()),
    ).exists()
    if funded or unresolved:
        raise MentoringError('mentoring_obligations_pending',
            'Resolve your paid mentoring sessions and pending calendar cleanup before deleting your account.', 409)
    # No provider or ledger obligation remains. Historic ledgers are retained by
    # their own domain; these dependency rows no longer need to prevent cascade.
    MentorTimeReservation.objects.filter(session_id__in=ids).delete()
    MentorBookingAttempt.objects.filter(session_id__in=ids).delete()
