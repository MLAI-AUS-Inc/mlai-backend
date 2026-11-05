"""Bounded owner health summaries and audited policy overrides, without recipient identities."""

import copy
from datetime import datetime, time, timedelta

from django.db import transaction

from crm.blasts import BlastError
from crm.models import CrmAuditEvent, SeedTest, SendingStream

from . import observations, policy, volume


def rates(row):
    sent, delivered = row.get('sent', 0), row.get('delivered', 0)
    return {'delivered': delivered / sent if sent else None,
        'hard_bounce': row.get('bounced_hard', 0) / sent if sent else None,
        'complaint': row.get('complained', 0) / delivered if delivered else None,
        'unsubscribe': row.get('unsubscribed', 0) / sent if sent else None}


def forecast(stream, now):
    """Capacity on the next seven sending days, assuming healthy utilisation; not promised volume."""
    if stream.transactional:
        return []
    candidate = copy.copy(stream)
    first = now.astimezone(volume.MELBOURNE).date()
    result = []
    for offset in range(30):
        day = first + timedelta(days=offset)
        days, opening, _, zone = volume.window_spec(candidate)
        if day.weekday() not in days or day.isoformat() in policy.value('CRM_BLACKOUT_DATES'):
            continue
        result.append({'day': day.isoformat(), 'capacity': volume.budget(candidate)})
        if len(result) == 7:
            break
        if candidate.state in ('warming', 'active'):
            candidate.ramp_stage = min(candidate.ramp_stage + 1, len(policy.value('CRM_RAMP_LADDER')) - 1)
    return result


def stream_dto(stream, *, now):
    from .preflight.seeds import probe_result
    used, _ = volume.usage(stream, now) if not stream.transactional else (0, 0)
    history = []
    for day in stream.days.order_by('-day')[:30]:
        counts = {key: getattr(day, key) for key in ('sent', 'delivered', 'bounced_hard', 'bounced_soft', 'complained', 'unsubscribed', 'deferred', 'clicked')}
        history.append({'day': day.day.isoformat(), 'budget': day.budget, 'stage': day.ramp_stage, 'healthy': day.healthy,
            **counts, 'rates': rates(counts), 'by_provider': {provider: {**values, 'rates': rates(values)}
                for provider, values in day.by_provider.items()}, 'postmaster': day.postmaster})
    seeds = []
    for probe in SeedTest.objects.filter(stream=stream).order_by('-created_at')[:14]:
        result = probe_result(probe)
        seeds.append({'id': str(probe.pk), 'created_at': probe.created_at.isoformat(), 'status': probe.status,
            'heartbeat': bool(probe.results.get('heartbeat')), 'grade': result['grade'], 'reason': result['reason'],
            'results': [{key: row.get(key) for key in ('provider', 'folder', 'spf', 'dkim', 'dmarc')} for row in result['results']]})
    return {'key': stream.key, 'label': stream.label, 'domain': stream.domain,
        'from': stream.mailbox.email, 'reply_to': stream.mailbox.reply_to, 'state': stream.state,
        'state_reason': stream.state_reason, 'state_changed_at': stream.state_changed_at.isoformat() if stream.state_changed_at else None,
        'ramp_stage': stream.ramp_stage, 'transactional': stream.transactional, 'enabled': stream.mailbox.enabled,
        'today': {'day': now.astimezone(volume.MELBOURNE).date().isoformat(), 'budget': volume.budget(stream) if not stream.transactional else None,
            'reserved': used}, 'daily_budget_override': stream.daily_budget_override, 'policy_overrides': stream.policy_overrides,
        'capacity_forecast': forecast(stream, now), 'forecast_assumption': 'Healthy feedback and sufficient demand; sending windows and competing campaigns apply.',
        'days': history, 'seeds': seeds, 'checks': {kind: observations.latest(stream, kind)
            for kind in ('dns', 'config', 'reputation', 'postmaster', 'root_postmaster', 'heartbeat', 'suppressions')}}


def update_stream(key, values, *, actor, now):
    """Owners may tune bounded thresholds and windows, never secrets, identity or flags."""
    if not isinstance(values, dict) or not values or set(values) - {'daily_budget_override', 'policy_overrides'}:
        raise BlastError('invalid_stream_policy')
    with transaction.atomic():
        stream = SendingStream.objects.select_for_update().get(key=key)
        before = {'daily_budget_override': stream.daily_budget_override, 'policy_overrides': stream.policy_overrides}
        if 'daily_budget_override' in values:
            amount = values['daily_budget_override']
            if amount is not None and (type(amount) is not int or not 1 <= amount <= policy.value('CRM_STREAM_MAX_DAILY')):
                raise BlastError('invalid_stream_budget')
            if stream.transactional:
                raise BlastError('transactional_budget_not_applicable')
            stream.daily_budget_override = amount
        if 'policy_overrides' in values:
            overrides = values['policy_overrides']
            if not isinstance(overrides, dict) or set(overrides) - policy.OVERRIDABLE:
                raise BlastError('invalid_stream_policy')
            for name, value in overrides.items():
                default = policy.DEFAULTS[name]
                if type(value) is not type(default) and not (type(default) is float and type(value) is int):
                    raise BlastError('invalid_stream_policy')
                try:
                    policy.environment_settings({name: str(value)})
                except ValueError:
                    raise BlastError('invalid_stream_policy') from None
            stream.policy_overrides = overrides
            try:
                volume.window_spec(stream); volume.window_spec(stream, critical=True)
                for name in policy.OVERRIDABLE:
                    red = name.removesuffix('_AMBER') + '_RED'
                    if name.endswith('_AMBER') and red in policy.DEFAULTS and policy.value(name, stream) >= policy.value(red, stream):
                        raise ValueError
            except (ValueError, KeyError):
                raise BlastError('invalid_stream_policy') from None
        stream.save(update_fields=['daily_budget_override', 'policy_overrides', 'updated_at'])
        CrmAuditEvent.objects.create(actor=actor, action='edit', target_type='sending_stream', target_id=str(stream.pk),
            detail={'operation': 'policy_override', 'before': before,
                'after': {'daily_budget_override': stream.daily_budget_override, 'policy_overrides': stream.policy_overrides}})
    return stream
