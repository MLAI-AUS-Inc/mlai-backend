"""Synthetic system-mail failures remain alert-only while marketing is stopped."""

from datetime import timedelta
from unittest.mock import patch
from uuid import uuid4

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone

from crm.deliverability import observations, transactional
from crm.deliverability.streams import sync_streams
from crm.models import CrmAuditEvent, DeliverabilityIncident, EmailEvent, SendingStream
from integrations import system_mail


@override_settings(CRM_TRANSACTIONAL_MONITOR_ENABLED=True, CRM_GUARDIAN_ENABLED=False,
    CRM_MONITORS_ENABLED=False, CRM_SENDING_ENABLED=False, CRM_GUARDIAN_DRY_RUN=False)
class TransactionalDatabaseTests(TestCase):
    def setUp(self):
        cache.clear(); self.now = timezone.now(); sync_streams()
        self.notify = SendingStream.objects.get(key='notify')
        self.news = SendingStream.objects.get(key='news')
        self.news.state = 'stopped'; self.news.save()
        self.alert = self.enterContext(patch('crm.deliverability.alerts.alert_open_incidents'))
        self.enterContext(patch('integrations.http_client.request', side_effect=AssertionError('Network forbidden')))

    def attempts(self, count, failures, *, kind='signin', provider='customerio'):
        for index in range(count):
            with patch('integrations.system_mail.timezone.now', return_value=self.now - timedelta(seconds=10)):
                with system_mail.observe(provider, kind) as attempt:
                    if index >= failures: attempt.accepted({'id': str(uuid4())})

    def run_monitor(self):
        cache.delete('crm:transactional:monitor')
        return transactional.run_transactional_monitor(now=self.now)

    def test_more_than_five_percent_signin_failures_are_red_without_marketing_flags(self):
        self.attempts(20, 2)
        before = (self.notify.state, self.notify.ramp_stage)
        self.assertEqual(self.run_monitor(), {'status': 'completed', 'signals': 1})
        incident = DeliverabilityIncident.objects.get()
        self.assertEqual((incident.signal, incident.severity, incident.action), ('transactional_failure_rate', 'red', 'alert_only'))
        self.notify.refresh_from_db(); self.news.refresh_from_db()
        self.assertEqual((self.notify.state, self.notify.ramp_stage), before)
        self.assertEqual(self.news.state, 'stopped')
        self.assertEqual(self.run_monitor()['signals'], 1)
        self.assertEqual(DeliverabilityIncident.objects.count(), 1)

    def test_exactly_five_percent_is_not_red_and_provider_groups_do_not_dilute_failures(self):
        self.attempts(20, 1)
        self.assertEqual(self.run_monitor()['signals'], 0)
        self.attempts(2, 1, kind='notification', provider='resend')
        self.assertEqual(self.run_monitor()['signals'], 1)
        self.assertEqual(DeliverabilityIncident.objects.get().sample, 2)

    def test_old_or_unknown_observations_never_claim_success(self):
        result = self.run_monitor()
        self.assertEqual(result['signals'], 0)
        observation = observations.latest(self.notify, 'transactional')
        self.assertEqual(observation['status'], 'no_data')
        self.assertIsNone(observation['resend_delivery_p95_seconds'])
        self.assertEqual(observation['customerio_delivery_status'], 'unknown')
        self.assertEqual(observation['customerio_dkim'], 'unknown')

    def test_system_transport_receipts_do_not_replace_staff_contact_activity(self):
        from crm.queries import recent_activity
        self.attempts(20, 2)
        visible = CrmAuditEvent.objects.create(action='edit', target_type='contact', target_id='synthetic')
        rows, _ = recent_activity()
        self.assertIn(visible.pk, [row.pk for row in rows])
        self.assertFalse(any(row.target_type == 'transactional_mail' for row in rows))

    def event(self, identifier, kind, at, **values):
        return EmailEvent.objects.create(source='resend', event_type=kind, provider_message_id=identifier,
            receipt=uuid4().hex, sender='notifications@notify.mlai.au', recipient='synthetic@example.test',
            stream=self.notify, occurred_at=at, **values)

    def test_delivery_latency_requires_sustained_provider_feedback_and_never_uses_acceptance(self):
        self.attempts(1, 0, provider='resend', kind='notification')
        identifier = str(uuid4())
        self.event(identifier, 'email.sent', self.now - timedelta(minutes=10))
        self.event(identifier, 'email.delivered', self.now - timedelta(minutes=7))
        self.assertEqual(self.run_monitor()['signals'], 0)
        previous = observations.latest(self.notify, 'transactional')
        observations.record(self.notify, 'transactional', {**previous,
            'latency_since': (self.now - timedelta(minutes=16)).isoformat()}, now=self.now - timedelta(seconds=55))
        self.assertEqual(self.run_monitor()['signals'], 1)
        incident = DeliverabilityIncident.objects.get()
        self.assertEqual((incident.signal, incident.value, incident.action), ('transactional_delivery_latency', 180, 'alert_only'))

    @override_settings(CRM_GUARD_HARD_BOUNCE_MIN_SAMPLE=1)
    def test_notify_hard_bounce_does_not_change_a_stream_or_campaign(self):
        identifier = str(uuid4())
        self.event(identifier, 'email.sent', self.now - timedelta(minutes=5))
        self.event(identifier, 'email.bounced', self.now - timedelta(minutes=4), bounce_type='Permanent')
        before = self.notify.state
        self.assertEqual(self.run_monitor()['signals'], 1)
        self.notify.refresh_from_db(); self.assertEqual(self.notify.state, before)
        self.assertEqual(DeliverabilityIncident.objects.get().action, 'alert_only')

    @override_settings(CRM_TRANSACTIONAL_MAX_ROWS=1)
    def test_incomplete_observation_page_is_unknown(self):
        self.attempts(2, 2)
        self.assertEqual(self.run_monitor()['signals'], 0)
        self.assertEqual(observations.latest(self.notify, 'transactional')['status'], 'unknown')

    def test_telemetry_database_failure_does_not_poison_the_caller_transaction(self):
        from django.db import connection
        def fail_write(**values):
            with connection.cursor() as cursor:
                cursor.execute('SELECT 1 / 0')
        with patch.object(CrmAuditEvent.objects, 'create', side_effect=fail_write):
            with system_mail.observe('customerio', 'signin') as attempt:
                attempt.accepted({'id': 'accepted'})
        self.assertTrue(attempt.ok)
        self.assertEqual(SendingStream.objects.count(), 2)
