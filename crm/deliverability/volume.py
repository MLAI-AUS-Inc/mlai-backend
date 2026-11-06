"""Stream budgets, hourly admission and the same forward model used by estimates."""

import copy
import math
import re
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.db.models import Q, Sum

from crm.models import BlastWave, CrmAuditEvent

from . import policy

MELBOURNE = ZoneInfo('Australia/Melbourne')
WEEKDAYS = ('mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun')


def time_critical(blast, now):
    start = getattr(getattr(blast, 'event', None), 'start_at', None)
    return bool(blast.purpose == 'event_update' and start and now <= start <= now + timedelta(hours=policy.value('CRM_TIME_CRITICAL_HOURS')))


def priority(blast, now):
    from .reconfirmation import is_reconfirmation
    return (0 if time_critical(blast, now) else 1 if blast.purpose == 'event_update' else 3 if is_reconfirmation(blast) else 2,
            blast.queued_at or blast.created_at, str(blast.pk))


def window_spec(stream, *, critical=False):
    raw = policy.value('CRM_EVENT_SEND_WINDOW' if critical else 'CRM_SEND_WINDOW', stream)
    match = re.fullmatch(r'([a-z]{3})(?:-([a-z]{3}))? (\d{2}):(\d{2})-(\d{2}):(\d{2}) ([A-Za-z_]+/[A-Za-z_]+)', raw)
    if not match or match[1] not in WEEKDAYS or (match[2] and match[2] not in WEEKDAYS):
        raise ValueError('Invalid stream sending window')
    first, last = WEEKDAYS.index(match[1]), WEEKDAYS.index(match[2] or match[1])
    days = {(first + offset) % len(WEEKDAYS) for offset in range((last - first) % len(WEEKDAYS) + 1)}
    start, end = time(int(match[3]), int(match[4])), time(int(match[5]), int(match[6]))
    if start >= end:
        raise ValueError('Sending windows must finish on the same day')
    return days, start, end, ZoneInfo(match[7])


def window(stream, now, *, critical=False):
    """Current one-hour slot and window bounds, or None outside an allowed window."""
    days, start, end, zone = window_spec(stream, critical=critical)
    local = now.astimezone(zone)
    if local.weekday() not in days or local.date().isoformat() in policy.value('CRM_BLACKOUT_DATES'):
        return None
    opening = datetime.combine(local.date(), start, zone)
    closing = datetime.combine(local.date(), end, zone)
    if not opening <= local < closing:
        return None
    hour = timedelta(hours=1)
    slot = opening + (local - opening) // hour * hour
    slots = math.ceil((closing - opening) / hour)
    left = math.ceil((closing - slot) / hour)
    return {'open': opening, 'close': closing, 'slot': slot, 'slots': slots, 'left': left}


def budget(stream, day=None):
    """No state or override can exceed the stream maximum; holds admit nothing."""
    ladder = policy.value('CRM_RAMP_LADDER')
    stage = min(stream.ramp_stage, len(ladder) - 1)
    maximum = min(policy.value('CRM_STREAM_MAX_DAILY'), ladder[-1])
    if stream.state in ('held', 'stopped'):
        return 0
    amount = stream.daily_budget_override
    if amount is None:
        amount = ladder[stage]
        if stream.state == 'active':
            amount = max(amount, math.floor(policy.value('CRM_STREAM_ACTIVE_PEAK_FACTOR') * stream.peak_healthy_daily_volume))
    amount = min(maximum, max(0, amount))
    return math.floor(amount * (policy.value('CRM_STREAM_SLOW_FACTOR') if stream.state == 'slowed' else 1))


def slot_allowance(day_budget, used_today, used_this_hour, slots, slots_left, *, canary=False):
    """Pure bounded allocation. The first canary has an explicit initial allowance."""
    remaining = max(0, day_budget - used_today)
    hour_cap = math.ceil(policy.value('CRM_HOURLY_HEADROOM') * day_budget / slots)
    if canary:
        target = min(policy.value('CRM_CANARY_MAX'), max(policy.value('CRM_CANARY_MIN'), math.ceil(
            policy.value('CRM_CANARY_FRACTION') * math.ceil(remaining / slots_left))))
        hour_cap = max(hour_cap, min(day_budget, target))
        return max(0, min(remaining, target, hour_cap - used_this_hour)), hour_cap
    return max(0, min(remaining, math.ceil(remaining / slots_left) - used_this_hour, hour_cap - used_this_hour)), hour_cap


def usage(stream, now, *, slot_start=None):
    """Reservations consume capacity before provider calls, including uncertain starts."""
    local = now.astimezone(MELBOURNE)
    start = datetime.combine(local.date(), time.min, MELBOURNE)
    end = start + timedelta(days=1)
    hour = slot_start or local.replace(minute=0, second=0, microsecond=0)
    # An unstarted loading wave belongs to its reservation; started waves to handoff time.
    rows = BlastWave.objects.filter(blast__stream=stream).filter(
        Q(started_at__gte=start, started_at__lt=end)
        | Q(started_at__isnull=True, status='loading', reserved_at__gte=start, reserved_at__lt=end))
    today = rows.aggregate(n=Sum('size'))['n'] or 0
    this_hour = rows.filter(Q(started_at__gte=hour) | Q(started_at__isnull=True, reserved_at__gte=hour)).aggregate(n=Sum('size'))['n'] or 0
    return today, this_hour


def allowance_for(blast, now):
    """Must be called with the stream row locked when making a reservation."""
    stream = blast.stream
    current = window(stream, now, critical=time_critical(blast, now))
    if not current:
        return 0, 0
    used_today, used_hour = usage(stream, now, slot_start=current['slot'])
    first = not blast.waves.filter(started_at__isnull=False).exists()
    allowance, cap = slot_allowance(budget(stream), used_today, used_hour, current['slots'], current['left'], canary=first)
    if not first and stream.incidents.filter(state='open', action='half_slot').exists():
        allowance //= 2
    if blast.incidents.filter(state='open', action='slow_blast').exists():
        allowance = max(1, allowance // 2) if allowance else 0
    from . import reconfirmation
    if reconfirmation.is_reconfirmation(blast):
        allowance = reconfirmation.allowance(blast, now, allowance)
    return allowance, cap


def provider_caps(allowance, shares, states=None):
    """Limits use the remaining audience mix; held recipients keep their ranks."""
    total = sum(shares.values())
    if not total:
        return {}
    states = states or {}
    return {provider: min(count, math.ceil(allowance * count / total * policy.value('CRM_PROVIDER_SHARE_HEADROOM') *
        (0 if states.get(provider) == 'held' else policy.value('CRM_PROVIDER_THROTTLE_FACTOR') if states.get(provider) == 'throttled' else 1)))
        for provider, count in shares.items()}


def decay_stage(stage, idle_days):
    if idle_days >= policy.value('CRM_IDLE_RESET_DAYS'):
        return 0
    if idle_days >= policy.value('CRM_IDLE_LONG_DECAY_DAYS'):
        return max(0, stage - policy.value('CRM_IDLE_LONG_DECAY_STAGES'))
    if idle_days >= policy.value('CRM_IDLE_DECAY_DAYS'):
        return max(0, stage - policy.value('CRM_IDLE_DECAY_STAGES'))
    return stage


def apply_idle_decay(stream, now):
    """Idempotent for each idle gap, including retries before the first provider start."""
    if not stream.last_send_day or stream.state in ('held', 'stopped'):
        return
    idle = (now.astimezone(MELBOURNE).date() - stream.last_send_day).days
    anchor = stream.last_send_day.isoformat()
    if idle < policy.value('CRM_IDLE_DECAY_DAYS') or (stream.state_detail or {}).get('idle_decay_for') == anchor:
        return
    before = stream.ramp_stage
    stream.ramp_stage = decay_stage(before, idle)
    if stream.state == 'active':
        stream.state = 'warming'
    stream.ramp_updated_at = now
    stream.state_detail = {**(stream.state_detail or {}), 'idle_decay_for': anchor}
    stream.save(update_fields=['ramp_stage', 'state', 'ramp_updated_at', 'state_detail', 'updated_at'])
    CrmAuditEvent.objects.create(action='edit', target_type='sending_stream', target_id=str(stream.pk),
        detail={'operation': 'idle_decay', 'from_stage': before, 'stage': stream.ramp_stage, 'idle_days': idle})


def healthy_day(row, *, incident=False, feedback=True):
    return bool(row.sent and feedback and row.delivered and not incident
        and row.bounced_hard / row.sent < policy.value('CRM_RAMP_HEALTHY_HARD_BOUNCE')
        and row.complained / row.delivered < policy.value('CRM_RAMP_HEALTHY_COMPLAINT')
        and row.deferred / row.sent < policy.value('CRM_RAMP_HEALTHY_DEFERRAL'))


def promote_stage(stage, sent, day_budget, healthy):
    if healthy and day_budget and sent / day_budget >= policy.value('CRM_RAMP_MIN_UTILIZATION'):
        return min(stage + 1, len(policy.value('CRM_RAMP_LADDER')) - 1)
    return stage


def preview_stream(stream, now):
    """Project the next send's idle adjustment once, without persisting a change."""
    candidate = copy.copy(stream)
    if (stream.last_send_day and stream.state not in ('held', 'stopped')
            and (stream.state_detail or {}).get('idle_decay_for') != stream.last_send_day.isoformat()):
        candidate.ramp_stage = decay_stage(stream.ramp_stage, (now.astimezone(MELBOURNE).date() - stream.last_send_day).days)
        if candidate.ramp_stage != stream.ramp_stage and candidate.state == 'active':
            candidate.state = 'warming'
    return candidate


def estimate(stream, people, now, *, used_today=0, used_hour=0, canary_started_at=None, critical=False, canary_factor=1):
    """Simulate the same allocation rules, assuming healthy feedback and promotions."""
    simulated = preview_stream(stream, now)
    if not 0 < canary_factor <= 1:
        raise ValueError('Invalid initial batch factor')
    remaining = people
    schedule, hours = [], []
    clock = now.replace(second=0, microsecond=0)
    first = canary_started_at is None
    gate = canary_started_at
    day = clock.astimezone(MELBOURNE).date()
    spent = used_today
    hourly_used = used_hour
    last_hour = clock.astimezone(MELBOURNE).replace(minute=0)
    day_count = 0
    deadline = clock + timedelta(days=policy.value('CRM_ESTIMATE_MAX_DAYS'))
    if simulated.state in ('held', 'stopped'):
        return {'days': None, 'finish_at': None, 'schedule': [], 'status': 'waiting_for_checks'}
    while remaining and clock < deadline:
        current_day = clock.astimezone(MELBOURNE).date()
        if current_day != day:
            if day_count:
                schedule.append({'day': day.isoformat(), 'count': day_count})
                simulated.ramp_stage = promote_stage(simulated.ramp_stage, spent, budget(simulated), True)
            day, spent, day_count = current_day, 0, 0
        bounds = window(simulated, clock, critical=critical)
        hour = bounds['slot'] if bounds else clock.astimezone(MELBOURNE).replace(minute=0)
        if hour != last_hour:
            hourly_used, last_hour = 0, hour
        if bounds:
            wait = policy.value('CRM_EVENT_CANARY_WAIT_MINUTES' if critical else 'CRM_CANARY_WAIT_MINUTES')
            gate_clear = first or gate is None or clock >= gate + timedelta(minutes=wait)
            allowance, cap = slot_allowance(budget(simulated), spent, hourly_used, bounds['slots'], bounds['left'], canary=first)
            if first:
                allowance = math.ceil(allowance * canary_factor)
            n = min(remaining, allowance) if gate_clear else 0
            if n and (first or n >= policy.value('CRM_MIN_WAVE') or n == remaining):
                hours.append({'at': clock.isoformat(), 'count': n, 'hour_cap': cap, 'day_budget': budget(simulated)})
                remaining -= n
                spent += n
                hourly_used += n
                day_count += n
                if first:
                    gate, first = clock, False
        if remaining:
            clock += timedelta(minutes=1)
    if day_count:
        schedule.append({'day': day.isoformat(), 'count': day_count})
    return {'days': len(schedule) if not remaining else None, 'finish_at': clock.isoformat() if not remaining else None,
            'schedule': schedule, 'status': 'expected' if not remaining else 'waiting_for_capacity', 'slots': hours}
