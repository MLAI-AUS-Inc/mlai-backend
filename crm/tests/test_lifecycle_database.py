"""Activity, dormant maintenance and provider/cohort safeguards using synthetic rows."""

import uuid
from datetime import timedelta
from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone

from core.models import User
from crm.deliverability import lifecycle, lifecycle_guard, waves
from crm.deliverability.streams import sync_streams
from crm.models import (Blast, BlastDelivery, BlastWave, Contact, ContactConsentEvent, CrmEvent,
    EventRegistration, InboundMessage, SendingStream, StreamDay)


@override_settings(CRM_LIFECYCLE_ENABLED=True, CRM_GUARDIAN_DRY_RUN=False)
class LifecycleDatabaseTests(TestCase):
    def setUp(self):
        cache.clear()
        self.now = timezone.now()
        self.enterContext(patch('integrations.http_client.request', side_effect=AssertionError('Network forbidden')))
        sync_streams()
        self.news = SendingStream.objects.get(key='news')
        self.blast = Blast.objects.create(stream=self.news, mailbox=self.news.mailbox, purpose='marketing',
            channels=['email'], status='sending', queued_at=self.now - timedelta(days=2), subject='Synthetic news')

    def contact(self, **values):
        return Contact.objects.create(**{'email': str(uuid.uuid4()) + '@example.test', 'source': 'manual', **values})

    def test_activity_includes_dated_evidence_and_ignores_opens_and_future_dates(self):
        user = User.objects.create_user('activity-user@example.test')
        user.last_login = self.now - timedelta(days=10); user.save()
        person = self.contact(user=user, last_attended_at=self.now - timedelta(days=9), attributes={
            'engagement_newsletter': {'source': 'substack_export', 'exported_at': self.now.isoformat(),
                'last_clicked_at': (self.now - timedelta(hours=1)).isoformat()}})
        event = CrmEvent.objects.create(luma_event_id='activity', name='Synthetic event', start_at=self.now)
        EventRegistration.objects.create(contact=person, event=event, registered_at=self.now - timedelta(days=8))
        BlastDelivery.objects.create(blast=self.blast, contact=person, channel='email', address=person.email,
            status='delivered', first_clicked_at=self.now - timedelta(days=5), first_opened_at=self.now)
        InboundMessage.objects.create(contact=person, channel='email', from_address='synthetic', provider_id='reply-past',
            received_at=self.now - timedelta(days=3))
        InboundMessage.objects.create(contact=person, channel='email', from_address='synthetic', provider_id='reply-future',
            received_at=self.now + timedelta(days=3))
        self.assertEqual(lifecycle.run_lifecycle_refresh(now=self.now)['status'], 'complete')
        person.refresh_from_db()
        self.assertEqual(person.last_active_at, self.now - timedelta(hours=1))
        self.assertEqual(person.email_domain, 'example.test')
        self.assertEqual(ContactConsentEvent.objects.count(), 0)
        self.assertEqual(lifecycle.run_lifecycle_refresh(now=self.now)['changed'], 0)

    def test_undated_newsletter_counts_and_recent_opens_do_not_create_activity(self):
        person = self.contact(attributes={'engagement_import': {'source_columns': {'Total clicks': '10',
            'Last opened at': self.now.isoformat()}}})
        BlastDelivery.objects.create(blast=self.blast, contact=person, channel='email', address=person.email,
            status='delivered', first_opened_at=self.now)
        lifecycle.run_lifecycle_refresh(now=self.now)
        person.refresh_from_db(); self.assertIsNone(person.last_active_at)

    def test_thirty_day_no_answer_preserves_preferences_suppressions_and_other_holds(self):
        attrs = {'crm_lifecycle': {'reconfirmation_sent_at': (self.now - timedelta(days=31)).isoformat()},
            'marketing_review_hold': {'reason': 'original_review'}, 'retained': 'value'}
        person = self.contact(attributes=attrs, marketing_consent='withdrawn', email_status='bounced',
            last_active_at=self.now - timedelta(days=500))
        suppression = ContactConsentEvent.objects.create(contact=person, channel='email', kind='bounced', source='synthetic', occurred_at=self.now - timedelta(days=50))
        lifecycle.run_lifecycle_refresh(now=self.now)
        person.refresh_from_db()
        self.assertTrue(person.attributes['dormant_suppressed'])
        self.assertEqual(person.attributes['marketing_review_hold'], attrs['marketing_review_hold'])
        self.assertEqual((person.email_status, person.marketing_consent), ('bounced', 'withdrawn'))
        self.assertTrue(ContactConsentEvent.objects.filter(pk=suppression.pk, kind='bounced').exists())
        self.assertEqual(ContactConsentEvent.objects.count(), 1)

    def test_an_observed_login_lifts_only_the_dormant_flag(self):
        user = User.objects.create_user('returned-user@example.test')
        user.last_login = self.now; user.save()
        person = self.contact(user=user, attributes={'dormant_suppressed': True, 'marketing_review_hold': True,
            'crm_lifecycle': {'reconfirmation_sent_at': (self.now - timedelta(days=40)).isoformat()}})
        lifecycle.run_lifecycle_refresh(now=self.now)
        person.refresh_from_db()
        self.assertNotIn('dormant_suppressed', person.attributes)
        self.assertIs(person.attributes['marketing_review_hold'], True)
        self.assertEqual(person.marketing_consent, 'unknown')

    def test_foreign_cache_lock_is_never_released(self):
        cache.set('crm:lifecycle:lock', 'another-worker', 300)
        self.assertEqual(lifecycle.run_lifecycle_refresh(now=self.now), {'status': 'throttled'})
        self.assertEqual(cache.get('crm:lifecycle:lock'), 'another-worker')

    def test_cohort_quarantine_changes_only_untouched_unheld_people(self):
        import_id = str(uuid.uuid4())
        bad = Contact.objects.bulk_create([Contact(email=f'bad-{n}@example.test', source='import',
            attributes={'source_import': import_id}) for n in range(100)])
        good = Contact.objects.bulk_create([Contact(email=f'good-{n}@example.test', source='manual') for n in range(400)])
        BlastDelivery.objects.bulk_create([BlastDelivery(blast=self.blast, contact=person, channel='email',
            address=person.email, status='bounced' if index < 10 else 'delivered',
            bounce_kind='hard' if index < 10 else '', delivered_at=None if index < 10 else self.now)
            for index, person in enumerate([*bad, *good])])
        untouched = self.contact(source='import', attributes={'source_import': import_id, 'retained': 'value'}, marketing_consent='granted')
        existing = self.contact(source='import', attributes={'source_import': import_id, 'marketing_review_hold': {'reason': 'existing'}})
        withdrawn = self.contact(source='import', attributes={'source_import': import_id}, marketing_consent='withdrawn')
        incidents = lifecycle_guard.quarantine(self.blast, now=self.now)
        untouched.refresh_from_db(); existing.refresh_from_db(); withdrawn.refresh_from_db()
        self.assertTrue(incidents)
        self.assertEqual(untouched.attributes['marketing_review_hold']['reason'], 'cohort_deliverability')
        self.assertEqual(untouched.marketing_consent, 'granted')
        self.assertEqual(existing.attributes['marketing_review_hold'], {'reason': 'existing'})
        self.assertNotIn('marketing_review_hold', withdrawn.attributes)
        bad[0].refresh_from_db(); self.assertNotIn('marketing_review_hold', bad[0].attributes)
        self.news.refresh_from_db(); self.assertEqual(self.news.state, 'warming')

    def test_click_comparison_throttles_only_a_mature_provider_with_enough_history(self):
        wave = BlastWave.objects.create(blast=self.blast, index=0, size=1000, status='finished',
            started_at=self.now - timedelta(hours=36), finished_at=self.now - timedelta(hours=35))
        people = Contact.objects.bulk_create([Contact(email=f'click-{n}@example.test', source='manual') for n in range(1000)])
        BlastDelivery.objects.bulk_create([BlastDelivery(blast=self.blast, wave=wave, contact=person, channel='email',
            address=person.email, status='delivered', delivered_at=self.now - timedelta(hours=35),
            provider='microsoft' if n < 500 else 'google',
            first_clicked_at=self.now - timedelta(hours=30) if n < 10 or 500 <= n < 550 else None)
            for n, person in enumerate(people)])
        for n in range(3, 6):
            StreamDay.objects.create(stream=self.news, day=(self.now - timedelta(days=n)).date(), budget=1000, ramp_stage=3,
                by_provider={'microsoft': {'sent': 1000, 'clicked': 100}, 'google': {'sent': 1000, 'clicked': 100}})
        incidents = lifecycle_guard.click_anomalies(self.news, now=self.now)
        self.assertEqual([incident.recipient_provider for incident in incidents], ['microsoft'])
        self.assertEqual(waves.provider_states(self.blast), {'microsoft': 'throttled'})
        self.news.refresh_from_db(); self.assertEqual(self.news.state, 'warming')

    def test_missing_click_baseline_and_small_cohorts_never_invent_an_anomaly(self):
        self.assertEqual(lifecycle_guard.click_anomalies(self.news, now=self.now), [])
