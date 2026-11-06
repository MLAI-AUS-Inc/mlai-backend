"""Deduplicated guardrail evidence for campaign, stream and provider windows."""

import hashlib
from dataclasses import replace
from datetime import timedelta

from django.db.models import Q

from crm.models import BlastDelivery, EmailEvent

from . import policy

ATTEMPTED = ('submitting', 'sent', 'delivered', 'read', 'bounced', 'undelivered', 'unknown')


def evidence_key(values):
    """Identify negative evidence, independent of a changing denominator."""
    return hashlib.sha256('\n'.join(sorted(str(value) for value in values)).encode()).hexdigest()


def rate_limit(event):
    """Documented provider deferrals, separate from authentication/policy rejection."""
    return ((event.recipient_provider == 'google' and event.enhanced_code == '4.7.28')
        or (event.recipient_provider == 'microsoft' and event.enhanced_code == '4.7.650')
        or (event.recipient_provider == 'yahoo' and 'tss04' in event.diagnostic.lower()))


def measure(rows, events, *, now):
    """Rows are handoffs, not opens; callbacks for the same message count once."""
    from .guardian import Metrics, policy_block
    attempted = rows.filter(Q(status__in=ATTEMPTED) | Q(status='unsubscribed', error_code='') | ~Q(provider_id='')).count()
    delivered = rows.filter(Q(delivered_at__isnull=False) | Q(status__in=('delivered', 'read')) | Q(error_code='resend_complained')).count()
    delivered_ids = set(rows.filter(Q(delivered_at__isnull=False) | Q(status__in=('delivered', 'read'))).values_list('pk', flat=True))
    negative = {name: set() for name in ('hard_bounce_rate', 'complaint_rate', 'policy_block', 'deferral_rate', 'unsubscribe_rate', 'provider_rate_limit')}
    for row in rows.filter(Q(bounce_kind='hard') | Q(error_code='resend_complained') | Q(status='unsubscribed', error_code='')):
        identity = row.provider_id or 'delivery:' + str(row.pk)
        if row.bounce_kind == 'hard':
            negative['hard_bounce_rate'].add(identity)
        if row.error_code == 'resend_complained':
            negative['complaint_rate'].add(identity)
        if row.status == 'unsubscribed' and not row.error_code:
            negative['unsubscribe_rate'].add(identity)
    for event in events.filter(occurred_at__lte=now).exclude(event_type__in=('email.sent', 'email.delivered', 'email.opened', 'email.clicked')):
        identity = event.provider_message_id or 'event:' + str(event.pk)
        # Match a legacy row without a provider id to its callback identity.
        if event.delivery_id:
            identity = 'delivery:' + str(event.delivery_id)
        if event.event_type == 'email.bounced' and event.bounce_type != 'Transient':
            negative['hard_bounce_rate'].add(identity)
        if event.event_type == 'email.complained':
            negative['complaint_rate'].add(identity)
        if event.event_type in ('email.unsubscribed', 'subscriber.unsubscribed'):
            negative['unsubscribe_rate'].add(identity)
        if event.event_type == 'email.delivery_delayed':
            # A final delivery callback clears a transient delay for the canary gate.
            if not event.delivery_id or event.delivery_id not in delivered_ids:
                negative['deferral_rate'].add(identity)
        if rate_limit(event):
            negative['provider_rate_limit'].add(identity)
        if (event.occurred_at >= now - timedelta(hours=policy.value('CRM_GUARD_POLICY_WINDOW_HOURS'))
                and policy_block(event)):
            negative['policy_block'].add(identity)
    # Row and event ids must share the same canonical identity when both exist.
    for name, criterion in (('hard_bounce_rate', Q(bounce_kind='hard')), ('complaint_rate', Q(error_code='resend_complained'))):
        for row in rows.filter(criterion).only('pk', 'provider_id'):
            if 'delivery:' + str(row.pk) in negative[name]:
                negative[name].discard(row.provider_id)
    return Metrics(attempted, max(delivered, len(negative['complaint_rate'])),
        len(negative['hard_bounce_rate']), len(negative['complaint_rate']), len(negative['policy_block']),
        len(negative['unsubscribe_rate']), len(negative['deferral_rate']), len(negative['provider_rate_limit']),
        {name: evidence_key(values) for name, values in negative.items()})


def blast_metrics(blast, *, now, wave=None):
    rows = blast.deliveries.filter(channel='email').exclude(address__iendswith='@resend.dev')
    events = EmailEvent.objects.filter(blast=blast).exclude(recipient__iendswith='@resend.dev')
    if wave:
        rows, events = rows.filter(wave=wave), events.filter(wave=wave)
    return measure(rows, events, now=now)


def stream_metrics(stream, *, now, hours, provider=''):
    since = now - timedelta(hours=hours)
    rows = BlastDelivery.objects.filter(blast__stream=stream, channel='email').exclude(address__iendswith='@resend.dev')
    rows = rows.filter(Q(wave__started_at__gte=since, wave__started_at__lte=now)
        | Q(wave__isnull=True, blast__queued_at__gte=since, blast__queued_at__lte=now))
    # Only callbacks from handoffs within this scope contribute to its denominator.
    events = EmailEvent.objects.filter(delivery__in=rows)
    if provider:
        rows, events = rows.filter(provider=provider), events.filter(recipient_provider=provider)
    result = measure(rows, events, now=now)
    # Policy/rate-limit codes are absolute event counts. Late callbacks still
    # matter when their original handoff has aged out of the rate denominator.
    from .guardian import policy_block
    recent = EmailEvent.objects.filter(stream=stream, occurred_at__gte=now - timedelta(hours=1),
        occurred_at__lte=now).exclude(recipient__iendswith='@resend.dev')
    if provider:
        recent = recent.filter(recipient_provider=provider)
    recent = list(recent.exclude(event_type__in=('email.sent', 'email.delivered', 'email.opened', 'email.clicked')))
    blocked = {row.provider_message_id or str(row.pk) for row in recent if policy_block(row)}
    limited = {row.provider_message_id or str(row.pk) for row in recent if rate_limit(row)}
    return replace(result, policy_blocks=len(blocked), rate_limited=len(limited),
        evidence={**result.evidence, 'policy_block': evidence_key(blocked), 'provider_rate_limit': evidence_key(limited)})


def scoped(signals, *, scope, provider='', metrics):
    return [replace(signal, scope=scope, provider=provider,
        evidence_key=metrics.evidence.get(signal.key, '')) for signal in signals]


def extra_signals(metrics, *, stream=None, blast=False, provider=False, canary=False):
    from .guardian import Signal
    p = lambda key: policy.value(key, stream)
    result = []
    if blast and metrics.attempted >= p('CRM_GUARD_UNSUBSCRIBE_MIN_SAMPLE'):
        rate = metrics.unsubscribed / metrics.attempted
        if rate >= p('CRM_GUARD_UNSUBSCRIBE_AMBER'):
            red = rate >= p('CRM_GUARD_UNSUBSCRIBE_RED')
            result.append(Signal('unsubscribe_rate', 'red' if red else 'amber', rate,
                p('CRM_GUARD_UNSUBSCRIBE_RED' if red else 'CRM_GUARD_UNSUBSCRIBE_AMBER'), metrics.attempted,
                'pause_blast' if red else 'slow_blast'))
    if provider and metrics.rate_limited >= p('CRM_GUARD_PROVIDER_RATE_LIMIT_COUNT'):
        result.append(Signal('provider_rate_limit', 'amber', metrics.rate_limited,
            p('CRM_GUARD_PROVIDER_RATE_LIMIT_COUNT'), metrics.rate_limited, 'hold_provider'))
    elif (provider and metrics.attempted >= p('CRM_GUARD_DEFERRAL_MIN_SAMPLE')) or (canary and metrics.attempted):
        rate = metrics.deferred / metrics.attempted
        minimum = p('CRM_CANARY_MAX_DEFERRAL_RATE') if canary else p('CRM_GUARD_DEFERRAL_AMBER')
        if rate >= minimum:
            hold = rate >= p('CRM_GUARD_DEFERRAL_RED')
            result.append(Signal('deferral_rate', 'amber', rate, minimum, metrics.attempted,
                ('hold_provider' if hold else 'throttle_provider') if provider else 'slow_stream'))
    return result
