"""Explicitly approved 0003 reversal/reapplication, disposable synthetic DB only."""

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from .test_campaign_waves_database import WAVE_SETTINGS


class LegacyWaveAdoptionTests(TransactionTestCase):
    def test_provider_ownership_and_feedback_survive_reversal_and_readoption(self):
        before = [('crm', '0002_deliverability_foundations')]
        after = [('crm', '0003_campaign_waves_and_planner')]
        executor = MigrationExecutor(connection)
        leaves = executor.loader.graph.leaf_nodes()
        try:
            executor.migrate(before)
            apps = executor.loader.project_state(before).apps
            Contact = apps.get_model('crm', 'Contact')
            Blast = apps.get_model('crm', 'Blast')
            Delivery = apps.get_model('crm', 'BlastDelivery')
            Event = apps.get_model('crm', 'EmailEvent')
            contact = Contact.objects.create(email='legacy@example.net', source='manual', marketing_consent='withdrawn', email_status='bounced')
            blast = Blast.objects.create(channels=['email'], purpose='marketing', status='sending',
                queued_at=timezone.now(), listmonk_list_id=123, listmonk_campaign_id=456,
                stats={'email': {'started_at': '2026-11-02T22:00:00Z', 'listmonk_status': 'running'}})
            delivery = Delivery.objects.create(blast=blast, contact=contact, channel='email', address=contact.email,
                status='bounced', consent_basis='explicit', provider_id='preserved-id')
            event = Event.objects.create(source='resend', event_type='email.bounced', receipt='a' * 64,
                recipient=contact.email, sender='hello@news.mlai.au', delivery=delivery, blast=blast, occurred_at=timezone.now())
            draft = Blast.objects.create(channels=['email'], purpose='marketing', status='draft')
            for run in range(2):
                executor = MigrationExecutor(connection)
                executor.migrate(after)
                apps = executor.loader.project_state(after).apps
                wave = apps.get_model('crm', 'BlastWave').objects.get(blast_id=blast.pk)
                self.assertEqual((wave.index, wave.listmonk_list_id, wave.listmonk_campaign_id, wave.status), (0, 123, 456, 'running'))
                self.assertEqual(apps.get_model('crm', 'BlastDelivery').objects.get(pk=delivery.pk).wave_id, wave.pk)
                self.assertEqual(apps.get_model('crm', 'EmailEvent').objects.get(pk=event.pk).wave_id, wave.pk)
                self.assertFalse(apps.get_model('crm', 'BlastWave').objects.filter(blast_id=draft.pk).exists())
                preserved = apps.get_model('crm', 'Contact').objects.get(pk=contact.pk)
                self.assertEqual((preserved.email_domain, preserved.email_status, preserved.marketing_consent), ('example.net', 'bounced', 'withdrawn'))
                if run == 0:
                    executor = MigrationExecutor(connection)
                    executor.migrate(before)
                    apps = executor.loader.project_state(before).apps
                    preserved_blast = apps.get_model('crm', 'Blast').objects.get(pk=blast.pk)
                    preserved_delivery = apps.get_model('crm', 'BlastDelivery').objects.get(pk=delivery.pk)
                    self.assertEqual((preserved_blast.listmonk_list_id, preserved_blast.listmonk_campaign_id, preserved_delivery.provider_id,
                        preserved_delivery.status), (123, 456, 'preserved-id', 'bounced'))
                    self.assertNotIn('wave_legacy_adopted', preserved_blast.stats['email'])
        finally:
            MigrationExecutor(connection).migrate(leaves)


@override_settings(**WAVE_SETTINGS)
class WaveReservationConcurrencyTests(TransactionTestCase):
    def test_two_dispatchers_cannot_double_book_a_stream_slot(self):
        if connection.vendor != 'postgresql':
            self.skipTest('Row lock behavior requires PostgreSQL')
        from concurrent.futures import ThreadPoolExecutor
        from datetime import datetime, timedelta
        from threading import Barrier
        from unittest.mock import patch
        from django.core.cache import cache
        from django.db import close_old_connections
        from crm.deliverability import volume, waves
        from crm.deliverability.streams import sync_streams
        from crm.models import Blast, BlastDelivery, BlastWave, Contact, SendingStream
        now = datetime(2026, 11, 9, 9, tzinfo=volume.MELBOURNE)
        sync_streams()
        stream = SendingStream.objects.get(key='news')
        stream.mailbox.enabled = True; stream.mailbox.save()
        cache.clear(); cache.set('crm:deliverability:healthy', True, 3600)
        contacts = Contact.objects.bulk_create([Contact(email=f'concurrency-{n}@example.test', source='manual') for n in range(100)])
        blasts = [Blast.objects.create(purpose='marketing', channels=['email'], stream=stream, mailbox=stream.mailbox,
            status='queued', queued_at=now + timedelta(seconds=n)) for n in range(2)]
        BlastDelivery.objects.bulk_create([BlastDelivery(blast=blast, contact=contact, channel='email',
            address=contact.email, consent_basis='explicit', send_rank=n, provider='google')
            for blast in blasts for n, contact in enumerate(contacts)])
        barrier = Barrier(2)
        def claim(blast_id):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                wave, _ = waves.reserve(blast_id, now=now)
                return wave.pk if wave else None
            finally:
                close_old_connections()
        with patch('crm.sending_domains.is_verified', return_value=True), ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(claim, [blast.pk for blast in blasts]))
        self.assertEqual(sum(result is not None for result in results), 1)
        self.assertEqual(BlastWave.objects.get().size, 50)
        self.assertEqual(volume.usage(stream, now), (50, 50))
