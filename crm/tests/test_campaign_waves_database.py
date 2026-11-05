"""Wave acceptance against synthetic PostgreSQL data and an in-memory provider."""

import copy
import hashlib
import re
from collections import Counter
from datetime import datetime, timedelta
from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone

from crm import blast_email, blasts, listmonk, listmonk_sync
from crm.deliverability import day_close, guardian, planner, volume, waves
from crm.deliverability.streams import sync_streams
from crm.deliverability.wave_sync import record_smtp_feedback
from crm.models import Blast, BlastDelivery, BlastWave, Contact, CrmAuditEvent, DeliverabilityIncident, EmailEvent, MailDomainInfo, SendingStream, StreamDay
from crm.resend_feedback import record_event

from .api_fixtures import PREFIX, CrmApiMixin, make_event, register
from .test_blasts_database import FakeProviders, SENDING, ok


class WaveProvider(FakeProviders):
    """Exercise real HTTP adapters, including attributes and list-only removals."""

    def __init__(self):
        super().__init__()
        self.soft = {}
        self.starts = Counter()

    def route(self, method, path, params, body):
        if (method, path) == ('PUT', '/api/subscribers/lists') and body['action'] == 'remove':
            for subscriber_id in body['ids']:
                item = self.subscribers[subscriber_id]
                item['lists'] = [membership for membership in item['lists'] if membership['id'] not in body['target_list_ids']]
            return ok(True)
        match = re.fullmatch(r'/api/campaigns/(\d+)/status', path)
        if match and body['status'] == 'running':
            self.starts[int(match[1])] += 1
        result = super().route(method, path, params, body)
        if method == 'POST' and path == '/api/subscribers':
            self.subscriber(body['email'])['attribs'] = copy.deepcopy(body.get('attribs') or {})
        if method == 'PUT' and re.fullmatch(r'/api/subscribers/\d+', path):
            self.subscribers[int(path.rsplit('/', 1)[1])]['attribs'] = copy.deepcopy(body.get('attribs') or {})
        return result

    def emails_for(self, query):
        match = re.search(r'campaign_id = (\d+)', query)
        if match and "type = 'soft'" in query:
            return self.soft.get(int(match[1]), set())
        return super().emails_for(query)


WAVE_SETTINGS = {**SENDING, 'CRM_WAVES_ENABLED': True, 'CRM_RESEND_WEBHOOK_SECRET': 'synthetic-webhook-only',
    'CRM_DELIVERABILITY_SLACK_CHANNEL': '', 'CRM_DELIVERABILITY_OWNERS': ('admin@example.test',)}


@override_settings(**WAVE_SETTINGS)
class CampaignWaveTests(CrmApiMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.now = datetime(2026, 11, 9, 9, tzinfo=volume.MELBOURNE)
        self.fake = WaveProvider()
        self.enterContext(patch('integrations.http_client.request', self.fake))
        self.enterContext(patch('crm.sending_domains.is_verified', return_value=True))
        sync_streams()
        self.news = SendingStream.objects.get(key='news')
        self.news.mailbox.enabled = True
        self.news.mailbox.daily_limit = 1
        self.news.mailbox.save()
        cache.set('crm:deliverability:healthy', True, 3600)
        for domain, provider in (('google.test', 'google'), ('microsoft.test', 'microsoft')):
            MailDomainInfo.objects.create(domain=domain, provider=provider, mx=['mx.' + domain], has_mx=True, checked_at=timezone.now())

    def blast(self, **fields):
        return Blast.objects.create(stream=self.news, mailbox=self.news.mailbox, created_by=self.admin,
            channels=['email'], subject='Community news', body_markdown='Hello [MLAI](https://mlai.au).',
            segment_definition={'rules': [{'type': 'source', 'values': ['manual']}]},
            **{'purpose': 'marketing', 'status': 'draft', **fields})

    def contacts(self, count):
        offset = Contact.objects.count()
        return Contact.objects.bulk_create([Contact(email=f'person-{offset + n}@{"google.test" if n % 2 else "microsoft.test"}',
            marketing_consent='granted', marketing_consent_at=timezone.now(), source='manual') for n in range(count)])

    def queue(self, count=100, **fields):
        self.contacts(count)
        blast = self.blast(**fields)
        blasts.queue_blast(blast.pk, actor=self.admin, confirm={'email': count}, idempotency_key=str(blast.pk), now=self.now)
        blast.refresh_from_db()
        return blast

    def feedback(self, wave, *, bounce=0):
        # Bulk fixtures model the provider receipts. One real ingestion below
        # checks matching and state transitions through the signed-event service.
        rows = list(wave.deliveries.order_by('pk'))
        wave.deliveries.update(status='delivered', delivered_at=self.now)
        events = []
        for index, row in enumerate(rows):
            message = f'{wave.pk}:{index}'
            events.append(EmailEvent(source='resend', event_type='email.delivered', receipt=hashlib.sha256(message.encode()).hexdigest(),
                provider_message_id=message[:64], recipient=row.address, sender=self.news.mailbox.email, recipient_provider=row.provider,
                stream=self.news, blast=wave.blast, wave=wave, delivery=row, occurred_at=self.now))
        with patch('django.utils.timezone.now', return_value=self.now):
            EmailEvent.objects.bulk_create(events)
        if bounce:
            wave.deliveries.filter(pk__in=[row.pk for row in rows[:bounce]]).update(status='bounced', bounce_kind='hard')
        self.fake.campaigns[wave.listmonk_campaign_id]['status'] = 'finished'
        self.fake.campaigns[wave.listmonk_campaign_id]['sent'] = len(rows)

    def test_preview_and_api_are_read_only_and_hide_recipient_addresses(self):
        self.contacts(4)
        blast = self.blast()
        before = (BlastDelivery.objects.count(), CrmAuditEvent.objects.count(), Contact.objects.count(), Blast.objects.get(pk=blast.pk).plan)
        response = self.client.post(PREFIX + f'blasts/{blast.pk}/plan/', {}, format='json')
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()['people'], 4)
        self.assertIn('no-store', response['Cache-Control'])
        self.assertNotIn('person-', response.content.decode())
        self.assertEqual(before, (BlastDelivery.objects.count(), CrmAuditEvent.objects.count(), Contact.objects.count(), Blast.objects.get(pk=blast.pk).plan))
        self.assertEqual(self.member_client.post(PREFIX + f'blasts/{blast.pk}/plan/', {}).status_code, 403)

    def test_snapshot_can_span_days_and_idempotency_keeps_ranks(self):
        blast = self.queue(300)
        before = list(blast.deliveries.order_by('send_rank').values_list('contact_id', 'send_rank', 'provider', 'tier'))
        self.assertEqual(len(before), 300)
        self.assertEqual({row[3] for row in before}, {'A'})
        blasts.queue_blast(blast.pk, actor=self.admin, confirm={'email': 0}, idempotency_key=str(blast.pk), now=self.now)
        self.assertEqual(before, list(blast.deliveries.order_by('send_rank').values_list('contact_id', 'send_rank', 'provider', 'tier')))
        self.assertFalse(blast.waves.exists())
        self.assertEqual(blast.plan['people'], 300)

    def test_canary_reserves_stream_capacity_before_provider_calls(self):
        first = self.queue(200)
        wave, block = waves.reserve(first.pk, now=self.now)
        self.assertIsNone(block)
        self.assertEqual(wave.size, 50)
        self.assertEqual(volume.usage(self.news, self.now), (50, 50))
        second = self.blast(status='queued', queued_at=self.now + timedelta(seconds=1))
        BlastDelivery.objects.bulk_create([BlastDelivery(blast=second, contact=row.contact, channel='email', address=row.address,
            consent_basis='explicit', send_rank=row.send_rank, provider=row.provider) for row in first.deliveries.all()])
        self.assertEqual(waves.reserve(second.pk, now=self.now)[1], 'waiting_for_capacity')
        self.assertEqual(self.fake.campaigns, {})
        self.assertEqual(waves.reserve(first.pk, now=self.now)[0].pk, wave.pk)

    def test_start_uses_fixed_identity_headers_plain_part_and_current_consent_reason(self):
        blast = self.queue(60)
        outcome = waves.dispatch(blast.pk, now=self.now)
        wave = blast.waves.get()
        self.assertEqual(outcome['campaign'], 'running')
        payload = self.fake.campaigns[wave.listmonk_campaign_id]
        self.assertEqual(payload['name'], f'crm-blast-{blast.pk}-w0')
        self.assertTrue(payload['from_email'].startswith('MLAI <'))
        self.assertIn('https://mlai.au', payload['altbody'])
        self.assertIn({'X-MLAI-Wave': '0'}, payload['headers'])
        self.assertIn({'Feedback-ID': f'0:{str(blast.pk)[:8]}:news:mlai'}, payload['headers'])
        self.assertEqual(len(self.fake.on_list(wave.listmonk_list_id)), 50)
        for subscriber in self.fake.subscribers.values():
            self.assertIn('you opted in', subscriber['attribs']['consent_reason'])

    def test_canary_wait_and_feedback_gate_distinguish_quiet_completed_sends(self):
        blast = self.queue(100)
        waves.dispatch(blast.pk, now=self.now)
        wave = blast.waves.get()
        self.assertEqual(waves.canary_block(blast, self.now + timedelta(minutes=44)), 'checking_first_batch')
        self.assertIsNone(waves.canary_block(blast, self.now + timedelta(minutes=45)))
        self.assertEqual(waves.admission(blast, self.now + timedelta(hours=3)), 'waiting_for_feedback')
        self.feedback(wave)
        self.assertIsNone(waves.admission(blast, self.now + timedelta(hours=3)))

    def test_priority_reserves_for_an_upcoming_event_before_older_marketing(self):
        marketing = self.queue(100)
        event = make_event('priority-event', start=self.now + timedelta(hours=24))
        urgent = self.blast(purpose='event_update', event=event, status='queued', queued_at=self.now + timedelta(seconds=1))
        rows = list(marketing.deliveries.select_related('contact'))
        for row in rows:
            register(event, row.contact, registered_at=self.now)
        BlastDelivery.objects.bulk_create([BlastDelivery(blast=urgent, contact=row.contact, address=row.address,
            channel='email', consent_basis='event_registration', provider=row.provider, send_rank=row.send_rank) for row in rows])
        self.assertEqual(waves.reserve(marketing.pk, now=self.now)[1], 'earlier_campaign')
        self.assertEqual(blasts.claim_blasts(now=self.now)[0], urgent.pk)
        wave, block = waves.reserve(urgent.pk, now=self.now)
        self.assertIsNone(block)
        self.assertEqual(wave.size, 50)

    def test_provider_hold_keeps_google_ranks_and_allows_microsoft(self):
        blast = self.queue(100)
        DeliverabilityIncident.objects.create(stream=self.news, recipient_provider='google', signal='deferral_rate',
            severity='amber', value=.2, threshold=.2, sample=50, action='hold_provider', fingerprint='provider-hold',
            opened_at=self.now, last_detected_at=self.now)
        wave, block = waves.reserve(blast.pk, now=self.now)
        self.assertIsNone(block)
        self.assertEqual(set(wave.deliveries.values_list('provider', flat=True)), {'microsoft'})
        self.assertEqual(blast.deliveries.filter(provider='google', status='queued', wave__isnull=True).count(), 50)
        self.news.refresh_from_db()
        self.assertEqual(self.news.state, 'warming')

    def test_adopted_running_campaign_finishes_with_its_original_resources(self):
        self.contacts(2)
        blast = self.blast(status='sending', listmonk_list_id=12, listmonk_campaign_id=34)
        self.fake.lists[12] = blast_email.resource_name(blast.pk)
        self.fake.campaigns[34] = {'id': 34, 'name': blast_email.resource_name(blast.pk), 'status': 'running'}
        wave = BlastWave.objects.create(blast=blast, index=0, kind='canary', status='running', size=2,
            listmonk_list_id=12, listmonk_campaign_id=34, stats={'legacy_adopted': True})
        BlastDelivery.objects.bulk_create([BlastDelivery(blast=blast, wave=wave, contact=contact, channel='email',
            address=contact.email, consent_basis='explicit', status='submitting') for contact in Contact.objects.all()])
        with override_settings(CRM_WAVES_ENABLED=False):
            blasts.dispatch_blast(blast.pk, now=self.now)
            self.fake.campaigns[34]['status'] = 'finished'
            outcome = blasts.dispatch_blast(blast.pk, now=self.now + timedelta(minutes=1))
        self.assertEqual(outcome['status'], 'sent')
        self.assertEqual(blast.deliveries.filter(status='sent').count(), 2)
        self.assertEqual((len(self.fake.campaigns), len(self.fake.lists), sum(self.fake.starts.values())), (1, 1, 0))

    def test_adopted_finished_status_reconciles_rows_without_creating_any_resource(self):
        self.contacts(1)
        blast = self.blast(status='sending', listmonk_campaign_id=34)
        self.fake.campaigns[34] = {'id': 34, 'name': blast_email.resource_name(blast.pk), 'status': 'finished'}
        wave = BlastWave.objects.create(blast=blast, index=0, kind='canary', status='finished', size=1,
            listmonk_campaign_id=34, stats={'legacy_adopted': True})
        contact = Contact.objects.get()
        BlastDelivery.objects.create(blast=blast, wave=wave, contact=contact, channel='email',
            address=contact.email, consent_basis='explicit', status='submitting')
        listmonk_sync.sync_blast(blast, since=self.now, now=self.now)
        blast.refresh_from_db()
        self.assertEqual((blast.status, blast.deliveries.get().status), ('sent', 'sent'))
        self.assertEqual((len(self.fake.lists), len(self.fake.campaigns), sum(self.fake.starts.values())), (0, 1, 0))

    def test_legacy_adoption_does_not_apply_a_small_canarys_absolute_bounce_rule(self):
        blast = self.queue(1000)
        wave = BlastWave.objects.create(blast=blast, index=0, kind='canary', status='running', size=1000,
            started_at=self.now, stats={'legacy_adopted': True})
        blast.deliveries.update(wave=wave, status='sent')
        blast.deliveries.filter(pk__in=list(blast.deliveries.values_list('pk', flat=True)[:8])).update(status='bounced', bounce_kind='hard')
        cache.delete('crm:deliverability:guard')
        guardian.run_deliverability_guard(now=self.now)
        self.assertFalse(DeliverabilityIncident.objects.exists())

    def test_lost_admin_access_cancels_the_wave_after_the_refusal_commits(self):
        from roo.models import PointsAdmin
        blast = self.queue(100)
        waves.dispatch(blast.pk, now=self.now)
        PointsAdmin.objects.filter(user=self.admin).update(is_active=False)
        with self.captureOnCommitCallbacks(execute=True):
            outcome = blasts.dispatch_blast(blast.pk, now=self.now)
        self.assertEqual(outcome['status'], 'failed')
        self.assertEqual(blast.waves.get().status, 'cancelled')
        self.assertEqual(sum(self.fake.starts.values()), 1)

    def test_opt_out_between_load_and_start_is_removed_only_from_this_list(self):
        blast = self.queue(50)
        original = blast_email._load_batch
        withdrawn = blast.deliveries.order_by('send_rank').first().contact
        def load(*args, **kwargs):
            result = original(*args, **kwargs)
            Contact.objects.filter(pk=withdrawn.pk).update(marketing_consent='withdrawn')
            return result
        with patch('crm.blast_email._load_batch', side_effect=load):
            waves.dispatch(blast.pk, now=self.now)
        wave = blast.waves.get()
        self.assertNotIn(withdrawn.email, self.fake.on_list(wave.listmonk_list_id))
        self.assertEqual(blast.deliveries.get(contact=withdrawn).status, 'cancelled')
        self.assertEqual(self.fake.subscriber(withdrawn.email)['status'], 'enabled')
        self.assertEqual(wave.stats['handed'], 49)

    def test_lost_start_response_reconciles_existing_campaign_even_after_slot_changes(self):
        blast = self.queue(100)
        original = listmonk.set_campaign_status
        def start_and_timeout(campaign_id, status):
            original(campaign_id, status)
            raise listmonk.ListmonkError('timeout')
        with patch('crm.listmonk.set_campaign_status', side_effect=start_and_timeout):
            with self.assertRaises(listmonk.ListmonkError):
                waves.dispatch(blast.pk, now=self.now)
        wave = blast.waves.get()
        self.assertIsNone(wave.listmonk_campaign_id)
        self.assertEqual(wave.status, 'loading')
        self.assertEqual(len(self.fake.campaigns), 1)
        self.assertEqual(waves.dispatch(blast.pk, now=self.now + timedelta(hours=2))['campaign'], 'running')
        wave.refresh_from_db()
        self.assertEqual((blast.waves.count(), len(self.fake.campaigns), sum(self.fake.starts.values())), (1, 1, 1))
        self.assertEqual(wave.started_at, self.now)
        self.assertEqual(volume.usage(self.news, self.now + timedelta(hours=2))[0], 50)
        self.assertFalse(wave.deliveries.filter(status='queued').exists())

    def test_unstarted_draft_expires_without_losing_rank_or_reusing_its_campaign(self):
        blast = self.queue(100)
        wave, _ = waves.reserve(blast.pk, now=self.now)
        rank = list(wave.deliveries.order_by('send_rank').values_list('pk', 'send_rank'))
        with patch('crm.listmonk.set_campaign_status', side_effect=listmonk.ListmonkError('timeout')):
            with self.assertRaises(listmonk.ListmonkError):
                waves.advance(wave.pk, now=self.now)
        self.assertEqual(waves.advance(wave.pk, now=self.now + timedelta(hours=1))['blocked'], 'reservation_expired')
        wave.refresh_from_db()
        self.assertEqual(wave.status, 'cancelled')
        self.assertEqual(list(blast.deliveries.filter(pk__in=[pk for pk, _ in rank]).order_by('send_rank').values_list('pk', 'send_rank')), rank)
        self.assertEqual(volume.usage(self.news, self.now + timedelta(hours=1))[0], 0)
        waves.dispatch(blast.pk, now=self.now + timedelta(hours=1))
        self.assertEqual(blast.waves.count(), 2)
        self.assertEqual(len(self.fake.campaigns), 2)
        self.assertEqual(sum(self.fake.starts.values()), 1)

    def test_feedback_matches_old_snapshot_by_handoff_time(self):
        blast = self.queue(50)
        BlastDelivery.objects.filter(blast=blast).update(created_at=self.now - timedelta(days=20))
        waves.dispatch(blast.pk, now=self.now)
        row = blast.deliveries.first()
        with patch('django.utils.timezone.now', return_value=self.now):
            record_event({'type': 'email.delivered', 'sender': self.news.mailbox.email, 'recipient': row.address,
                'email_id': 'synthetic-old-snapshot', 'subject': blast.subject, 'test': False}, 'old-snapshot', now=self.now)
        event = EmailEvent.objects.get(provider_message_id='synthetic-old-snapshot')
        self.assertEqual((event.delivery_id, event.wave_id, event.recipient_provider), (row.pk, row.wave_id, row.provider))

    def test_pause_resume_uses_same_campaign_and_cannot_override_system_or_another_staff_pause(self):
        blast = self.queue(60)
        waves.dispatch(blast.pk, now=self.now)
        with self.captureOnCommitCallbacks(execute=True):
            waves.pause_blast(blast.pk, actor=self.admin, now=self.now)
        wave = blast.waves.get()
        self.assertEqual(wave.status, 'paused')
        other = self.chat_user('other-admin@example.test', 'e', role='committee')
        with self.assertRaises(blasts.BlastError) as caught:
            waves.pause_blast(blast.pk, actor=other, now=self.now, resume=True)
        self.assertEqual(caught.exception.code, 'pause_owner_required')
        with self.captureOnCommitCallbacks(execute=True):
            waves.pause_blast(blast.pk, actor=self.admin, now=self.now + timedelta(minutes=1), resume=True)
        wave.refresh_from_db()
        self.assertEqual((wave.status, len(self.fake.campaigns)), ('running', 1))
        Blast.objects.filter(pk=blast.pk).update(pause_state='system')
        with self.assertRaises(blasts.BlastError) as caught:
            waves.pause_blast(blast.pk, actor=self.admin, now=self.now, resume=True)
        self.assertEqual(caught.exception.code, 'owner_required')

    def test_cancel_stops_current_campaign_and_marks_untouched_rows_without_retrying(self):
        blast = self.queue(100)
        waves.dispatch(blast.pk, now=self.now)
        with self.captureOnCommitCallbacks(execute=True):
            blasts.cancel_blast(blast.pk, actor=self.admin, now=self.now)
        wave = blast.waves.get()
        self.assertEqual(wave.status, 'cancelled')
        self.assertEqual(blast.deliveries.filter(status='cancelled').count(), 50)
        self.assertEqual(blast.deliveries.filter(status='unknown').count(), 50)
        self.assertFalse(blast.deliveries.filter(status__in=('queued', 'submitting')).exists())
        blasts.dispatch_blast(blast.pk, now=self.now + timedelta(days=1))
        self.assertEqual(sum(self.fake.starts.values()), 1)

    def test_cancel_of_paused_campaign_never_resumes_it_to_satisfy_provider_status_rules(self):
        blast = self.queue(100)
        waves.dispatch(blast.pk, now=self.now)
        with self.captureOnCommitCallbacks(execute=True):
            waves.pause_blast(blast.pk, actor=self.admin, now=self.now)
        with self.captureOnCommitCallbacks(execute=True):
            blasts.cancel_blast(blast.pk, actor=self.admin, now=self.now)
        wave = blast.waves.get()
        self.assertEqual((wave.status, wave.stats['provider_retained_paused']), ('cancelled', True))
        self.assertEqual(self.fake.campaigns[wave.listmonk_campaign_id]['status'], 'paused')
        self.assertEqual(sum(self.fake.starts.values()), 1)
        listmonk_sync.sync_blast(blast, since=self.now, now=self.now + timedelta(hours=1))
        self.assertEqual(self.fake.campaigns[wave.listmonk_campaign_id]['status'], 'paused')
        self.assertEqual(sum(self.fake.starts.values()), 1)

    def test_external_provider_cancellation_never_retries_the_remaining_audience(self):
        blast = self.queue(100)
        waves.dispatch(blast.pk, now=self.now)
        wave = blast.waves.get()
        self.fake.campaigns[wave.listmonk_campaign_id]['status'] = 'cancelled'
        result = blasts.dispatch_blast(blast.pk, now=self.now)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(blast.deliveries.filter(status='unknown').count(), 50)
        self.assertEqual(blast.deliveries.filter(status='cancelled').count(), 50)
        blasts.dispatch_blast(blast.pk, now=self.now + timedelta(days=1))
        self.assertEqual(sum(self.fake.starts.values()), 1)

    def test_frequency_gap_is_deferred_then_cancelled_and_never_changes_consent(self):
        blast = self.queue(50)
        other = self.blast(status='sent', queued_at=self.now - timedelta(days=1))
        prior = BlastWave.objects.create(blast=other, index=0, kind='regular', status='finished', size=50,
            started_at=self.now - timedelta(days=1), finished_at=self.now)
        BlastDelivery.objects.bulk_create([BlastDelivery(blast=other, contact=row.contact, wave=prior, channel='email',
            address=row.address, consent_basis='explicit', status='sent') for row in blast.deliveries.all()])
        self.assertFalse(planner.eligible_rows(blast.deliveries.all(), blast, self.now).exists())
        waves.reserve(blast.pk, now=self.now)
        self.assertEqual(blast.deliveries.filter(status='queued').count(), 50)
        BlastWave.objects.filter(pk=prior.pk).update(started_at=self.now + timedelta(days=6))
        self.assertEqual(waves.reserve(blast.pk, now=self.now + timedelta(days=7))[1], 'waiting_for_capacity')
        self.assertEqual(blast.deliveries.filter(status='cancelled', error_code='frequency_cap').count(), 50)
        self.assertEqual(Contact.objects.filter(marketing_consent='granted').count(), 50)

    def test_canary_bounce_rate_opens_wave_incident_and_holds_only_news(self):
        blast = self.queue(100)
        waves.dispatch(blast.pk, now=self.now)
        wave = blast.waves.get()
        wave.deliveries.filter(pk__in=list(wave.deliveries.values_list('pk', flat=True)[:3])).update(status='bounced', bounce_kind='hard')
        cache.delete('crm:deliverability:guard')
        guardian.run_deliverability_guard(now=self.now)
        incident = DeliverabilityIncident.objects.get()
        self.assertEqual((incident.wave_id, incident.severity, incident.sample), (wave.pk, 'red', 50))
        self.news.refresh_from_db(); blast.refresh_from_db()
        self.assertEqual((self.news.state, blast.pause_state), ('held', 'system'))
        self.assertEqual(SendingStream.objects.get(key='notify').state, 'warming')
        self.assertEqual(self.fake.campaigns[wave.listmonk_campaign_id]['status'], 'paused')

    def test_listmonk_soft_bounces_are_deduplicated_and_suppress_only_after_three_blasts(self):
        blast = self.queue(1)
        contact = blast.deliveries.get().contact
        for index in range(3):
            if index:
                blast = self.blast(status='sending')
                BlastDelivery.objects.create(blast=blast, contact=contact, address=contact.email, channel='email', status='sent', consent_basis='explicit')
            wave = BlastWave.objects.create(blast=blast, index=0, kind='regular', status='finished', size=1, started_at=self.now)
            blast.deliveries.update(wave=wave)
            row = blast.deliveries.get()
            record_smtp_feedback(row, 'soft', self.now)
            record_smtp_feedback(row, 'soft', self.now)
            contact.refresh_from_db()
            self.assertEqual(contact.soft_bounce_streak, index + 1)
            self.assertEqual(contact.email_status, 'bounced' if index == 2 else 'active')
        self.assertEqual(EmailEvent.objects.filter(source='listmonk').count(), 3)

    def test_sync_scopes_engagement_and_tracked_urls_to_one_wave(self):
        blast = self.queue(100)
        waves.dispatch(blast.pk, now=self.now)
        first = blast.waves.get()
        self.feedback(first)
        listmonk_sync.sync_blast(blast, since=self.now - timedelta(minutes=1), now=self.now)
        later = self.now + timedelta(hours=3)
        waves.dispatch(blast.pk, now=later)
        second = blast.waves.get(index=1)
        url = 'https://mlai.au'
        self.fake.link_clicks[(second.listmonk_campaign_id, url)].add(first.deliveries.first().address)
        listmonk_sync.sync_engagement(blast, wave=second, since=self.now, now=later)
        self.assertFalse(blast.deliveries.filter(first_clicked_at__isnull=False).exists())
        recipient = second.deliveries.first()
        self.fake.link_clicks[(second.listmonk_campaign_id, url)].add(recipient.address)
        listmonk_sync.sync_engagement(blast, wave=second, since=self.now, now=later)
        recipient.refresh_from_db()
        self.assertEqual(recipient.clicked_urls, [url])
        self.assertEqual(blast.deliveries.filter(first_clicked_at__isnull=False).count(), 1)
        self.assertEqual(StreamDay.objects.get().delivered, 50)

    def test_disabled_waves_reconcile_existing_campaign_but_start_nothing_new(self):
        blast = self.queue(100)
        waves.dispatch(blast.pk, now=self.now)
        wave = blast.waves.get()
        self.feedback(wave)
        with override_settings(CRM_WAVES_ENABLED=False):
            self.assertEqual(waves.dispatch(blast.pk, now=self.now)['campaign'], 'finished')
            self.assertEqual(waves.dispatch(blast.pk, now=self.now + timedelta(hours=1))['blocked'], 'waves_disabled')
        self.assertEqual(len(self.fake.campaigns), 1)

    def test_hard_ceiling_is_checked_before_any_snapshot_or_provider_call(self):
        self.contacts(4)
        blast = self.blast()
        with override_settings(CRM_MAX_BLAST_RECIPIENTS=3), self.assertRaises(blasts.BlastError) as caught:
            blasts.queue_blast(blast.pk, actor=self.admin, confirm={'email': 4}, idempotency_key='over-limit', now=self.now)
        self.assertEqual(caught.exception.code, 'audience_too_large')
        self.assertFalse(blast.deliveries.exists())
        self.assertEqual(self.fake.calls, [])

    def test_test_send_owns_separate_resources_and_never_sets_wave_or_blast_campaign_ids(self):
        blast = self.blast()
        blast_email.send_test_email(blast.pk, self.admin)
        blast.refresh_from_db()
        self.assertIsNone(blast.listmonk_campaign_id)
        self.assertFalse(blast.waves.exists())
        campaign, = self.fake.campaigns.values()
        self.assertEqual(campaign['name'], f'crm-blast-{blast.pk}-test')

    def test_missing_feedback_never_promotes_and_day_close_is_idempotent(self):
        blast = self.queue(100)
        waves.dispatch(blast.pk, now=self.now)
        wave = blast.waves.get()
        wave.deliveries.update(status='delivered', delivered_at=self.now)
        row = StreamDay.objects.get()
        row.budget = 50; row.save()
        day_close.close_day(row.pk, now=self.now + timedelta(days=1))
        row.refresh_from_db(); self.news.refresh_from_db()
        self.assertIsNone(row.healthy)
        self.assertEqual(self.news.ramp_stage, 0)
        self.feedback(wave)
        day_close.close_day(row.pk, now=self.now + timedelta(days=1))
        self.news.refresh_from_db()
        self.assertEqual(self.news.ramp_stage, 0)
        self.assertEqual(CrmAuditEvent.objects.filter(target_type='stream_day', detail__operation='day_closed').count(), 1)

    def test_real_dispatcher_sends_5000_once_over_six_healthy_sending_days(self):
        blast = self.queue(5000)
        daily = Counter()
        for day in range(9):
            morning = datetime(2026, 11, 9, 9, tzinfo=volume.MELBOURNE) + timedelta(days=day)
            for row in StreamDay.objects.filter(day__lt=morning.date()):
                day_close.close_day(row.pk, now=morning)
            for quarter in range(28):
                self.now = morning + timedelta(minutes=quarter * 15)
                cache.set('crm:deliverability:healthy', True, 3600)
                blasts.dispatch_blast(blast.pk, now=self.now)
                running = list(blast.waves.filter(status='running'))
                for wave in running:
                    self.feedback(wave)
                    daily[morning.date().isoformat()] += wave.size
                if running:
                    cache.delete('crm:deliverability:guard')
                    self.assertNotEqual(guardian.run_deliverability_guard(now=self.now)['status'], 'deferred')
            blast.refresh_from_db()
            if blast.status == 'sent':
                break
        self.assertEqual(list(daily.values()), [150, 300, 600, 1000, 1500, 1450])
        self.assertEqual(blast.status, 'sent')
        self.assertEqual(blast.deliveries.filter(status='delivered').count(), 5000)
        self.assertEqual(sum(daily.values()), 5000)
        self.assertEqual(blast.waves.count(), len(self.fake.campaigns))
        self.assertTrue(all(n == 1 for n in self.fake.starts.values()))
        self.assertFalse(DeliverabilityIncident.objects.exists())
        self.assertEqual(len([day for day in daily if datetime.fromisoformat(day).weekday() >= 5]), 0)
        for day, count in daily.items():
            self.assertLessEqual(count, StreamDay.objects.get(day=day).budget)
        by_slot, caps = Counter(), {}
        first = blast.waves.order_by('index').first()
        for wave in blast.waves.all():
            slot = volume.window(self.news, wave.started_at)['slot'].isoformat()
            by_slot[slot] += wave.size
            caps[slot] = max(caps.get(slot, 0), wave.stats['hour_cap'])
            if wave.index:
                self.assertGreaterEqual(wave.started_at, first.started_at + timedelta(minutes=45))
        self.assertTrue(all(count <= caps[slot] for slot, count in by_slot.items()))
