"""Owner preference-check planning, capacity and explicit signed confirmation."""

from datetime import datetime, timedelta
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from django.core.cache import cache
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from crm import blasts
from crm.blasts import BlastError
from crm.deliverability import planner, reconfirmation, volume
from crm.deliverability.streams import sync_streams
from crm.models import Blast, BlastDelivery, BlastWave, Contact, ContactConsentEvent, MailDomainInfo, SendingStream

from .api_fixtures import PREFIX, CrmApiMixin
from .test_campaign_waves_database import WAVE_SETTINGS, WaveProvider


@override_settings(**{**WAVE_SETTINGS, 'CRM_LIFECYCLE_ENABLED': True, 'CRM_PREFLIGHT_ENABLED': False})
class ReconfirmationDatabaseTests(CrmApiMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.now = timezone.now()
        self.fake = WaveProvider()
        self.enterContext(patch('integrations.http_client.request', self.fake))
        self.enterContext(patch('crm.sending_domains.is_verified', return_value=True))
        sync_streams()
        self.news = SendingStream.objects.get(key='news')
        self.news.state, self.news.ramp_stage, self.news.daily_budget_override = 'active', 12, 500
        self.news.save()
        self.news.mailbox.enabled = True; self.news.mailbox.save()
        MailDomainInfo.objects.create(domain='example.test', provider='google', has_mx=True, mx=['mx.example.test'], checked_at=self.now)

    def person(self, local='dormant', **fields):
        return Contact.objects.create(**{'email': local + '@example.test', 'source': 'manual',
            'marketing_consent': 'granted', 'marketing_consent_at': self.now - timedelta(days=700), **fields})

    def submitted(self, person=None):
        person = person or self.person()
        draft = reconfirmation.prepare_draft('news', actor=self.admin, now=self.now)
        draft.status = 'sending'; draft.save()
        wave = BlastWave.objects.create(blast=draft, index=0, kind='canary', status='running', size=1,
            started_at=self.now - timedelta(seconds=1), reserved_at=self.now - timedelta(seconds=1))
        delivery = BlastDelivery.objects.create(blast=draft, wave=wave, contact=person, channel='email',
            address=person.email, consent_basis='explicit', status='sent')
        url = reconfirmation.url_for(delivery, now=self.now)
        reconfirmation.handed_off(wave, now=self.now)
        return person, delivery, parse_qs(urlsplit(url).query)['token'][0]

    def test_owner_prepares_one_draft_without_queueing_and_staff_are_refused(self):
        first = self.client.post(PREFIX + 'deliverability/reconfirmation/', {'stream': 'news'}, format='json')
        second = self.client.post(PREFIX + 'deliverability/reconfirmation/', {'stream': 'news'}, format='json')
        self.assertEqual((first.status_code, second.status_code), (201, 201))
        self.assertEqual(first.json()['id'], second.json()['id'])
        self.assertEqual(first.json()['lifecycle_kind'], 'reconfirmation')
        self.assertEqual(BlastDelivery.objects.count(), 0)
        staff = self.chat_user('staff@example.test', 'x', role='committee')
        response = self.account_client(staff).post(PREFIX + 'deliverability/reconfirmation/', {'stream': 'news'}, format='json')
        self.assertEqual(response.status_code, 403)
        with self.assertRaises(BlastError) as refused:
            blasts.queue_blast(first.json()['id'], actor=staff, confirm={'email': 0}, idempotency_key='staff-attempt', now=self.now)
        self.assertEqual(refused.exception.code, 'owner_required')

    def test_plan_selects_only_dormant_recently_consented_never_hard_bounced_people(self):
        wanted = self.person()
        self.person('active', last_active_at=self.now)
        self.person('stale', marketing_consent_at=self.now - timedelta(days=1200))
        bounced = self.person('previous-bounce')
        ContactConsentEvent.objects.create(contact=bounced, channel='email', kind='bounced', source='synthetic', occurred_at=self.now - timedelta(days=20))
        draft = reconfirmation.prepare_draft('news', actor=self.admin, now=self.now)
        prepared = planner.prepare(draft, self.now)
        self.assertEqual([row.contact.pk for row in prepared.recipients], [wanted.pk])
        self.assertEqual(prepared.summary['tiers'], {'D': 1})
        self.assertEqual(prepared.excluded['previous_hard_bounce'], 1)
        self.assertEqual(prepared.excluded['consent_too_old'], 1)

    def test_composer_save_preserves_the_fixed_audience_and_staff_cannot_change_it(self):
        draft = reconfirmation.prepare_draft('news', actor=self.admin, now=self.now)
        payload = {'purpose': 'marketing', 'channels': ['email'], 'event_id': None, 'segment_id': None,
            'quick_segment': None, 'audience_preset': None, 'email': {'subject': 'A preference check'}}
        response = self.client.patch(PREFIX + f'blasts/{draft.pk}/', payload, format='json')
        self.assertEqual(response.status_code, 200, response.content)
        draft.refresh_from_db()
        self.assertEqual(draft.segment_definition, {'match': 'all', 'rules': [], 'exclude': []})
        self.assertTrue(reconfirmation.is_reconfirmation(draft))
        response = self.client.patch(PREFIX + f'blasts/{draft.pk}/', {**payload, 'audience_preset': 'opted_in'}, format='json')
        self.assertEqual(response.status_code, 400)

    def test_test_message_links_are_read_only_previews(self):
        from crm.deliverability.preflight.render import preview_payload
        draft = reconfirmation.prepare_draft('news', actor=self.admin, now=self.now)
        payload = preview_payload(draft, 123, 'synthetic-preview')
        self.assertNotIn(reconfirmation.GO_URL, payload['body'])
        self.assertIn('?preview=1', payload['body'])
        self.assertEqual(Client().get(reconfirmation.PATH, {'preview': '1'}).status_code, 200)
        self.assertEqual(ContactConsentEvent.objects.count(), 0)

    def test_get_and_scanners_do_not_confirm_and_post_requires_csrf(self):
        person, delivery, token = self.submitted()
        client = Client(enforce_csrf_checks=True)
        response = client.get(reconfirmation.PATH, {'token': token}, secure=True)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Yes, keep me subscribed')
        self.assertNotContains(response, person.email)
        person.refresh_from_db(); self.assertIsNone(person.consent_reconfirmed_at)
        self.assertEqual(client.post(reconfirmation.PATH, {'token': token}, secure=True).status_code, 403)
        response = client.post(reconfirmation.PATH, {'token': token, 'csrfmiddlewaretoken': client.cookies['csrftoken'].value},
            secure=True, HTTP_ORIGIN='https://testserver')
        self.assertEqual(response.status_code, 200)
        person.refresh_from_db(); self.assertIsNotNone(person.consent_reconfirmed_at)
        self.assertEqual(person.marketing_consent_at, self.now - timedelta(days=700))
        self.assertEqual(person.marketing_consent, 'granted')
        self.assertEqual(reconfirmation.confirm(token, now=timezone.now(), apply=True), 'already_confirmed')
        self.assertEqual(ContactConsentEvent.objects.filter(source='signed_reconfirmation').count(), 1)

    def test_confirmation_lifts_only_dormant_and_preserves_an_unrelated_review_hold(self):
        person, _, token = self.submitted()
        person.refresh_from_db()
        person.attributes.update(dormant_suppressed=True, marketing_review_hold={'reason': 'retained'})
        person.save()
        self.assertEqual(reconfirmation.confirm(token, now=self.now, apply=True), 'confirmed')
        person.refresh_from_db()
        self.assertNotIn('dormant_suppressed', person.attributes)
        self.assertEqual(person.attributes['marketing_review_hold'], {'reason': 'retained'})

    def test_expired_tampered_withdrawn_or_new_bounce_links_cannot_change_preferences(self):
        person, _, token = self.submitted()
        with self.assertRaises(BlastError): reconfirmation.confirm(token + 'x', now=self.now, apply=True)
        with self.assertRaises(BlastError): reconfirmation.confirm(token, now=self.now + timedelta(days=31), apply=True)
        person.marketing_consent = 'withdrawn'; person.save()
        with self.assertRaises(BlastError): reconfirmation.confirm(token, now=self.now, apply=True)
        person.marketing_consent = 'granted'; person.save()
        ContactConsentEvent.objects.create(contact=person, channel='email', kind='bounced', source='synthetic', occurred_at=self.now)
        with self.assertRaises(BlastError): reconfirmation.confirm(token, now=self.now, apply=True)
        person.refresh_from_db(); self.assertIsNone(person.consent_reconfirmed_at)

    def test_real_dispatcher_uses_personal_links_and_ten_percent_shared_daily_cap(self):
        self.now = datetime(2026, 11, 9, 9, tzinfo=volume.MELBOURNE)
        for n in range(100): self.person(f'person-{n}')
        draft = reconfirmation.prepare_draft('news', actor=self.admin, now=self.now)
        cache.set('crm:deliverability:healthy', True, 3600)
        blasts.queue_blast(draft.pk, actor=self.admin, confirm={'email': 100}, idempotency_key=str(draft.pk), now=self.now)
        with patch('django.utils.timezone.now', lambda: self.now):
            blasts.dispatch_blast(draft.pk, now=self.now)
            wave = draft.waves.get()
            self.assertEqual(wave.size, 50)
            self.assertTrue(all(row['attribs'].get(reconfirmation.ATTRIBUTE) for row in self.fake.subscribers.values()))
            wave.deliveries.update(status='delivered', delivered_at=self.now)
            self.fake.campaigns[wave.listmonk_campaign_id]['status'] = 'finished'
            self.now += timedelta(hours=1)
            blasts.dispatch_blast(draft.pk, now=self.now)
            self.assertEqual(draft.waves.count(), 1)
            self.assertEqual(reconfirmation.daily_used(self.news, self.now), 50)
        self.assertEqual(set(draft.deliveries.values_list('tier', flat=True)), {''})
        draft.refresh_from_db(); self.assertEqual(draft.plan['tiers'], {'D': 100})
        self.assertIn(reconfirmation.GO_URL, self.fake.campaigns[wave.listmonk_campaign_id]['body'])

    def test_inactive_stream_and_small_capacity_cannot_start_reconfirmation(self):
        self.news.state = 'warming'; self.news.save()
        with self.assertRaises(BlastError): reconfirmation.prepare_draft('news', actor=self.admin, now=self.now)
        self.news.state, self.news.daily_budget_override = 'active', 100; self.news.save()
        draft = reconfirmation.prepare_draft('news', actor=self.admin, now=self.now)
        self.assertEqual(reconfirmation.admission(draft), 'reconfirmation_capacity_low')
