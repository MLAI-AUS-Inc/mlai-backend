"""Import observations and explicit address corrections preserve consent and identity."""

import io
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone

from core.models import AccountBan, User
from crm import contact_import, listmonk_sync, luma_sync
from crm.deliverability import import_hygiene, typos
from crm.email_aliases import canonical_emails
from crm.models import Contact, ContactConsentEvent, EventRegistration, MailDomainInfo
from crm.queries import banned

from .api_fixtures import PREFIX, CrmApiMixin
from .test_contact_store_database import FakeLuma, luma_event, luma_guest


@override_settings(CRM_LIFECYCLE_ENABLED=True, COMMUNITY_CHAT_CRM_ENABLED=True, CRM_LUMA_SYNC_ENABLED=False)
class ImportHygieneDatabaseTests(CrmApiMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.now = timezone.now()
        self.enterContext(patch('crm.deliverability.hygiene.lookup', side_effect=lambda domain, kind:
            None if domain == 'unknown.test' else [] if domain == 'nomx.test' else ['10 mx.google.com']))

    def test_report_distinguishes_no_mx_disposable_typo_and_unanswered(self):
        counts = import_hygiene.report(['a@nomx.test', 'b@unknown.test', 'c@mailinator.com', 'd@gmial.com'], now=self.now)
        self.assertEqual((counts['no_mx'], counts['domain_unverified'], counts['disposable'], counts['likely_typo']), (1, 1, 2, 1))
        self.assertTrue(MailDomainInfo.objects.filter(domain='nomx.test', has_mx=False).exists())
        self.assertFalse(MailDomainInfo.objects.filter(domain='unknown.test').exists())

    def test_import_stores_origin_and_hygiene_without_inventing_opt_in(self):
        _, parsed = contact_import.read_csv(io.StringIO('Email\na@nomx.test\nb@unknown.test\nc@gmial.com\n'))
        record = contact_import.apply_import(parsed, file_name='synthetic.csv', consent='none', now=self.now)
        self.assertEqual(record.counts['created'], 3)
        self.assertEqual((record.counts['no_mx'], record.counts['domain_unverified'], record.counts['likely_typo']), (1, 1, 1))
        self.assertEqual(Contact.objects.filter(marketing_consent='unknown').count(), 3)
        self.assertEqual(ContactConsentEvent.objects.count(), 0)
        self.assertTrue(all(person.attributes.get('source_import') == str(record.pk) for person in Contact.objects.all()))

    def test_manual_correction_preserves_all_holds_and_later_import_does_not_duplicate(self):
        person = Contact.objects.create(email='correction@gmial.com', source='manual', marketing_consent='withdrawn',
            email_status='unsubscribed', sms_status='opted_out', whatsapp_opt_in='withdrawn',
            attributes={'marketing_review_hold': {'reason': 'retained'}, 'retained': 'value'})
        self.assertEqual(canonical_emails([person.email]), {})
        with patch('crm.email_aliases.cache.delete', return_value=False):
            response = self.client.post(PREFIX + f'contacts/{person.pk}/correct-email/',
                {'expected': person.email, 'replacement': 'correction@gmail.com'}, format='json')
            self.assertEqual(response.status_code, 200, response.content)
            self.assertEqual(canonical_emails(['correction@gmial.com']), {'correction@gmial.com': 'correction@gmail.com'})
        person.refresh_from_db()
        self.assertEqual((person.email_status, person.marketing_consent, person.sms_status, person.whatsapp_opt_in),
            ('unsubscribed', 'withdrawn', 'opted_out', 'withdrawn'))
        self.assertEqual(person.attributes['marketing_review_hold'], {'reason': 'retained'})
        _, parsed = contact_import.read_csv(io.StringIO('Email\ncorrection@gmial.com\n'))
        contact_import.apply_import(parsed, file_name='repeat.csv', consent='granted', now=self.now)
        self.assertEqual(Contact.objects.count(), 1)
        person.refresh_from_db(); self.assertEqual(person.marketing_consent, 'withdrawn')
        self.assertEqual(ContactConsentEvent.objects.count(), 0)

    def test_luma_resync_uses_the_corrected_contact_and_keeps_registration_identity(self):
        person = Contact.objects.create(email='guest@gmial.com', source='luma')
        source = FakeLuma(events=[luma_event()])
        luma_sync.sync_event_catalog(source)
        event = luma_sync.CrmEvent.objects.get(luma_event_id='evt-1')
        source.guests = {'evt-1': [luma_guest('guest@gmial.com', 'synthetic-guest')]}
        luma_sync.sync_event_guests(source, event, now=self.now)
        registration = EventRegistration.objects.get(contact=person)
        typos.correct(person.pk, expected=person.email, replacement='guest@gmail.com', actor=self.admin, now=self.now)
        luma_sync.sync_event_guests(source, event, now=self.now)
        self.assertEqual(Contact.objects.count(), 1)
        self.assertTrue(EventRegistration.objects.filter(pk=registration.pk, contact=person, removed_at__isnull=True).exists())

    def test_late_opt_out_and_future_ban_at_original_address_still_protect_corrected_contact(self):
        person = Contact.objects.create(email='late@gmial.com', source='manual', marketing_consent='granted')
        typos.correct(person.pk, expected=person.email, replacement='late@gmail.com', actor=self.admin, now=self.now)
        self.assertTrue(listmonk_sync.record_email_opt_out('late@gmial.com', 'unsubscribed', source='synthetic', now=self.now))
        person.refresh_from_db(); self.assertEqual(person.marketing_consent, 'withdrawn')
        user = User.objects.create_user('late@gmial.com')
        AccountBan.objects.create(user=user, email=user.email)
        self.assertTrue(Contact.objects.filter(pk=person.pk).annotate(denied=banned()).get().denied)

    def test_conflict_stale_or_arbitrary_corrections_are_refused(self):
        person = Contact.objects.create(email='conflict@gmial.com', source='manual')
        Contact.objects.create(email='conflict@gmail.com', source='manual')
        for expected, replacement in ((person.email, 'conflict@gmail.com'), ('changed@gmial.com', 'conflict@gmail.com'),
                (person.email, 'different@gmail.com')):
            response = self.client.post(PREFIX + f'contacts/{person.pk}/correct-email/', {'expected': expected, 'replacement': replacement}, format='json')
            self.assertIn(response.status_code, (400, 409))
        person.refresh_from_db(); self.assertEqual(person.email, 'conflict@gmial.com')

    def test_background_domain_fill_advances_past_unknown_and_stays_bounded(self):
        Contact.objects.bulk_create([Contact(email=f'person@{domain}', email_domain=domain, source='manual')
            for domain in ('a.test', 'unknown.test', 'z.test')])
        with override_settings(CRM_IMPORT_DOMAIN_MAX_NEW=1):
            for _ in range(3):
                self.assertIn(import_hygiene.run_domain_hygiene(now=self.now)['status'], ('partial', 'complete'))
        self.assertTrue(MailDomainInfo.objects.filter(domain='z.test').exists())
        self.assertFalse(MailDomainInfo.objects.filter(domain='unknown.test').exists())
