"""Window evaluation and amber recovery, independent of provider delivery effects."""

from dataclasses import replace
from datetime import timedelta

from django.db import transaction
from django.db.models import Q
from django.utils.dateparse import parse_datetime

from crm.models import Blast, BlastDelivery, CrmAuditEvent, DeliverabilityIncident, EmailEvent, SendingStream

from . import observations, policy, scopes


def run_scopes(*, now):
    from .guardian import LIVE, Signal, detect, enforce, evaluate
    firing, detected = set(), 0

    def accept(stream, blast, metrics, signals, scope='', wave=None, provider=''):
        nonlocal detected
        for signal in scopes.scoped(signals, scope=scope, provider=provider, metrics=metrics):
            incident = detect(blast, signal, now=now, wave=wave, stream=stream)
            if incident:
                firing.add(incident.fingerprint)
                enforce(incident, now=now)
                detected += 1

    for blast in Blast.objects.filter(stream__transactional=False).filter(
            Q(status__in=LIVE) | Q(sent_at__gte=now - timedelta(days=policy.value('CRM_GUARD_LOOKBACK_DAYS')))).select_related('stream'):
        for wave in [None, *blast.waves.filter(started_at__isnull=False).filter(
                Q(status__in=('running', 'paused')) | Q(finished_at__gte=now - timedelta(days=1)))]:
            canary = bool(wave and wave.kind == 'canary' and not wave.stats.get('legacy_adopted'))
            metrics = scopes.blast_metrics(blast, now=now, wave=wave)
            signals = evaluate(metrics, canary=canary, stream=blast.stream,
                min_sample=wave.stats.get('canary_min_sample') if wave else None)
            signals += scopes.extra_signals(metrics, stream=blast.stream, blast=wave is None, canary=canary)
            accept(blast.stream, blast, metrics, signals, wave=wave)
    for stream in SendingStream.objects.filter(transactional=False):
        for hours in (1, 24, 168):
            metrics = scopes.stream_metrics(stream, now=now, hours=hours)
            # Other horizons are retained as aggregate health evidence; complaint action uses 24h.
            if hours == 24:
                accept(stream, None, metrics, [signal for signal in evaluate(metrics, stream=stream)
                    if signal.key == 'complaint_rate'], scope='24h')
        providers = BlastDelivery.objects.filter(blast__stream=stream, channel='email').exclude(provider='').values_list('provider', flat=True).distinct()
        for provider in providers:
            for hours in (1, 24):
                metrics = scopes.stream_metrics(stream, now=now, hours=hours, provider=provider)
                if hours == 1:
                    signals = scopes.extra_signals(metrics, stream=stream, provider=True)
                    signals += [signal for signal in evaluate(metrics, stream=stream) if signal.key == 'policy_block']
                    accept(stream, None, metrics, signals, scope='1h', provider=provider)
        pending = BlastDelivery.objects.filter(blast__stream=stream, channel='email',
            wave__started_at__gte=now - timedelta(hours=policy.value('CRM_GUARD_FEEDBACK_LOOKBACK_HOURS')),
            wave__started_at__lte=now - timedelta(minutes=policy.value('CRM_GUARD_FEEDBACK_MIN_AGE_MINUTES')))
        n = pending.exclude(status__in=('queued', 'cancelled', 'failed')).count()
        fresh = EmailEvent.objects.filter(stream=stream, source='resend',
            received_at__gte=now - timedelta(minutes=policy.value('CRM_FEEDBACK_MAX_AGE_MINUTES')),
            received_at__lte=now).exclude(recipient__iendswith='@resend.dev').exists()
        if n >= policy.value('CRM_GUARD_FEEDBACK_MIN_SAMPLE') and not fresh and pending.filter(status__in=('submitting', 'sent', 'unknown'), delivered_at__isnull=True).exists():
            signal = Signal('feedback_silence', 'amber', n, policy.value('CRM_GUARD_FEEDBACK_MIN_SAMPLE'), n,
                'hold_stream', evidence_key=scopes.evidence_key(pending.values_list('pk', flat=True)))
            incident = detect(None, signal, stream=stream, now=now)
            if incident:
                firing.add(incident.fingerprint); enforce(incident, now=now); detected += 1
        limits = list(CrmAuditEvent.objects.filter(target_type='deliverability_dependency', target_id=str(stream.pk),
            detail__operation='rate_limit', detail__epoch__gte=(now - timedelta(hours=1)).timestamp(), detail__epoch__lte=now.timestamp()).values_list('pk', flat=True))
        limits += list(EmailEvent.objects.filter(stream=stream, smtp_code='429', occurred_at__gte=now - timedelta(hours=1),
            occurred_at__lte=now).values_list('pk', flat=True))
        if len(limits) >= policy.value('CRM_GUARD_PROVIDER_RATE_LIMIT_COUNT', stream):
            signal = Signal('provider_rate_limit', 'amber', len(limits), policy.value('CRM_GUARD_PROVIDER_RATE_LIMIT_COUNT', stream),
                len(limits), 'half_slot', evidence_key=scopes.evidence_key(limits), scope='service1h')
            incident = detect(None, signal, stream=stream, now=now)
            if incident:
                firing.add(incident.fingerprint); enforce(incident, now=now); detected += 1
        # Dependency holds may only clear on positive evidence, never a missing/stale check.
        for kind, key in (('dns', 'dns_drift'), ('config', 'listmonk_down'), ('heartbeat', 'seed_spam'), ('postmaster', 'postmaster_spam_rate'), ('postmaster', 'postmaster_compliance')):
            check = observations.latest(stream, kind)
            incident = stream.incidents.filter(state='open', signal=key).first()
            clear = check.get('status') == 'pass'
            if kind == 'config':
                clear = check.get('status') == 'pass' and check.get('critical') == []
            elif kind == 'heartbeat':
                clear = check.get('grade') == 'pass'
            elif key == 'postmaster_spam_rate':
                known = [row['spam_rate'] for row in check.get('days', {}).values() if type(row.get('spam_rate')) in (int, float)]
                clear = bool(known) and max(known) < policy.value('CRM_GUARD_POSTMASTER_AMBER', stream)
            elif key == 'postmaster_compliance':
                clear = check.get('compliance', {}).get('status') == 'compliant'
            if incident and not clear:
                firing.add(incident.fingerprint)
    from . import lifecycle_guard
    lifecycle = lifecycle_guard.evaluate(now=now)
    firing.update(incident.fingerprint for incident in lifecycle)
    return detected + len(lifecycle), firing


def _clean_since(incident, *, now, firing):
    row = CrmAuditEvent.objects.filter(target_type='deliverability_incident', target_id=str(incident.pk),
        detail__operation__in=('condition_clear', 'condition_returned')).order_by('-created_at', '-pk').first()
    is_clear = bool(row and row.detail.get('operation') == 'condition_clear')
    if firing:
        if is_clear:
            CrmAuditEvent.objects.create(action='edit', target_type='deliverability_incident', target_id=str(incident.pk),
                detail={'operation': 'condition_returned', 'at': now.isoformat()})
        return None
    if not is_clear:
        CrmAuditEvent.objects.create(action='edit', target_type='deliverability_incident', target_id=str(incident.pk),
            detail={'operation': 'condition_clear', 'at': now.isoformat()})
        return now
    return parse_datetime(row.detail.get('at', '')) or now


def recover(firing, *, now):
    """Only amber automation heals; red and explicit owner/staff decisions remain durable."""
    from .guardian import _audit, amber_quiet_since
    if policy.value('CRM_GUARDIAN_DRY_RUN'):
        return
    for stream_id in SendingStream.objects.filter(transactional=False).values_list('pk', flat=True):
        with transaction.atomic():
            stream = SendingStream.objects.select_for_update().get(pk=stream_id)
            for incident in stream.incidents.select_for_update().filter(state='open', severity='amber'):
                clean = _clean_since(incident, now=now, firing=incident.fingerprint in firing)
                hours = (0 if incident.action == 'hold_stream' else policy.value('CRM_GUARD_PROVIDER_HOLD_HOURS')
                    if incident.action == 'hold_provider' else policy.value('CRM_GUARD_PROVIDER_CLEAN_HOURS')
                    if incident.action == 'throttle_provider' else policy.value('CRM_GUARD_RATE_LIMIT_HOURS')
                    if incident.signal == 'provider_rate_limit' else policy.value('CRM_GUARD_SLOW_CLEAN_HOURS'))
                quiet = incident.fingerprint not in firing and amber_quiet_since(incident, now=now)
                if quiet or (clean is not None and now >= clean + timedelta(hours=hours)):
                    incident.state, incident.resolved_at = 'resolved', now
                    incident.save(update_fields=['state', 'resolved_at', 'updated_at'])
                    _audit(incident, 'auto_resolved')
            open_actions = set(stream.incidents.filter(state='open').values_list('action', flat=True))
            slow_until = parse_datetime(str((stream.state_detail or {}).get('slow_until', '')))
            paused = stream.blasts.filter(pause_state='system').exists() if hasattr(stream, 'blasts') else Blast.objects.filter(stream=stream, pause_state='system').exists()
            if (stream.state in ('held', 'slowed') and not paused and not open_actions.intersection({'stop_stream', 'pause_blast', 'hold_stream', 'slow_stream'})
                    and (not slow_until or now >= slow_until)):
                stream.state, stream.state_reason, stream.state_changed_at = 'warming', 'checks_recovered', now
                stream.state_detail = {key: value for key, value in (stream.state_detail or {}).items() if key not in ('slow_until', 'incident_id')}
                stream.save(update_fields=['state', 'state_reason', 'state_changed_at', 'state_detail', 'updated_at'])
                CrmAuditEvent.objects.create(action='edit', target_type='sending_stream', target_id=str(stream.pk),
                    detail={'operation': 'auto_resumed', 'at': now.isoformat()})
