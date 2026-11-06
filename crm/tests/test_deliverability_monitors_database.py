"""Synthetic recovery, attribution, monitor and owner API acceptance fixtures."""

import hashlib
import uuid
from datetime import timedelta
from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone

from crm.deliverability import guardian, guard_scopes, observations, scopes, waves
from crm.deliverability.monitors import dns, postmaster, resend
from crm.deliverability.streams import sync_streams
from crm.models import (Blast, BlastDelivery, BlastWave, Contact, DeliverabilityIncident, EmailEvent,
    ReputationCheck, SendingStream)

from .api_fixtures import PREFIX, CrmApiMixin
from .test_blasts_database import SENDING


@override_settings(**{**SENDING, 'CRM_GUARDIAN_ENABLED': True, 'CRM_MONITORS_ENABLED': True,
    'CRM_WAVES_ENABLED': True, 'CRM_PREFLIGHT_ENABLED': False, 'CRM_SENDING_DOMAINS': ['news.mlai.au:resend'],
    'CRM_DELIVERABILITY_OWNERS': ('admin@example.test',), 'CRM_DELIVERABILITY_SLACK_CHANNEL': ''})
class MonitorDatabaseTests(CrmApiMixin, TestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.now = timezone.now()
        self.enterContext(patch('integrations.http_client.request', side_effect=AssertionError('Network forbidden')))
        self.enterContext(patch('crm.sending_domains.is_verified', return_value=True))
        sync_streams()
        self.news = SendingStream.objects.get(key='news')
        self.news.mailbox.enabled = True; self.news.mailbox.save()
        self.blast = Blast.objects.create(stream=self.news, mailbox=self.news.mailbox, created_by=self.admin,
            purpose='marketing', channels=['email'], status='sending', subject='Community news',
            body_markdown='Community news', queued_at=self.now - timedelta(hours=1))
        self.wave = BlastWave.objects.create(blast=self.blast, index=0, kind='canary', status='finished', size=100,
            started_at=self.now - timedelta(hours=1), finished_at=self.now - timedelta(minutes=50))

    def rows(self, count, *, provider='microsoft'):
        contacts = Contact.objects.bulk_create([Contact(email=f'{uuid.uuid4()}@example.test', source='manual',
            marketing_consent='granted', marketing_consent_at=self.now) for _ in range(count)])
        BlastDelivery.objects.bulk_create([BlastDelivery(blast=self.blast, wave=self.wave, contact=contact,
            address=contact.email, channel='email', consent_basis='explicit', status='sent', provider=provider)
            for contact in contacts])
        return list(self.blast.deliveries.filter(contact__in=contacts))

    def event(self, row, kind, **fields):
        message = row.provider_id or str(row.pk)
        with patch('django.utils.timezone.now', return_value=self.now):
            return EmailEvent.objects.create(source='resend', event_type=kind, receipt=hashlib.sha256(str(uuid.uuid4()).encode()).hexdigest(),
                provider_message_id=message, recipient=row.address, sender=self.news.mailbox.email, stream=self.news,
                blast=self.blast, delivery=row, wave=self.wave, recipient_provider=row.provider, occurred_at=self.now, **fields)

    def test_rows_and_repeated_callbacks_are_one_negative_message(self):
        row = self.rows(1)[0]
        row.status, row.bounce_kind, row.provider_id = 'bounced', 'hard', str(uuid.uuid4()); row.save()
        self.event(row, 'email.bounced', bounce_type='Permanent')
        self.event(row, 'email.bounced', bounce_type='Permanent')
        metrics = scopes.blast_metrics(self.blast, now=self.now, wave=self.wave)
        self.assertEqual((metrics.attempted, metrics.hard_bounces), (1, 1))

    def test_provider_throttle_keeps_the_other_providers_and_stream_running(self):
        rows = self.rows(50)
        self.rows(50, provider='google')
        for row in rows[:5]:
            self.event(row, 'email.delivery_delayed')
        metrics = scopes.stream_metrics(self.news, now=self.now, hours=1, provider='microsoft')
        signal, = scopes.scoped(scopes.extra_signals(metrics, provider=True), scope='1h', provider='microsoft', metrics=metrics)
        incident = guardian.detect(None, signal, stream=self.news, now=self.now)
        guardian.enforce(incident, now=self.now)
        self.news.refresh_from_db(); self.blast.refresh_from_db()
        self.assertEqual((self.news.state, self.blast.pause_state), ('warming', ''))
        self.blast.stream = self.news
        self.assertEqual(waves.provider_states(self.blast), {'microsoft': 'throttled'})

    def test_owner_can_resume_acknowledged_canary_but_new_negative_evidence_reopens_it(self):
        rows = self.rows(50)
        BlastDelivery.objects.filter(pk__in=[row.pk for row in rows[:3]]).update(status='bounced', bounce_kind='hard')
        metrics = scopes.blast_metrics(self.blast, now=self.now, wave=self.wave)
        signal, = scopes.scoped(guardian.evaluate(metrics, canary=True), scope='', metrics=metrics)
        incident = guardian.detect(self.blast, signal, wave=self.wave, now=self.now)
        self.assertEqual(waves.canary_block(self.blast, self.now), 'first_batch_bounces')
        guardian.acknowledge(incident.pk, actor=self.admin, resolution='resume_slow', now=self.now)
        self.blast.refresh_from_db()
        self.assertIsNone(waves.canary_block(self.blast, self.now))
        BlastDelivery.objects.filter(pk=rows[3].pk).update(status='bounced', bounce_kind='hard')
        self.assertEqual(waves.canary_block(self.blast, self.now), 'first_batch_bounces')

    def test_same_count_with_new_window_evidence_is_not_silently_acknowledged(self):
        first = guardian.Signal('policy_block', 'red', 3, 3, 3, 'stop_stream', evidence_key='old', scope='1h', provider='google')
        incident = guardian.detect(None, first, stream=self.news, now=self.now)
        guardian.acknowledge(incident.pk, actor=self.admin, resolution='resume', now=self.now)
        self.assertIsNone(guardian.detect(None, first, stream=self.news, now=self.now))
        changed = guardian.Signal('policy_block', 'red', 3, 3, 3, 'stop_stream', evidence_key='new', scope='1h', provider='google')
        self.assertIsNotNone(guardian.detect(None, changed, stream=self.news, now=self.now))

    def test_completed_canary_amber_cools_but_new_evidence_and_red_still_block(self):
        rows = self.rows(50)
        self.blast.deliveries.update(status='delivered', delivered_at=self.now)
        BlastDelivery.objects.filter(pk=rows[0].pk).update(status='bounced', bounce_kind='hard', delivered_at=None)
        metrics = scopes.blast_metrics(self.blast, now=self.now, wave=self.wave)
        signal, = scopes.scoped(guardian.evaluate(metrics, canary=True), scope='', metrics=metrics)
        incident = guardian.detect(self.blast, signal, wave=self.wave, now=self.now)
        later = self.now + timedelta(hours=24)
        self.assertEqual(waves.canary_block(self.blast, later - timedelta(seconds=1)), 'first_batch_bounces')
        self.assertIsNone(waves.canary_block(self.blast, later))
        self.assertIsNone(guardian.detect(self.blast, signal, wave=self.wave, now=later))
        guard_scopes.recover(set(), now=later)
        incident.refresh_from_db(); self.assertEqual(incident.state, 'resolved')
        BlastDelivery.objects.filter(pk=rows[1].pk).update(status='bounced', bounce_kind='hard', delivered_at=None)
        self.assertEqual(waves.canary_block(self.blast, later), 'first_batch_bounces')
        BlastDelivery.objects.filter(pk=rows[2].pk).update(status='bounced', bounce_kind='hard', delivered_at=None)
        metrics = scopes.blast_metrics(self.blast, now=later, wave=self.wave)
        signal, = scopes.scoped(guardian.evaluate(metrics, canary=True), scope='', metrics=metrics)
        red = guardian.detect(self.blast, signal, wave=self.wave, now=later)
        self.assertEqual(red.severity, 'red')
        self.assertEqual(waves.canary_block(self.blast, later + timedelta(days=2)), 'first_batch_bounces')

    def test_completed_deferral_clears_and_invalid_policy_root_is_refused(self):
        rows = self.rows(50)
        self.event(rows[0], 'email.delivery_delayed')
        self.assertEqual(scopes.blast_metrics(self.blast, now=self.now).deferred, 1)
        BlastDelivery.objects.filter(pk=rows[0].pk).update(status='delivered', delivered_at=self.now)
        self.assertEqual(scopes.blast_metrics(self.blast, now=self.now).deferred, 0)
        response = self.client.patch(PREFIX + 'deliverability/streams/news/', 42, format='json')
        self.assertEqual(response.status_code, 400)

    def test_dns_requires_two_spaced_failures_and_two_spaced_recovery_passes(self):
        with patch.object(dns, 'advisories', return_value={}), patch.object(dns.sending_domains, 'domain_health',
                side_effect=[{'status': 'failing'}, {'status': 'failing'}, {'status': 'verified'}, {'status': 'verified'}, {'status': 'verified'}]):
            dns.check(self.news, now=self.now)
            self.news.refresh_from_db(); self.assertEqual(self.news.state, 'warming')
            dns.check(self.news, now=self.now + timedelta(minutes=10))
            self.news.refresh_from_db(); self.assertEqual(self.news.state, 'held')
            first = dns.check(self.news, now=self.now + timedelta(minutes=20))
            repeated = dns.check(self.news, now=self.now + timedelta(minutes=20, seconds=1))
            self.assertEqual((first['passes'], repeated['passes']), (1, 1))
            dns.check(self.news, now=self.now + timedelta(minutes=30))
        _, firing = guard_scopes.run_scopes(now=self.now + timedelta(minutes=30))
        guard_scopes.recover(firing, now=self.now + timedelta(minutes=30))
        self.news.refresh_from_db(); self.assertEqual(self.news.state, 'warming')

    def test_dependency_recovers_on_positive_evidence_but_never_clears_a_red_stop(self):
        incident = guardian.detect(None, guardian.Signal('listmonk_down', 'amber', 1, 1, 1, 'hold_stream'), stream=self.news, now=self.now)
        observations.record(self.news, 'config', {'status': 'pass', 'critical': []}, now=self.now)
        _, firing = guard_scopes.run_scopes(now=self.now)
        guard_scopes.recover(firing, now=self.now)
        incident.refresh_from_db(); self.news.refresh_from_db()
        self.assertEqual((incident.state, self.news.state), ('resolved', 'warming'))
        guardian.detect(None, guardian.Signal('blocklist', 'red', 1, 1, 1, 'stop_stream'), stream=self.news, now=self.now)
        guard_scopes.recover(set(), now=self.now + timedelta(days=2))
        self.news.refresh_from_db(); self.assertEqual(self.news.state, 'stopped')

    def test_postmaster_red_demotes_once_and_owner_recovery_does_not_demote_twice(self):
        self.news.ramp_stage = 5; self.news.save()
        day = (self.now - timedelta(days=1)).date().isoformat()
        with patch.object(postmaster, 'token', return_value='synthetic'), patch.object(postmaster, 'query',
                return_value={day: {'spam_rate': 0.0018}}), patch.object(postmaster, 'json_request', return_value={}):
            postmaster.check(self.news, now=self.now)
            postmaster.check(self.news, now=self.now)
        self.news.refresh_from_db()
        self.assertEqual((self.news.state, self.news.ramp_stage), ('stopped', 3))
        incident = DeliverabilityIncident.objects.get(signal='postmaster_spam_rate')
        guardian.acknowledge(incident.pk, actor=self.admin, resolution='resume_slow', now=self.now)
        self.news.refresh_from_db(); self.assertEqual(self.news.ramp_stage, 3)

    def test_notify_incidents_never_stop_transactional_delivery_or_marketing(self):
        notify = SendingStream.objects.get(key='notify')
        incident = guardian.detect(None, guardian.Signal('postmaster_spam_rate', 'red', 0.02, 0.0015, 1, 'stop_stream'), stream=notify, now=self.now)
        notify.refresh_from_db(); self.news.refresh_from_db()
        self.assertEqual((incident.action, notify.state, self.news.state), ('alert_only', 'warming', 'warming'))

    def test_owner_health_hides_recipients_and_staff_cannot_read_or_change_policy(self):
        self.rows(1)
        response = self.client.get(PREFIX + 'deliverability/')
        self.assertEqual(response.status_code, 200)
        news = next(row for row in response.json()['streams'] if row['key'] == 'news')
        self.assertEqual(len(news['capacity_forecast']), 7)
        self.assertIn('checks', news)
        self.assertNotIn('@example.test', str(response.json()))
        with override_settings(CRM_DELIVERABILITY_OWNERS=()):
            for method, route, data in (('get', 'deliverability/', None), ('patch', 'deliverability/streams/news/', {'daily_budget_override': 200}),
                    ('post', 'deliverability/self-check/', {'stream': 'news'}), ('post', 'deliverability/seed-test/', {'stream': 'news'})):
                response = getattr(self.client, method)(PREFIX + route, data, format='json') if data else self.client.get(PREFIX + route)
                self.assertEqual(response.status_code, 403)

    def test_policy_changes_are_audited_and_cannot_reconfigure_protected_identity(self):
        response = self.client.patch(PREFIX + 'deliverability/streams/news/', {'daily_budget_override': 200,
            'policy_overrides': {'CRM_GUARD_HARD_BOUNCE_RED': 0.07}}, format='json')
        self.assertEqual(response.status_code, 200)
        for values in ({'domain': 'mlai.au'}, {'policy_overrides': {'CRM_GUARDIAN_ENABLED': False}},
                {'daily_budget_override': 999999}, {'policy_overrides': {'CRM_GUARD_HARD_BOUNCE_RED': 0.001}}):
            self.assertEqual(self.client.patch(PREFIX + 'deliverability/streams/news/', values, format='json').status_code, 400)

    def test_a_fully_accounted_quiet_batch_is_not_a_feedback_outage(self):
        self.rows(50)
        self.blast.deliveries.update(status='delivered', delivered_at=self.now)
        _, firing = guard_scopes.run_scopes(now=self.now + timedelta(minutes=40))
        self.assertFalse(DeliverabilityIncident.objects.filter(signal='feedback_silence').exists())

    def test_suppression_reconciliation_does_not_invent_a_withdrawal_on_a_bounce(self):
        row = self.rows(1)[0]
        with override_settings(RESEND_READ_API_KEY='synthetic'), patch.object(resend, 'listing', return_value=[
                {'email': row.address, 'origin': 'bounce'}]):
            resend.reconcile_suppressions(now=self.now)
        row.contact.refresh_from_db()
        self.assertEqual((row.contact.email_status, row.contact.marketing_consent), ('bounced', 'granted'))

    def test_owner_email_retries_keep_the_frozen_payload_and_never_use_a_marketing_key(self):
        from crm.deliverability import owner_mail
        from crm.models import CrmAuditEvent
        notify = SendingStream.objects.get(key='notify')
        notify.mailbox.enabled = True; notify.mailbox.save()
        with override_settings(CRM_DELIVERABILITY_EMAIL_ALERTS_ENABLED=True, CRM_NOTIFY_API_KEY='synthetic-notify'), patch(
                'crm.sending_domains.domain_health', return_value={'status': 'verified'}), patch.object(owner_mail, 'json_request',
                side_effect=[owner_mail.Unavailable('uncertain'), {'id': str(uuid.uuid4())}]) as request:
            self.assertFalse(owner_mail.send('First subject', 'Initial aggregate evidence', key='test-alert', now=self.now))
            self.assertTrue(owner_mail.send('New subject', 'Changed evidence', key='test-alert', now=self.now + timedelta(minutes=3)))
            self.assertTrue(owner_mail.send('New subject', 'Changed evidence', key='test-alert', now=self.now + timedelta(minutes=4)))
        self.assertEqual(request.call_count, 2)
        self.assertEqual(request.call_args_list[0].kwargs['json'], request.call_args_list[1].kwargs['json'])
        self.assertEqual(request.call_args_list[0].kwargs['headers']['Idempotency-Key'], request.call_args_list[1].kwargs['headers']['Idempotency-Key'])
        audit = CrmAuditEvent.objects.filter(target_type='deliverability_alert_email').order_by('-pk').first()
        self.assertNotIn('synthetic-notify', str(audit.detail))
        self.assertNotIn('admin@example.test', str(audit.detail))

    def test_an_uncertain_owner_alert_is_never_retried_after_the_provider_receipt_expires(self):
        from crm.deliverability import owner_mail
        notify = SendingStream.objects.get(key='notify')
        notify.mailbox.enabled = True; notify.mailbox.save()
        with override_settings(CRM_DELIVERABILITY_EMAIL_ALERTS_ENABLED=True, CRM_NOTIFY_API_KEY='synthetic-notify'), patch(
                'crm.sending_domains.domain_health', return_value={'status': 'verified'}), patch.object(owner_mail, 'json_request',
                side_effect=owner_mail.Unavailable('uncertain')) as request:
            self.assertFalse(owner_mail.send('Subject', 'Evidence', key='expired-alert', now=self.now))
            self.assertFalse(owner_mail.send('Subject', 'Evidence', key='expired-alert', now=self.now + timedelta(hours=24)))
        self.assertEqual(request.call_count, 1)
