"""Conservative sender and shared account budgets during deliverability recovery."""

from datetime import timedelta

from django.db import connection, transaction
from django.db.models import Q, Sum
from django.utils.crypto import salted_hmac

from crm.models import BlastWave, CrmAuditEvent

from . import policy

ACCOUNT_LOCK_NAMESPACE = 1629564447


def enforced():
    return policy.value('CRM_DELIVERABILITY_RECOVERY_MODE') == 'enforce'


def constrained(mailbox):
    return (enforced() and mailbox is not None and mailbox.route == 'resend'
        and mailbox.email.lower() in policy.value('CRM_RECOVERY_SENDERS'))


def lock_account():
    """Call before stream/recipient locks so competing send paths reserve once."""
    if enforced() and connection.vendor == 'postgresql':
        with connection.cursor() as cursor:
            cursor.execute('SELECT pg_advisory_xact_lock(%s, %s)', [ACCOUNT_LOCK_NAMESPACE, 1])


def _attempts(now, period, *, mailbox=None, excluding_wave=None, include_probes=True):
    cutoff = now - period
    waves = BlastWave.objects.filter(
        Q(started_at__gt=cutoff) | Q(started_at__isnull=True, status='loading', reserved_at__gt=cutoff),
        blast__mailbox__route='resend')
    extras = CrmAuditEvent.objects.filter(action='send', target_type='crm_email_attempt', created_at__gt=cutoff)
    if mailbox:
        waves = waves.filter(blast__mailbox=mailbox)
        extras = extras.filter(detail__mailbox=str(mailbox.pk))
    if excluding_wave:
        waves = waves.exclude(pk=excluding_wave)
    if not include_probes:
        extras = extras.exclude(detail__kind='seed')
    reserved = waves.aggregate(total=Sum('size'))['total'] or 0
    return reserved + sum(extras.values_list('detail__attempts', flat=True))


def remaining(mailbox, now, *, excluding_wave=None, kind='campaign'):
    """Return a reservation allowance. Controlled probes still consume daily quota."""
    if not enforced() or mailbox is None or mailbox.route != 'resend':
        return None
    day = timedelta(days=1)
    used = _attempts(now, day, excluding_wave=excluding_wave)
    from system_email.models import SystemEmail
    recent_system_attempts = SystemEmail.objects.filter(provider='resend', updated_at__gt=now - day
        ).aggregate(total=Sum('attempts'))['total'] or 0
    protected = max(int(policy.value('CRM_RECOVERY_ACCOUNT_ESSENTIAL_RESERVE')), recent_system_attempts)
    shared_budget = int(policy.value('CRM_RECOVERY_ACCOUNT_DAILY_LIMIT')) - protected
    sender_budget = mailbox.daily_limit
    if constrained(mailbox):
        sender_budget = min(sender_budget, policy.value('CRM_RECOVERY_ROLLING_DAY'))
    sender_used = _attempts(now, day, mailbox=mailbox, excluding_wave=excluding_wave)
    limits = [shared_budget - used, sender_budget - sender_used]
    if constrained(mailbox) and kind != 'seed':
        used_this_hour = _attempts(now, timedelta(hours=1), mailbox=mailbox, excluding_wave=excluding_wave, include_probes=False)
        limits.append(policy.value('CRM_RECOVERY_ROLLING_HOUR') - used_this_hour)
    return max(0, min(limits))


def send_fingerprint(sender, recipient, subject):
    return salted_hmac('crm-preview-match', '\0'.join((sender.lower(), recipient.lower(), subject)),
        algorithm='sha256').hexdigest()


def record_test_attempt(mailbox, now, *, kind, count, fingerprints=()):
    """Charge the attempt before the provider call, even if its response is lost."""
    if mailbox is None or mailbox.route != 'resend':
        return True
    with transaction.atomic():
        lock_account()
        available = remaining(mailbox, now, kind=kind)
        if available is not None and available < count:
            return False
        CrmAuditEvent.objects.create(action='send', target_type='crm_email_attempt', target_id=str(mailbox.pk),
            detail={'mailbox': str(mailbox.pk), 'kind': kind, 'attempts': count, 'fingerprints': list(fingerprints)})
    return True
