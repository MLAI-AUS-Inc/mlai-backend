"""Fault injections for the guarded, rollback-only campaign simulation command."""

import math
from datetime import timedelta
from unittest.mock import patch

from django.core.cache import cache

from crm.models import BlastDelivery, Contact, CrmEvent, DeliverabilityIncident, EventRegistration

from . import guardian, observations, volume, waves
from .monitors import config_check, dns, postmaster
from .simulation import require


def exercise(sim, scenario, days):
    """Inject provider observations, then assert the actual persisted decisions."""
    if scenario in ('healthy', 'outlook_deferrals'):
        sim.finish(days, outlook=scenario == 'outlook_deferrals')
        return
    if scenario == 'complaints_creep':
        sim.finish(days)
        rows = list(sim.blast.deliveries.order_by('send_rank')[:max(3, math.ceil(sim.recipients * .0012))])
        for row in rows:
            sim.event(row, 'email.complained')
        sim.guard()
        incident = DeliverabilityIncident.objects.get(signal='complaint_rate', state='open', blast=sim.blast, wave__isnull=True)
        sim.stream.refresh_from_db()
        require(sim.stream.state == 'stopped', 'complaints stop the stream')
        before = sim.stream.ramp_stage
        for other in DeliverabilityIncident.objects.filter(state='open', severity='red').exclude(pk=incident.pk):
            guardian.acknowledge(other.pk, actor=sim.actor, resolution='keep_stopped', now=sim.now)
        guardian.acknowledge(incident.pk, actor=sim.actor, resolution='resume_slow', now=sim.now)
        sim.stream.refresh_from_db()
        require(sim.stream.ramp_stage == max(0, before - 2), 'owner recovery demotes two stages')
        require(sim.stream.state == 'slowed' and volume.budget(sim.stream) <= volume.policy.value('CRM_RAMP_LADDER')[sim.stream.ramp_stage] / 2,
            'owner recovery uses half speed')
        sim.evidence['complaint_rate'] = len(rows) / sim.recipients
        return
    if scenario == 'idle_decay':
        sim.stream.ramp_stage = 8
        sim.stream.last_send_day = sim.now.date() - timedelta(days=90)
        sim.stream.save()
        sim.first()
        sim.stream.refresh_from_db()
        require(sim.stream.ramp_stage == 3, 'ninety idle days demote five stages')
        waves.reserve(sim.blast.pk, now=sim.now)
        sim.stream.refresh_from_db()
        require(sim.stream.ramp_stage == 3, 'idle decay is applied once')
        sim.evidence['stage_after_idle'] = 3
        return
    if scenario == 'bounce_spike_canary':
        wave = sim.first()
        rows = list(wave.deliveries.order_by('send_rank')[:math.ceil(wave.size * .08)])
        BlastDelivery.objects.filter(pk__in=[row.pk for row in rows]).update(status='bounced', bounce_kind='hard', delivered_at=None)
        for row in rows:
            sim.event(row, 'email.bounced', bounce_type='Permanent')
        sim.guard()
        sim.now += timedelta(hours=1)
        sim.dispatch()
        sim.blast.refresh_from_db(); sim.stream.refresh_from_db()
        require(sim.blast.pause_state == 'system' and sim.stream.state == 'held', 'bounce canary pauses and holds')
        require(sim.blast.waves.count() == 1, 'no second wave after canary failure')
        sim.evidence['canary_bounce_rate'] = len(rows) / wave.size
        return
    if scenario == 'gmail_block_codes':
        wave = sim.first()
        for row in wave.deliveries.filter(provider='google')[:3]:
            sim.event(row, 'email.failed', enhanced_code='5.7.1', diagnostic='Likely unsolicited mail')
        sim.guard()
        sim.stream.refresh_from_db()
        require(sim.stream.state == 'stopped', 'three Google policy codes stop the stream')
        require(cache.get('crm:deliverability:urgent-dns:' + str(sim.stream.pk)), 'policy rejection schedules urgent DNS')
        return
    if scenario == 'webhook_outage':
        wave = sim.first(feedback=False)
        sim.now += timedelta(minutes=70)
        sim.guard()
        sim.stream.refresh_from_db()
        require(sim.stream.state == 'held', 'unresolved handoffs hold after seventy silent minutes')
        sim.feedback(wave)
        sim.guard()
        sim.stream.refresh_from_db()
        require(sim.stream.state == 'warming', 'positive callbacks automatically release feedback hold')
        sim.dispatch()
        sim.evidence['silence_minutes'] = 70
        return
    if scenario == 'dns_drift':
        sim.first()
        with patch.object(dns, 'advisories', return_value={}), patch.object(dns.sending_domains, 'domain_health',
                side_effect=[{'status': 'failing'}, {'status': 'failing'}, {'status': 'verified'}, {'status': 'verified'}]):
            dns.check(sim.stream, now=sim.now)
            sim.now += timedelta(minutes=10); dns.check(sim.stream, now=sim.now)
            sim.stream.refresh_from_db(); require(sim.stream.state == 'held', 'two DNS failures hold')
            sim.now += timedelta(minutes=10); dns.check(sim.stream, now=sim.now)
            sim.guard(); sim.stream.refresh_from_db(); require(sim.stream.state == 'held', 'one pass does not release DNS hold')
            sim.now += timedelta(minutes=10); dns.check(sim.stream, now=sim.now)
            sim.guard(); sim.stream.refresh_from_db(); require(sim.stream.state == 'warming', 'two spaced DNS passes release hold')
        return
    if scenario == 'listmonk_down':
        wave = sim.first(feedback=False)
        campaign = wave.listmonk_campaign_id
        sim.provider.down = True
        with (patch('crm.deliverability.preflight.render.template_body', side_effect=config_check.listmonk.ListmonkError('unavailable')),
                patch('crm.deliverability.monitors.resend.check', return_value={'status': 'pass', 'critical': [], 'warnings': []})):
            config_check.check(sim.stream, now=sim.now)
        sim.guard(); sim.stream.refresh_from_db(); require(sim.stream.state == 'held', 'dependency outage holds')
        sim.dispatch(feedback=False)
        require(len(sim.provider.campaigns) == 1, 'outage keeps existing campaign')
        sim.provider.down = False
        sim.now += timedelta(minutes=10)
        observations.record(sim.stream, 'config', {'status': 'pass', 'critical': []}, now=sim.now)
        sim.guard(); sim.feedback(wave); sim.dispatch()
        wave.refresh_from_db()
        require(wave.listmonk_campaign_id == campaign and sim.provider.initial_starts[campaign] == 1, 'recovery uses original provider campaign')
        return
    if scenario == 'postmaster_spam_high':
        sim.stream.ramp_stage = 5; sim.stream.save()
        sim.first()
        sim.now += timedelta(days=1)
        day = (sim.now - timedelta(days=1)).date().isoformat()
        with patch.object(postmaster, 'token', return_value='synthetic'), patch.object(postmaster, 'query',
                return_value={day: {'spam_rate': .0018}}), patch.object(postmaster, 'json_request', return_value={}):
            postmaster.check(sim.stream, now=sim.now)
            postmaster.check(sim.stream, now=sim.now)
        sim.stream.refresh_from_db()
        require(sim.stream.state == 'stopped' and sim.stream.ramp_stage == 3, 'late Postmaster report stops and demotes once')
        sim.evidence['postmaster_spam_rate'] = .0018
        return
    if scenario == 'staff_pause_resume':
        wave = sim.first(feedback=False)
        before = list(sim.blast.deliveries.order_by('send_rank').values_list('pk', 'wave_id', 'send_rank'))
        campaign = wave.listmonk_campaign_id
        waves.pause_blast(sim.blast.pk, actor=sim.actor, now=sim.now, reason='Synthetic staff pause')
        sim.dispatch(feedback=False)
        require(sim.provider.campaigns[campaign]['status'] == 'paused', 'staff pauses existing provider campaign')
        waves.pause_blast(sim.blast.pk, actor=sim.actor, now=sim.now, resume=True)
        sim.dispatch(feedback=False)
        require(sim.provider.campaigns[campaign]['status'] == 'running', 'staff resumes existing provider campaign')
        require(before == list(sim.blast.deliveries.order_by('send_rank').values_list('pk', 'wave_id', 'send_rank')), 'staff controls preserve ranks, counts and assignments')
        sim.feedback(wave)
        return
    if scenario == 'event_update_priority':
        marketing = sim.blast
        wave = sim.first()
        sim.now += timedelta(hours=1)
        event = CrmEvent.objects.create(luma_event_id='simulation-event', name='Synthetic event',
            start_at=sim.now + timedelta(hours=24), url='https://example.test/event')
        EventRegistration.objects.bulk_create([EventRegistration(event=event, contact=person,
            approval_status='approved', registered_at=sim.now) for person in Contact.objects.all()])
        urgent = sim.queue(event=event)
        # Observe the finished marketing wave before testing its next reservation.
        waves.advance(wave.pk, now=sim.now)
        require(waves.reserve(marketing.pk, now=sim.now)[1] == 'earlier_campaign', 'time-critical event gets the next available slot')
        next_wave, block = waves.reserve(urgent.pk, now=sim.now)
        require(next_wave is not None and block is None, 'urgent event reserves ahead of older marketing')
        sim.evidence['priority'] = 'event_update'
        return
    raise ValueError('Unknown simulation scenario')
