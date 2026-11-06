"""Alert-only system-mail health, independent of all marketing admission flags."""

import hashlib
import math
from collections import defaultdict
from datetime import timedelta
from email.utils import parseaddr

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from crm.models import CrmAuditEvent, EmailEvent, SendingStream
from integrations import system_mail

from . import observations, policy


def _evidence(values):
    return hashlib.sha256(','.join(sorted(str(value) for value in values)).encode()).hexdigest()


def _measure(stream, now):
    from .guardian import Signal
    limit = policy.value('CRM_TRANSACTIONAL_MAX_ROWS')
    cutoff = now - timedelta(minutes=policy.value('CRM_TRANSACTIONAL_WINDOW_MINUTES'))
    rows = list(CrmAuditEvent.objects.filter(action='send', target_type='transactional_mail', detail__phase='result',
        created_at__gte=cutoff, created_at__lte=now).order_by('-pk')[:limit + 1])
    if len(rows) > limit:
        return {'status': 'unknown', 'reason': 'observation_limit'}, []
    groups = defaultdict(list)
    for row in rows:
        data = row.detail
        started = parse_datetime(data.get('started_at', ''))
        if (data.get('provider') in ('resend', 'customerio') and data.get('kind') in ('signin', 'notification')
                and type(data.get('ok')) is bool and started and timezone.is_aware(started) and cutoff <= started <= now):
            groups[(data['provider'], data['kind'])].append(row)
    signals, outcomes = [], []
    for (provider, kind), attempts in sorted(groups.items()):
        failed = sum(row.detail['ok'] is False for row in attempts)
        rate = failed / len(attempts)
        outcomes.append({'provider': provider, 'kind': kind, 'attempted': len(attempts), 'failed': failed, 'failure_rate': rate})
        if len(attempts) >= policy.value('CRM_TRANSACTIONAL_MIN_SAMPLE') and rate > policy.value('CRM_TRANSACTIONAL_FAILURE_RED'):
            signals.append(Signal('transactional_failure_rate', 'red', rate, policy.value('CRM_TRANSACTIONAL_FAILURE_RED'),
                len(attempts), 'alert_only', _evidence(row.pk for row in attempts if not row.detail['ok']), scope=provider + ':' + kind))
    # API acceptance is never substituted for provider delivery. Customer.io has
    # no authenticated delivery callback in this application, so its latency is unknown.
    events = list(EmailEvent.objects.filter(stream=stream, source='resend', blast__isnull=True,
        occurred_at__gte=now - timedelta(minutes=policy.value('CRM_TRANSACTIONAL_EVENT_LOOKBACK_MINUTES')),
        occurred_at__lte=now, event_type__in=('email.sent', 'email.delivered', 'email.bounced'))
        .order_by('-occurred_at').values('pk', 'provider_message_id', 'event_type', 'occurred_at', 'bounce_type')[:limit + 1])
    if len(events) > limit:
        return {'status': 'unknown', 'reason': 'feedback_limit', 'outcomes': outcomes}, signals
    messages = defaultdict(dict)
    for event in events:
        message = messages[event['provider_message_id']]
        kind = event['event_type']
        if kind not in message or event['occurred_at'] < message[kind]['occurred_at']:
            message[kind] = event
    sent = [value for key, value in messages.items() if key and 'email.sent' in value]
    hard = [value['email.bounced']['pk'] for value in sent if 'email.bounced' in value and value['email.bounced']['bounce_type'] != 'Transient']
    if len(sent) >= policy.value('CRM_GUARD_HARD_BOUNCE_MIN_SAMPLE'):
        rate = len(hard) / len(sent)
        if rate >= policy.value('CRM_GUARD_HARD_BOUNCE_AMBER'):
            red = rate >= policy.value('CRM_GUARD_HARD_BOUNCE_RED')
            threshold = policy.value('CRM_GUARD_HARD_BOUNCE_RED' if red else 'CRM_GUARD_HARD_BOUNCE_AMBER')
            signals.append(Signal('transactional_hard_bounce_rate', 'red' if red else 'amber', rate, threshold,
                len(sent), 'alert_only', _evidence(hard), scope='resend'))
    latencies = sorted((row['email.delivered']['occurred_at'] - row['email.sent']['occurred_at']).total_seconds()
        for row in sent if 'email.delivered' in row and row['email.delivered']['occurred_at'] >= row['email.sent']['occurred_at'])
    p95 = latencies[math.ceil(0.95 * len(latencies)) - 1] if len(latencies) >= policy.value('CRM_TRANSACTIONAL_MIN_SAMPLE') else None
    previous = observations.latest(stream, 'transactional')
    since = None
    if p95 is not None and p95 > policy.value('CRM_TRANSACTIONAL_LATENCY_SECONDS'):
        checked = parse_datetime(previous.get('checked_at', ''))
        since = parse_datetime(previous.get('latency_since') or '') if checked and timezone.is_aware(checked) and now - timedelta(seconds=policy.value('CRM_TRANSACTIONAL_INTERVAL_SECONDS') * 3) <= checked <= now else None
        since = since if since and timezone.is_aware(since) and since <= now else now
        if now - since >= timedelta(minutes=policy.value('CRM_TRANSACTIONAL_LATENCY_HOLD_MINUTES')):
            signals.append(Signal('transactional_delivery_latency', 'amber', p95, policy.value('CRM_TRANSACTIONAL_LATENCY_SECONDS'),
                len(latencies), 'alert_only', _evidence(row['pk'] for row in events if row['event_type'] == 'email.delivered'), scope='resend'))
    system_domain = parseaddr(system_mail.sender())[1].rpartition('@')[2].lower()
    cio_domain = parseaddr(str(getattr(settings, 'CUSTOMERIO_FROM_EMAIL', '') or ''))[1].rpartition('@')[2].lower()
    return {'status': 'attention' if signals else 'observed' if outcomes or events else 'no_data', 'outcomes': outcomes,
        'resend_sent': len(sent), 'resend_hard_bounced': len(hard), 'resend_latency_sample': len(latencies),
        'resend_delivery_p95_seconds': p95, 'customerio_delivery_status': 'unknown',
        'latency_since': since.isoformat() if since else None,
        'sender_matches_notify': system_domain == stream.domain,
        'customerio_sender_matches_notify': cio_domain == stream.domain,
        'customerio_dkim': 'operator_verified' if policy.value('CRM_CUSTOMERIO_NOTIFY_DKIM_VERIFIED') else 'unknown'}, signals


def run_transactional_monitor(*, now=None):
    """Never pause, cap, throttle or reroute system mail, even during a red incident."""
    if not policy.value('CRM_TRANSACTIONAL_MONITOR_ENABLED'):
        return {'status': 'disabled'}
    now = now or timezone.now()
    try:
        if not cache.add('crm:transactional:monitor', 1, timeout=policy.value('CRM_TRANSACTIONAL_INTERVAL_SECONDS')):
            return {'status': 'throttled'}
        stream = SendingStream.objects.filter(key='notify', transactional=True).first()
        if stream is None:
            return {'status': 'not_configured'}
        result, signals = _measure(stream, now)
        from .guardian import detect
        for signal in signals:
            detect(None, signal, now=now, stream=stream)
        observations.record(stream, 'transactional', result, now=now)
        if signals:
            from .alerts import alert_open_incidents
            alert_open_incidents(now=now)
        return {'status': 'completed', 'signals': len(signals)}
    except Exception as exc:
        import logging
        logging.getLogger(__name__).warning('System email monitor unavailable error_type=%s', type(exc).__name__)
        return {'status': 'deferred'}
