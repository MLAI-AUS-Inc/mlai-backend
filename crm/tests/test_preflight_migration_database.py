"""Approved 0004 reversal/reapplication on the disposable synthetic database."""

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.utils import timezone


class PreflightMigrationTests(TransactionTestCase):
    def test_new_metadata_leaves_provider_ownership_and_consent_intact(self):
        before = [('crm', '0003_campaign_waves_and_planner')]
        after = [('crm', '0004_preflight_and_lifecycle')]
        executor = MigrationExecutor(connection)
        leaves = executor.loader.graph.leaf_nodes()
        try:
            executor.migrate(before)
            old = executor.loader.project_state(before).apps
            contact = old.get_model('crm', 'Contact').objects.create(email='withdrawn@example.test', source='manual',
                marketing_consent='withdrawn', email_status='bounced', soft_bounce_streak=3,
                attributes={'dormant_suppressed': True})
            mailbox = old.get_model('crm', 'SenderMailbox').objects.create(email='hello@news.mlai.au',
                display_name='MLAI', domain='news.mlai.au', route='resend')
            stream = old.get_model('crm', 'SendingStream').objects.create(key='news', label='Community news',
                domain=mailbox.domain, mailbox=mailbox)
            blast = old.get_model('crm', 'Blast').objects.create(channels=['email'], status='sending',
                stream=stream, mailbox=mailbox, listmonk_list_id=123, listmonk_campaign_id=456,
                pause_state='system', pause_reason='hard_bounce_rate')
            wave = old.get_model('crm', 'BlastWave').objects.create(blast=blast, index=0, kind='canary', status='paused',
                size=1, listmonk_list_id=123, listmonk_campaign_id=456)
            delivery = old.get_model('crm', 'BlastDelivery').objects.create(blast=blast, wave=wave, contact=contact,
                channel='email', address=contact.email, status='bounced', bounce_kind='hard', provider_id='kept-id')
            for run in range(2):
                executor = MigrationExecutor(connection)
                executor.migrate(after)
                new = executor.loader.project_state(after).apps
                current = new.get_model('crm', 'Blast').objects.get(pk=blast.pk)
                self.assertEqual((current.preview_text, current.sign_off_name, current.preflight), ('', '', {}))
                person = new.get_model('crm', 'Contact').objects.get(pk=contact.pk)
                self.assertEqual((person.marketing_consent, person.email_status, person.soft_bounce_streak,
                    person.attributes, person.consent_reconfirmed_at), ('withdrawn', 'bounced', 3, {'dormant_suppressed': True}, None))
                self.assertEqual((current.listmonk_list_id, current.listmonk_campaign_id, current.pause_state), (123, 456, 'system'))
                kept = new.get_model('crm', 'BlastDelivery').objects.get(pk=delivery.pk)
                self.assertEqual((kept.wave_id, kept.provider_id, kept.status, kept.bounce_kind), (wave.pk, 'kept-id', 'bounced', 'hard'))
                self.assertFalse(new.get_model('crm', 'SeedMailbox').objects.exists())
                self.assertFalse(new.get_model('crm', 'SeedTest').objects.exists())
                self.assertFalse(new.get_model('crm', 'ReputationCheck').objects.exists())
                if run == 0:
                    stream = new.get_model('crm', 'SendingStream').objects.get(pk=stream.pk)
                    mailbox = new.get_model('crm', 'SeedMailbox').objects.create(provider='gmail', address='seed@example.test',
                        imap_host='imap.example.test', auth='app_password', secret_env='CRM_SEED_SYNTHETIC_PASSWORD')
                    self.assertFalse(mailbox.enabled)
                    new.get_model('crm', 'SeedTest').objects.create(stream=stream, blast_id=blast.pk,
                        content_hash='a' * 64, results={'status': 'synthetic'})
                    new.get_model('crm', 'ReputationCheck').objects.create(subject='example.test', source='surbl',
                        listed=None, detail={'status': 'not_configured'}, checked_at=timezone.now())
                    MigrationExecutor(connection).migrate(before)
        finally:
            MigrationExecutor(connection).migrate(leaves)
