"""Reserved, consent-checked batches with durable ownership of provider resources.

The lock order is stream, blast, wave, contacts, deliveries. A reservation is
committed before loading listmonk. Provider resources are found by their exact
stable name after a lost response. An uncertain start is always reconciled
against that same campaign, never replaced by another wave.
"""

import math
import logging
import re
from collections import Counter
from datetime import timedelta
from time import monotonic

from django.conf import settings
from django.db import transaction
from django.db.models import Count, Max, Q

from crm import blast_email, listmonk
from crm.models import Blast, BlastDelivery, BlastWave, Contact, CrmAuditEvent, EmailEvent, SendingStream, StreamDay
from crm.queries import banned

from . import planner, policy, volume

ACTIVE = ('loading', 'running', 'paused')
LIVE = ('queued', 'sending')
Status = BlastDelivery.Status
logger = logging.getLogger(__name__)


def managed(blast):
    """Once a blast owns waves it keeps this path even if the feature is disabled."""
    return bool(not (blast.stats or {}).get('warmup') and (
        (policy.value('CRM_WAVES_ENABLED') and blast.stream_id) or blast.waves.exists()))


def resource_name(wave):
    if (wave.stats or {}).get('legacy_adopted'):
        return blast_email.resource_name(wave.blast_id)
    return f'{blast_email.resource_name(wave.blast_id)}-w{wave.index}'


def consent_reason(delivery):
    """Describe the recorded permission without disclosing internal evidence."""
    contact = delivery.contact
    if delivery.consent_basis == 'event_registration':
        return "You're getting this because you registered for this MLAI event."
    if delivery.consent_basis == 'inferred_attendance':
        return "You're getting this because you attended MLAI events and can opt out at any time."
    stamp = contact.marketing_consent_at
    date = f' on {stamp.astimezone(volume.MELBOURNE).strftime("%-d %B %Y")}' if stamp else ''
    return f"You're getting this because you opted in to MLAI emails{date}."


def plain_body(blast):
    """Keep link destinations in a readable alternative part; escape Go actions."""
    text = re.sub(r'\[([^\]]+)\]\((https?://[^\s)]+)\)', r'\1 (\2)', blast.body_markdown)
    text = re.sub(r'(?m)^\s{0,3}#{1,6}\s+', '', text)
    text = re.sub(r'(?m)^\s*[-*]\s+', '• ', text)
    if getattr(blast, 'sign_off_name', ''):
        text += '\n\n— ' + blast.sign_off_name
    return blast_email.listmonk_text(text.replace('**', '').replace('__', ''), blast.event, markdown=False)


def campaign_payload(wave, blast):
    payload = blast_email.campaign_payload(blast, wave.listmonk_list_id)
    payload.update(name=resource_name(wave), from_email=blast_email.from_address('MLAI', blast.mailbox.email),
        altbody=plain_body(blast) + '\n\n{{ .Subscriber.Attribs.consent_reason }}\nUnsubscribe: {{ UnsubscribeURL }}')
    payload['headers'] += [{'X-MLAI-Blast': str(blast.pk)}, {'X-MLAI-Wave': str(wave.index)},
        {'Feedback-ID': f'{wave.index}:{str(blast.pk)[:8]}:{blast.stream.key}:mlai'}]
    return payload


def provider_states(blast):
    states = {}
    for provider, action in blast.stream.incidents.filter(state='open',
            action__in=('throttle_provider', 'hold_provider')).values_list('recipient_provider', 'action'):
        if provider and (provider not in states or action == 'hold_provider'):
            states[provider] = 'held' if action == 'hold_provider' else 'throttled'
    return states


def canary_block(blast, now):
    """The small initial batch gets its own sample size and a mandatory wait."""
    first = blast.waves.filter(started_at__isnull=False).order_by('index').first()
    if not first or (first.stats or {}).get('legacy_adopted'):
        return None
    wait = policy.value('CRM_EVENT_CANARY_WAIT_MINUTES' if volume.time_critical(blast, now) else 'CRM_CANARY_WAIT_MINUTES')
    if now < first.started_at + timedelta(minutes=wait):
        return 'checking_first_batch'
    from .guardian import Metrics, evaluate, policy_block
    rows = first.deliveries.all()
    n = rows.exclude(status__in=('queued', 'cancelled', 'failed')).count()
    hard = rows.filter(bounce_kind='hard').count()
    complaints = rows.filter(error_code='resend_complained').count()
    delivered = rows.filter(delivered_at__isnull=False).count()
    events = first.email_events.all()
    blocks = sum(policy_block(event) for event in events.filter(event_type__in=('email.bounced', 'email.failed')))
    delayed = events.filter(event_type='email.delivery_delayed').values('recipient').distinct().count()
    if n and delayed / n >= policy.value('CRM_CANARY_MAX_DEFERRAL_RATE'):
        return 'first_batch_deferrals'
    if blocks >= policy.value('CRM_GUARD_POLICY_BLOCK_MIN_COUNT'):
        return 'first_batch_policy'
    minimum = (first.stats or {}).get('canary_min_sample', policy.value('CRM_CANARY_MIN_SAMPLE'))
    if hard >= policy.value('CRM_CANARY_HARD_BOUNCE_COUNT') or (n >= minimum and hard / n >= policy.value('CRM_GUARD_HARD_BOUNCE_AMBER')):
        return 'first_batch_bounces'
    if evaluate(Metrics(n, delivered, hard, complaints, blocks), canary=True, min_sample=minimum):
        return 'first_batch_feedback'
    return None


def admission(blast, now, *, continuing=False):
    """Every fresh reservation and start must pass the same current checks."""
    if not policy.value('CRM_WAVES_ENABLED'):
        return 'waves_disabled'
    block = blast_email.email_block(blast)
    if block:
        return block
    if not settings.CRM_RESEND_WEBHOOK_SECRET:
        return 'feedback_not_configured'
    if continuing:
        # This audience already consumed capacity. The gate applies to later waves.
        return None
    if policy.value('CRM_PREFLIGHT_ENABLED'):
        from .preflight.service import admission as preflight_admission
        block = preflight_admission(blast)
        if block:
            return block
    if not volume.window(blast.stream, now, critical=volume.time_critical(blast, now)):
        return 'outside_sending_hours'
    block = canary_block(blast, now)
    if block:
        return block
    unanswered = blast.deliveries.filter(wave__started_at__isnull=False,
        status__in=('submitting', 'sent', 'unknown'), delivered_at__isnull=True).exists()
    if unanswered and not EmailEvent.objects.filter(
            stream=blast.stream, source='resend', received_at__gte=now - timedelta(minutes=policy.value('CRM_FEEDBACK_MAX_AGE_MINUTES')),
            received_at__lte=now).exists():
        return 'waiting_for_feedback'
    return None


def _lock(blast_id):
    stream_id = Blast.objects.values_list('stream_id', flat=True).get(pk=blast_id)
    stream = SendingStream.objects.select_for_update().get(pk=stream_id) if stream_id else None
    blast = Blast.objects.select_for_update(of=('self',)).select_related('mailbox', 'event').get(pk=blast_id)
    blast.stream = stream
    return blast


def _pending(blast):
    return blast.deliveries.filter(channel='email', wave__isnull=True, status='queued')


def _candidates(blast, now):
    rows = _pending(blast)
    if blast.queued_at and now >= blast.queued_at + timedelta(days=policy.value('CRM_FREQ_MAX_DEFER_DAYS')):
        eligible = planner.eligible_rows(rows, blast, now).values('pk')
        rows.exclude(pk__in=eligible).update(status=Status.CANCELLED, error_code='frequency_cap', updated_at=now)
    return planner.eligible_rows(rows, blast, now)


def _higher_priority(blast, now):
    for other in Blast.objects.filter(stream=blast.stream, status__in=LIVE, pause_state='').exclude(pk=blast.pk).select_related('event', 'mailbox'):
        if volume.priority(other, now) >= volume.priority(blast, now) or other.waves.filter(status__in=ACTIVE).exists():
            continue
        if admission(other, now) or not _candidates(other, now).exists():
            continue
        shares = {row['provider']: row['n'] for row in _candidates(other, now).values('provider').annotate(n=Count('pk')).order_by()}
        allowance, _ = volume.allowance_for(other, now)
        if sum(volume.provider_caps(allowance, shares, provider_states(other)).values()):
            return True
    return False


def reserve(blast_id, *, now):
    """The stream lock serializes capacity across competing dispatchers and blasts."""
    with transaction.atomic():
        blast = _lock(blast_id)
        active = blast.waves.filter(status__in=ACTIVE).first()
        if active:
            return active, None
        if blast.status not in LIVE:
            return None, 'blast_not_live'
        if not blast.stream_id:
            return None, 'stream_not_configured'
        volume.apply_idle_decay(blast.stream, now)
        block = admission(blast, now)
        if block:
            return None, block
        if _higher_priority(blast, now):
            return None, 'earlier_campaign'
        rows = _candidates(blast, now)
        shares = {row['provider']: row['n'] for row in rows.values('provider').annotate(n=Count('pk')).order_by()}
        allowance, cap = volume.allowance_for(blast, now)
        first = not blast.waves.filter(started_at__isnull=False).exists()
        reduced_canary = first and bool((blast.preflight or {}).get('seed_test', {}).get('timed_out'))
        if reduced_canary:
            allowance = math.ceil(allowance * policy.value('CRM_SEED_CANARY_FACTOR'))
        if blast.incidents.filter(state='open', action='slow_blast').exists():
            allowance = math.floor(allowance * policy.value('CRM_STREAM_SLOW_FACTOR'))
        caps = volume.provider_caps(allowance, shares, provider_states(blast))
        picked, counts = [], Counter()
        for row in rows.order_by('send_rank', 'pk').values('pk', 'provider').iterator():
            if counts[row['provider']] < caps.get(row['provider'], 0):
                picked.append(row['pk'])
                counts[row['provider']] += 1
                if len(picked) >= allowance:
                    break
        minimum_wave = min(policy.value('CRM_MIN_WAVE'), allowance) if reduced_canary else policy.value('CRM_MIN_WAVE')
        if not picked or (len(picked) < minimum_wave and len(picked) < sum(shares.values())):
            return None, 'waiting_for_capacity'
        index = (blast.waves.aggregate(n=Max('index'))['n'] or 0) + 1 if blast.waves.exists() else 0
        wave = BlastWave.objects.create(blast=blast, index=index, size=len(picked), reserved_at=now,
            kind='canary' if first else 'regular',
            stats={'hour_cap': cap, 'day_budget': volume.budget(blast.stream), 'by_provider_reserved': dict(counts),
                'canary_min_sample': math.ceil(policy.value('CRM_CANARY_MIN_SAMPLE') * (policy.value('CRM_SEED_CANARY_FACTOR') if reduced_canary else 1))})
        rows.filter(pk__in=picked).update(wave=wave, updated_at=now)
        CrmAuditEvent.objects.create(action='send', target_type='blast', target_id=str(blast.pk),
            detail={'operation': 'wave_reserved', 'wave': index, 'people': wave.size, 'hour_cap': cap})
        return wave, None


def _observed(wave, campaign, now):
    """Persist a provider outcome without replacing any terminal recipient feedback."""
    state = campaign.get('status')
    if state not in ('running', 'paused', 'finished', 'cancelled'):
        return False
    wave.status = state
    if wave.started_at is None:
        # Reservation time is a conservative bound when a start response was lost.
        wave.started_at = blast_email.parse_iso(campaign.get('started_at')) or wave.reserved_at
        wave.deliveries.filter(status=Status.QUEUED).update(status=Status.SUBMITTING, updated_at=now)
    if state in ('finished', 'cancelled'):
        wave.finished_at = wave.finished_at or now
        wave.deliveries.filter(status=Status.SUBMITTING).update(
            status=Status.SENT if state == 'finished' else Status.UNKNOWN, updated_at=now)
        wave.stats = {**(wave.stats or {}), 'cancel_pending': False, 'provider_retained_paused': False}
    wave.stats = {**(wave.stats or {}), **{key: campaign[key] for key in ('sent', 'to_send', 'views', 'clicks', 'bounces')
        if isinstance(campaign.get(key), int) and not isinstance(campaign.get(key), bool)}, 'synced_at': now.isoformat()}
    wave.save()
    return True


def _expire(wave, now):
    """Only a positively unstarted reservation may release its untouched rows."""
    wave.deliveries.filter(status__in=(Status.QUEUED, Status.SUBMITTING)).update(wave=None, status=Status.QUEUED, updated_at=now)
    wave.status, wave.finished_at = 'cancelled', now
    wave.stats = {**wave.stats, 'reservation_expired': True}
    wave.save()


def _ensure_resources(wave, blast):
    name = resource_name(wave)
    campaign = listmonk.get_campaign(wave.listmonk_campaign_id) if wave.listmonk_campaign_id else None
    if campaign and campaign.get('status') != 'draft':
        return campaign
    if not wave.listmonk_list_id:
        wave.listmonk_list_id = listmonk.find_list(name) or listmonk.create_list(name,
            tags=blast_email.campaign_tags(blast.pk), description='MLAI campaign batch')
        wave.save(update_fields=['listmonk_list_id', 'updated_at'])
    if not wave.listmonk_campaign_id:
        existing = listmonk.find_campaign(name)
        payload = campaign_payload(wave, blast)
        campaign = existing or listmonk.create_campaign(payload)
        wave.listmonk_campaign_id = int(campaign['id'])
        wave.save(update_fields=['listmonk_campaign_id', 'updated_at'])
        return campaign
    return campaign


def _contacts(wave):
    return list(Contact.objects.select_for_update().filter(pk__in=wave.deliveries.values('contact_id')).order_by('pk'))


def _recheck(wave, blast, now):
    """Remove new exclusions from this list without changing global consent."""
    invalid, subscriber_ids = {}, []
    eligible = set(planner.eligible_rows(wave.deliveries.all(), blast, now).values_list('pk', flat=True))
    from crm.queries import marketing_consent_q, registered_q
    from crm.models import EventRegistration
    if blast.purpose == 'marketing':
        permitted = set(Contact.objects.filter(pk__in=wave.deliveries.values('contact_id')).filter(
            marketing_consent_q(now)).values_list('pk', flat=True))
    else:
        permitted = set(EventRegistration.objects.filter(event_id=blast.event_id,
            contact_id__in=wave.deliveries.values('contact_id')).filter(registered_q()).values_list('contact_id', flat=True))
    for delivery in wave.deliveries.select_related('contact').annotate(is_banned=banned('contact__')):
        if delivery.status not in (Status.QUEUED, Status.SUBMITTING):
            continue
        reason = blast_email._drop_reason(blast, delivery.contact, delivery.is_banned)
        if delivery.address != delivery.contact.email:
            reason = 'address_changed'
        if delivery.pk not in eligible:
            reason = reason or 'frequency_cap'
        if not reason and delivery.contact_id not in permitted:
            reason = 'no_consent' if blast.purpose == 'marketing' else 'not_registered'
        if reason:
            invalid[delivery.pk] = reason
            if delivery.contact.listmonk_subscriber_id:
                subscriber_ids.append(delivery.contact.listmonk_subscriber_id)
    listmonk.remove_subscribers_from_list(subscriber_ids, wave.listmonk_list_id)
    for pk, reason in invalid.items():
        wave.deliveries.filter(pk=pk).update(status=Status.CANCELLED, error_code=reason, updated_at=now)


def advance(wave_id, *, now):
    """Reconcile first, load within the existing tick budget, then recheck/start."""
    tick_started = monotonic()
    blast_id = BlastWave.objects.values_list('blast_id', flat=True).get(pk=wave_id)
    with transaction.atomic():
        blast = _lock(blast_id)
        wave = BlastWave.objects.select_for_update().get(pk=wave_id)
        wave.stats = {**wave.stats, 'tracked_urls': blast_email.tracked_urls(blast_email.campaign_body(blast))}
        campaign = None
        if not wave.listmonk_campaign_id:
            # A lost start response may have rolled back both stored resource IDs.
            campaign = listmonk.find_campaign(resource_name(wave))
            if campaign:
                wave.listmonk_campaign_id = int(campaign['id'])
                wave.save(update_fields=['listmonk_campaign_id', 'updated_at'])
        if not wave.listmonk_campaign_id and blast.status not in LIVE:
            _cancel_unstarted(wave, now)
            return {'wave': wave.index, 'campaign': 'cancelled'}
        if not wave.listmonk_campaign_id:
            if blast.pause_state:
                return {'blocked': 'blast_paused'}
            block = admission(blast, now) if blast.stream_id else 'stream_not_configured'
            if block:
                return {'blocked': block}
        campaign = campaign or _ensure_resources(wave, blast)
        if _observed(wave, campaign, now):
            _record_handoff(wave, blast)
            if wave.status == 'cancelled' and blast.status in LIVE:
                # An external cancellation is not permission to retry its audience.
                _pending(blast).update(status=Status.CANCELLED, error_code='provider_cancelled', updated_at=now)
                if blast.last_error != 'listmonk_campaign_cancelled':
                    blast.last_error = 'listmonk_campaign_cancelled'
                    blast.save(update_fields=['last_error', 'updated_at'])
                    CrmAuditEvent.objects.create(action='edit', target_type='blast', target_id=str(blast.pk),
                        detail={'operation': 'provider_cancelled', 'wave': wave.index})
            if blast.status not in LIVE:
                _control(wave, 'cancelled', now)
            elif blast.pause_state:
                _control(wave, 'paused', now)
            elif wave.status == 'paused' and blast_email.email_stats(blast).get('resume_pending'):
                block = admission(blast, now, continuing=True)
                if block:
                    return {'blocked': block}
                _control(wave, 'running', now)
                blast.stats = blast_email.with_email_stats(blast.stats, resume_pending=False)
                blast.save(update_fields=['stats', 'updated_at'])
            elif not blast.pause_state and blast_email.email_stats(blast).get('resume_pending'):
                blast.stats = blast_email.with_email_stats(blast.stats, resume_pending=False)
                blast.save(update_fields=['stats', 'updated_at'])
            return {'wave': wave.index, 'campaign': wave.status}
        if wave.started_at:
            return {'blocked': 'campaign_state_unknown'}
        if campaign.get('status') != 'draft':
            return {'blocked': 'campaign_state_unknown'}
        if blast.status not in LIVE:
            _cancel_unstarted(wave, now)
            return {'wave': wave.index, 'campaign': 'cancelled'}
        if not blast.stream_id:
            return {'blocked': 'stream_not_configured'}
        bounds = volume.window(blast.stream, now, critical=volume.time_critical(blast, now))
        reserved_bounds = volume.window(blast.stream, wave.reserved_at, critical=volume.time_critical(blast, wave.reserved_at))
        if not (wave.stats or {}).get('legacy_adopted') and (not bounds or not reserved_bounds or bounds['slot'] != reserved_bounds['slot']):
            _expire(wave, now)
            return {'blocked': 'reservation_expired'}
        if blast.status not in LIVE or blast.pause_state:
            return {'blocked': 'blast_not_live' if blast.status not in LIVE else 'blast_paused'}
        block = admission(blast, now)
        if block:
            return {'blocked': block}
        if not wave.listmonk_list_id:
            _ensure_resources(wave, blast)
        _contacts(wave)
        _recheck(wave, blast, now)
        remaining = blast_email.SUBSCRIBERS_PER_TICK
        while remaining > 0 and monotonic() - tick_started < blast_email.TICK_SECONDS:
            batch = list(wave.deliveries.filter(status=Status.QUEUED).select_related('contact')
                .annotate(is_banned=banned('contact__')).order_by('send_rank', 'pk')[:min(remaining, blast_email.SUBSCRIBER_BATCH)])
            if not batch:
                break
            blast_email._load_batch(blast, wave.listmonk_list_id, batch, now, consent_reasons=True)
            remaining -= len(batch)
        if wave.deliveries.filter(status=Status.QUEUED).exists():
            return {'wave': wave.index, 'pending': True}
        _recheck(wave, blast, now)
        if not wave.deliveries.filter(status=Status.SUBMITTING).exists():
            wave.status, wave.finished_at = 'cancelled', now
            wave.save()
            return {'wave': wave.index, 'campaign': 'cancelled'}
        # A last consent check shares contact locks with webhook/import updates.
        effective_now = now + timedelta(seconds=int(monotonic() - tick_started))
        latest_bounds = volume.window(blast.stream, effective_now, critical=volume.time_critical(blast, effective_now))
        if not latest_bounds or (not wave.stats.get('legacy_adopted') and latest_bounds['slot'] != reserved_bounds['slot']):
            # Time spent preparing the batch cannot grant capacity in another slot.
            observed = listmonk.get_campaign(wave.listmonk_campaign_id)
            if _observed(wave, observed, effective_now):
                _record_handoff(wave, blast)
                return {'wave': wave.index, 'campaign': wave.status}
            _expire(wave, effective_now)
            return {'blocked': 'reservation_expired'}
        listmonk.update_campaign(wave.listmonk_campaign_id, campaign_payload(wave, blast))
        listmonk.set_campaign_status(wave.listmonk_campaign_id, 'running')
        wave.status, wave.started_at = 'running', effective_now
        wave.stats = {**wave.stats, 'handed': wave.deliveries.filter(status=Status.SUBMITTING).count()}
        wave.save()
        blast.stats = blast_email.with_email_stats(blast.stats, resume_pending=False)
        blast.save(update_fields=['stats', 'updated_at'])
        _record_handoff(wave, blast)
        CrmAuditEvent.objects.create(action='send', target_type='blast', target_id=str(blast.pk),
            detail={'operation': 'wave_started', 'wave': wave.index, 'people': wave.stats['handed']})
        return {'wave': wave.index, 'campaign': 'running'}


def _record_handoff(wave, blast):
    if not wave.started_at or not blast.stream_id:
        return
    day = wave.started_at.astimezone(volume.MELBOURNE).date()
    stream = blast.stream
    if not stream.last_send_day or day > stream.last_send_day:
        stream.last_send_day = day
        stream.save(update_fields=['last_send_day', 'updated_at'])
    StreamDay.objects.get_or_create(stream=stream, day=day,
        defaults={'budget': wave.stats.get('day_budget', volume.budget(stream)), 'ramp_stage': stream.ramp_stage})


def _control(wave, desired, now):
    if wave.status in ('finished', 'cancelled', 'failed'):
        return
    if desired == 'cancelled' and wave.status == 'paused':
        # listmonk permits running -> cancelled, but not paused -> cancelled.
        # Keep it paused permanently; resuming just to cancel could send mail.
        wave.status, wave.finished_at = 'cancelled', now
        wave.deliveries.filter(status=Status.SUBMITTING).update(status=Status.UNKNOWN, updated_at=now)
        wave.stats = {**wave.stats, 'cancel_pending': False, 'provider_retained_paused': True}
        wave.save()
        return
    if wave.status != desired:
        listmonk.set_campaign_status(wave.listmonk_campaign_id, desired)
        _observed(wave, {'status': desired}, now)


def dispatch(blast_id, *, now):
    wave, block = reserve(blast_id, now=now)
    return advance(wave.pk, now=now) if wave else {'blocked': block}


def cancel_locked(blast, now):
    """Commit cancellation intent; the sync retries provider effects after outages."""
    for wave in blast.waves.filter(status__in=ACTIVE):
        wave.stats = {**wave.stats, 'cancel_pending': True}
        wave.save(update_fields=['stats', 'updated_at'])
    blast.deliveries.filter(channel='email', wave__isnull=True, status='queued').update(status=Status.CANCELLED, updated_at=now)
    transaction.on_commit(lambda: reconcile_controls(blast.pk, now))
    return {'cancel_pending': blast.waves.filter(status__in=ACTIVE).exists()}


def _cancel_unstarted(wave, now):
    wave.deliveries.filter(status__in=(Status.QUEUED, Status.SUBMITTING)).update(status=Status.CANCELLED, updated_at=now)
    wave.status, wave.finished_at = 'cancelled', now
    wave.stats = {**wave.stats, 'cancel_pending': False}
    wave.save()


def reconcile_controls(blast_id, now):
    """External effects run only after intent commits and remain retryable on failure."""
    try:
        if not BlastWave.objects.filter(blast_id=blast_id).exists():
            with transaction.atomic():
                blast = _lock(blast_id)
                if blast.pause_state and blast.listmonk_campaign_id:
                    campaign = listmonk.get_campaign(blast.listmonk_campaign_id)
                    if campaign.get('status') == 'running':
                        listmonk.set_campaign_status(blast.listmonk_campaign_id, 'paused')
            return
        for wave in BlastWave.objects.filter(blast_id=blast_id, status__in=ACTIVE):
            advance(wave.pk, now=now)
        if not BlastWave.objects.filter(blast_id=blast_id, status__in=ACTIVE).exists():
            blast_email.merge_email_stats(blast_id, cancel_pending=False)
    except Exception as exc:
        logger.warning('CRM campaign control deferred: %s', type(exc).__name__)


def fail(blast_id, code, now):
    """A permanent refusal must not erase a provider start that already succeeded."""
    with transaction.atomic():
        blast = _lock(blast_id)
        wave = blast.waves.filter(status__in=ACTIVE).first()
        if wave:
            campaign = listmonk.get_campaign(wave.listmonk_campaign_id) if wave.listmonk_campaign_id else listmonk.find_campaign(resource_name(wave))
            if campaign and _observed(wave, campaign, now):
                return
            if campaign and campaign.get('status') != 'draft':
                return
            wave.status, wave.finished_at = 'failed', now
            wave.save()
        blast.deliveries.filter(channel='email', status__in=('queued', 'submitting')).update(
            status=Status.FAILED, error_code=code[:32], updated_at=now)
        blast.last_error = code
        blast.save(update_fields=['last_error', 'updated_at'])


def pause_blast(blast_id, *, actor, now, resume=False, reason=''):
    """Commit staff intent before reconciling the same campaign on a later tick."""
    from community_chat.admin_access import require_mlai_admin
    from crm.blasts import BlastError
    require_mlai_admin(actor)
    if len(reason) > 500:
        raise BlastError('invalid_pause_reason')
    with transaction.atomic():
        blast = _lock(blast_id)
        if blast.status not in LIVE:
            raise BlastError('blast_not_live', 409)
        if resume:
            if blast.pause_state == 'system':
                raise BlastError('owner_required', 403)
            if blast.pause_state == 'staff' and blast.paused_by_id != actor.pk:
                raise BlastError('pause_owner_required', 403)
            if not blast.pause_state:
                return blast
            blast.pause_state, blast.pause_reason, blast.paused_by, blast.paused_at = '', '', None, None
            blast.stats = blast_email.with_email_stats(blast.stats, resume_pending=True)
        else:
            if blast.pause_state == 'system':
                raise BlastError('owner_required', 403)
            if blast.pause_state == 'staff':
                return blast
            blast.pause_state, blast.pause_reason, blast.paused_by, blast.paused_at = 'staff', reason or 'staff_paused', actor, now
        blast.save(update_fields=['pause_state', 'pause_reason', 'paused_by', 'paused_at', 'stats', 'updated_at'])
        CrmAuditEvent.objects.create(actor=actor, action='edit', target_type='blast', target_id=str(blast.pk),
            detail={'operation': 'staff_resume' if resume else 'staff_pause'})
        transaction.on_commit(lambda: reconcile_controls(blast.pk, now))
        return blast
