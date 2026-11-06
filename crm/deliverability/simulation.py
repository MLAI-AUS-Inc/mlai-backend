"""Real campaign code with a fake clock/provider, only in an empty disposable test DB.

The command never migrates. Every synthetic row rolls back, and all unexpected
HTTP/socket access is forbidden before the first fixture is created.
"""

import copy
import hashlib
import itertools
import math
import os
import re
from collections import Counter
from contextlib import ExitStack
from datetime import datetime, timedelta
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.management.base import CommandError
from django.db import connection, transaction
from django.test.utils import override_settings

from crm import blasts, listmonk
from crm.models import (Blast, BlastDelivery, CrmEvent, DeliverabilityIncident, EmailEvent,
    MailDomainInfo, SendingStream, StreamDay)
from roo.models import PointsAdmin

from . import day_close, guardian, volume
from .streams import sync_streams

SCENARIOS = ('healthy', 'bounce_spike_canary', 'complaints_creep', 'outlook_deferrals',
    'gmail_block_codes', 'webhook_outage', 'dns_drift', 'listmonk_down',
    'postmaster_spam_high', 'staff_pause_resume', 'event_update_priority', 'idle_decay')


def require(condition, description):
    """Invariant checks remain active under python -O."""
    if not condition:
        raise CommandError('Simulation failed: ' + description)


def disposable_database():
    """Require an explicit exact-name opt-in and empty CRM; no connection is inferred safe."""
    name = str(connection.settings_dict.get('NAME', ''))
    if (settings.APP_ENV != 'test' or connection.vendor != 'postgresql'
            or not name.startswith('test_') or os.environ.get('CRM_SIMULATION_DATABASE') != name):
        raise CommandError('Requires APP_ENV=test and CRM_SIMULATION_DATABASE matching an isolated test_* PostgreSQL database.')
    if Blast.objects.exists() or BlastDelivery.objects.exists() or EmailEvent.objects.exists():
        raise CommandError('Simulation requires an empty CRM database.')
    from crm.models import Contact, SeedMailbox
    if Contact.objects.exists() or SendingStream.objects.exists() or SeedMailbox.objects.exists() or CrmEvent.objects.exists():
        raise CommandError('Simulation requires an empty CRM database.')


class FakeListmonk:
    """Only the adapter operations the real dispatcher needs; no sending transport."""

    def __init__(self):
        self.ids = itertools.count(100)
        self.lists, self.campaigns, self.subscribers = {}, {}, {}
        self.initial_starts = Counter()
        self.down = False

    def install(self, stack):
        for name in ('find_list', 'create_list', 'find_campaign', 'create_campaign', 'get_campaign',
                'update_campaign', 'set_campaign_status', 'query_subscribers', 'create_subscriber',
                'update_subscriber', 'add_subscribers_to_list', 'remove_subscribers_from_list'):
            stack.enter_context(patch.object(listmonk, name, getattr(self, name)))

    def available(self):
        if self.down:
            raise listmonk.ListmonkError('unavailable', status_code=503)

    def find_list(self, name):
        self.available()
        return next((key for key, value in self.lists.items() if value == name), None)

    def create_list(self, name, **kwargs):
        self.available()
        key = next(self.ids); self.lists[key] = name
        return key

    def find_campaign(self, name):
        self.available()
        return next((copy.deepcopy(value) for value in self.campaigns.values() if value['name'] == name), None)

    def create_campaign(self, payload):
        self.available()
        key = next(self.ids)
        self.campaigns[key] = {**copy.deepcopy(payload), 'id': key, 'status': 'draft', 'sent': 0}
        return copy.deepcopy(self.campaigns[key])

    def get_campaign(self, key):
        self.available()
        return copy.deepcopy(self.campaigns[key])

    def update_campaign(self, key, payload):
        self.available()
        require(self.campaigns[key]['status'] == 'draft', 'provider only edits draft campaigns')
        self.campaigns[key].update(copy.deepcopy(payload))
        return self.get_campaign(key)

    def set_campaign_status(self, key, status):
        self.available()
        previous = self.campaigns[key]['status']
        allowed = {'running': ('draft', 'paused'), 'paused': ('running',), 'cancelled': ('running',)}
        require(previous in allowed.get(status, ()), 'provider status transition')
        if status == 'running' and previous == 'draft':
            self.initial_starts[key] += 1
        self.campaigns[key]['status'] = status
        return self.get_campaign(key)

    def query_subscribers(self, query, **kwargs):
        self.available()
        ids = re.search(r'subscribers.id IN \(([\d,]+)\)', query)
        wanted = {int(value) for value in ids[1].split(',')} if ids else set()
        emails = {value.replace("''", "'") for value in re.findall(r"'((?:[^']|'')*)'", query)}
        return [copy.deepcopy(row) for key, row in self.subscribers.items() if key in wanted or row['email'] in emails]

    def create_subscriber(self, email, name, *, lists=(), attribs=None):
        self.available()
        require(not any(row['email'] == email for row in self.subscribers.values()), 'unique provider subscriber')
        key = next(self.ids)
        self.subscribers[key] = {'id': key, 'email': email, 'name': name, 'status': 'enabled',
            'attribs': copy.deepcopy(attribs or {}), 'lists': [{'id': n, 'subscription_status': 'confirmed'} for n in lists]}
        return copy.deepcopy(self.subscribers[key])

    def update_subscriber(self, key, payload):
        self.available()
        row = self.subscribers[key]
        row.update({k: copy.deepcopy(v) for k, v in payload.items() if k != 'lists'})
        row['lists'] = [{'id': n, 'subscription_status': 'confirmed'} for n in payload.get('lists', [])]
        return copy.deepcopy(row)

    def add_subscribers_to_list(self, keys, list_id):
        self.available()
        for key in keys:
            if not any(row['id'] == list_id for row in self.subscribers[key]['lists']):
                self.subscribers[key]['lists'].append({'id': list_id, 'subscription_status': 'confirmed'})

    def remove_subscribers_from_list(self, keys, list_id):
        self.available()
        for key in keys:
            self.subscribers[key]['lists'] = [row for row in self.subscribers[key]['lists'] if row['id'] != list_id]


class Simulation:
    """Synthetic fixtures and clock driving the actual planner, dispatch and guardian."""

    def __init__(self, recipients):
        self.now = datetime(2026, 11, 9, 9, tzinfo=volume.MELBOURNE)
        self.recipients = recipients
        self.provider = FakeListmonk()
        self.daily = Counter()
        self.completed = set()
        self.evidence = {}

    def fixtures(self):
        from crm.models import Contact
        self.actor = get_user_model().objects.create_user('simulation-owner@example.test')
        PointsAdmin.objects.create(user=self.actor, slack_user_id='USIMULATE', role='committee', is_active=True)
        sync_streams()
        self.stream = SendingStream.objects.get(key='news')
        self.stream.mailbox.enabled = True; self.stream.mailbox.save()
        for provider in ('google', 'microsoft'):
            MailDomainInfo.objects.create(domain=provider + '.simulation.test', provider=provider,
                mx=['mx.' + provider + '.simulation.test'], has_mx=True, checked_at=self.now)
        Contact.objects.bulk_create([Contact(email=f'person-{n}@{"google" if n % 2 else "microsoft"}.simulation.test',
            source='manual', marketing_consent='granted', marketing_consent_at=self.now) for n in range(self.recipients)])
        self.blast = self.queue()
        # Disposable transaction fixtures are invisible to autovacuum.
        with connection.cursor() as cursor:
            cursor.execute('ANALYZE crm_contact, crm_blastdelivery')

    def queue(self, *, event=None):
        blast = Blast.objects.create(created_by=self.actor, stream=self.stream, mailbox=self.stream.mailbox,
            purpose='event_update' if event else 'marketing', event=event, channels=['email'], subject='Synthetic community news',
            body_markdown='Synthetic fixture for the campaign simulator.',
            segment_definition={'rules': [{'type': 'source', 'values': ['manual']}]})
        cache.set('crm:deliverability:healthy', True, 3600)
        blasts.queue_blast(blast.pk, actor=self.actor, confirm={'email': self.recipients},
            idempotency_key=str(blast.pk), now=self.now)
        blast.refresh_from_db()
        return blast

    def event(self, row, kind, **fields):
        identity = f'{row.pk}:{kind}'
        return EmailEvent.objects.create(source='resend', event_type=kind,
            receipt=hashlib.sha256(identity.encode()).hexdigest(), provider_message_id=str(row.pk), recipient=row.address,
            sender=self.stream.mailbox.email, stream=self.stream, blast=row.blast, wave=row.wave,
            delivery=row, recipient_provider=row.provider, occurred_at=self.now, **fields)

    def feedback(self, wave, *, defer_microsoft=False):
        rows = list(wave.deliveries.order_by('send_rank'))
        microsoft = [row for row in rows if row.provider == 'microsoft']
        delayed = microsoft[:math.ceil(len(microsoft) * 0.1)] if defer_microsoft else []
        delayed_ids = [row.pk for row in delayed]
        wave.deliveries.update(status='delivered', delivered_at=self.now)
        if delayed_ids:
            wave.deliveries.filter(pk__in=delayed_ids).update(status='sent', delivered_at=None)
        EmailEvent.objects.bulk_create([EmailEvent(source='resend', event_type='email.delivered',
            receipt=hashlib.sha256(f'{row.pk}:delivered'.encode()).hexdigest(), provider_message_id=str(row.pk),
            recipient=row.address, sender=self.stream.mailbox.email, stream=self.stream, blast=wave.blast, wave=wave,
            delivery=row, recipient_provider=row.provider, occurred_at=self.now) for row in rows if row.pk not in delayed_ids])
        for row in delayed:
            self.event(row, 'email.delivery_delayed')
        if delayed:
            self.evidence['microsoft_deferred'] = len(delayed)
            self.evidence['microsoft_attempted'] = len(microsoft)
        self.provider.campaigns[wave.listmonk_campaign_id].update(status='finished', sent=len(rows))
        self.completed.add(wave.pk)
        self.daily[self.now.date().isoformat()] += wave.size
        return rows

    def guard(self):
        cache.delete('crm:deliverability:guard')
        result = guardian.run_deliverability_guard(now=self.now)
        require(result['status'] == 'completed', 'guardian completes: ' + str(result))
        return result

    def dispatch(self, *, feedback=True):
        cache.set('crm:deliverability:healthy', True, 3600)
        result = blasts.dispatch_blast(self.blast.pk, now=self.now)
        if feedback:
            for wave in self.blast.waves.filter(status='running'):
                if wave.pk not in self.completed:
                    self.feedback(wave)
        return result

    def first(self, *, feedback=True):
        self.dispatch(feedback=feedback)
        return self.blast.waves.get()

    def finish(self, days, *, outlook=False):
        injected = False
        start = self.now.replace(hour=9, minute=0)
        for offset in range(days + 4):
            morning = start + timedelta(days=offset)
            if morning.weekday() >= 5:
                continue
            if len(self.daily) >= days:
                break
            for row in StreamDay.objects.filter(day__lt=morning.date()):
                day_close.close_day(row.pk, now=morning)
            for quarter in range(28):
                self.now = morning + timedelta(minutes=15 * quarter)
                self.dispatch(feedback=False)
                observed = False
                for wave in self.blast.waves.filter(status='running'):
                    if wave.pk in self.completed:
                        continue
                    defer = outlook and not injected and len(self.daily) == 4 and wave.deliveries.filter(provider='microsoft').count() >= 50
                    self.feedback(wave, defer_microsoft=defer)
                    observed = True
                    if defer:
                        injected = True
                if observed or DeliverabilityIncident.objects.filter(state='open').exists():
                    self.guard()
                self.blast.refresh_from_db()
                if self.blast.status == 'sent':
                    break
            # Final callbacks account for the temporary provider delays.
            if injected:
                pending = self.blast.deliveries.filter(status='sent')
                for row in pending:
                    self.event(row, 'email.delivered')
                pending.update(status='delivered', delivered_at=self.now)
            if self.blast.status == 'sent':
                break
        require(self.blast.status == 'sent', 'audience finishes within the requested sending days')
        require(self.blast.deliveries.filter(status='delivered').count() == self.recipients, 'exact final delivery count')
        if not outlook and self.recipients == 5000:
            require(list(self.daily.values()) == [150, 300, 600, 1000, 1500, 1450], 'six-day healthy ramp')
        if outlook:
            require(injected and DeliverabilityIncident.objects.filter(recipient_provider='microsoft', action='throttle_provider').exists(), 'Microsoft-only throttle')
            require(not DeliverabilityIncident.objects.filter(recipient_provider='google').exists(), 'Google continues without a throttle')
            if self.recipients == 5000:
                require(len(self.daily) == 7, 'Microsoft delay adds one sending day')

    def verify_caps(self):
        daily, hourly, caps = Counter(), Counter(), {}
        for wave in self.blast.waves.filter(started_at__isnull=False):
            day = wave.started_at.astimezone(volume.MELBOURNE).date().isoformat()
            slot = wave.started_at.astimezone(volume.MELBOURNE).replace(minute=0, second=0, microsecond=0).isoformat()
            daily[day] += wave.size; hourly[slot] += wave.size
            caps[slot] = max(caps.get(slot, 0), wave.stats['hour_cap'])
        for day, count in daily.items():
            require(count <= StreamDay.objects.get(stream=self.stream, day=day).budget, 'daily budget respected')
        require(all(count <= caps[slot] for slot, count in hourly.items()), 'hourly caps respected')
        require(all(count == 1 for count in self.provider.initial_starts.values()), 'each provider campaign starts once')
        require(self.blast.deliveries.values('contact_id').distinct().count() == self.recipients, 'one delivery per person')
        return dict(daily)


def run(scenario, *, recipients=5000, days=8):
    """Return aggregate assertions after rolling back all synthetic database work."""
    require(scenario in SCENARIOS and 50 <= recipients <= 10000 and 1 <= days <= 30, 'bounded valid arguments')
    disposable_database()
    simulation = Simulation(recipients)
    isolated = {'APP_ENV': 'test', 'COMMUNITY_CHAT_CRM_ENABLED': True, 'CRM_SENDING_ENABLED': True,
        'CRM_WAVES_ENABLED': True, 'CRM_PREFLIGHT_ENABLED': False, 'CRM_GUARDIAN_ENABLED': True,
        'CRM_GUARDIAN_DRY_RUN': False, 'CRM_MONITORS_ENABLED': True, 'CRM_SEED_HEARTBEAT_ENABLED': False,
        'CRM_DELIVERABILITY_EMAIL_ALERTS_ENABLED': False, 'CRM_DELIVERABILITY_SLACK_CHANNEL': '',
        'CRM_DELIVERABILITY_OWNERS': ('simulation-owner@example.test',), 'CRM_LUMA_SYNC_ENABLED': False,
        'CRM_SMS_ENABLED': False, 'CRM_WHATSAPP_ENABLED': False,
        'CRM_BLACKOUT_DATES': (),
        'CRM_RESEND_WEBHOOK_SECRET': 'synthetic-simulation-only', 'LISTMONK_BASE_URL': 'http://simulation.invalid',
        'LISTMONK_API_USER': 'synthetic', 'LISTMONK_API_TOKEN': 'synthetic', 'LISTMONK_TEMPLATE_ID': 3,
        'CACHES': {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache', 'LOCATION': 'campaign-simulation'}}}
    with ExitStack() as stack:
        stack.enter_context(override_settings(**isolated))
        simulation.provider.install(stack)
        stack.enter_context(patch('django.utils.timezone.now', lambda: simulation.now))
        stack.enter_context(patch('crm.sending_domains.is_verified', return_value=True))
        forbidden = AssertionError('Outbound network forbidden in simulation')
        stack.enter_context(patch('integrations.http_client.request', side_effect=forbidden))
        stack.enter_context(patch('socket.socket.connect', side_effect=forbidden))
        stack.enter_context(patch('socket.create_connection', side_effect=forbidden))
        with transaction.atomic():
            simulation.fixtures()
            from .simulation_scenarios import exercise
            exercise(simulation, scenario, days)
            daily = simulation.verify_caps()
            result = {'scenario': scenario, 'status': 'passed', 'recipients': recipients,
                'daily_handoffs': daily, 'initial_campaign_starts': sum(simulation.provider.initial_starts.values()),
                'incidents': list(DeliverabilityIncident.objects.values('signal', 'severity', 'action', 'recipient_provider', 'state')),
                'evidence': simulation.evidence, 'rows_persisted': 0, 'network': 'forbidden'}
            transaction.set_rollback(True)
    return result
