"""Synthetic regression checks; run only with the exact test migration approval."""
from datetime import timedelta
from unittest.mock import patch
import uuid

from django.test import TestCase, override_settings
from django.utils import timezone

from community_chat.crm.services import save_blast_draft
from core.models import User
from crm import blasts, queries, sender_pool
from crm.models import Blast, BlastDelivery, Contact, CrmAuditEvent, SenderMailbox
from crm.resend_feedback import record_event

from .test_blasts_database import FakeProviders, SENDING
from .test_sender_pool_unit import SENDERS, WARMUP
from .test_resend_warmup_unit import SECRET


@override_settings(**SENDING, CRM_CAMPAIGN_SENDERS=SENDERS,
                   CRM_RESEND_MONITORED_SENDERS=WARMUP, CRM_SENDER_POOL_ENABLED=True,
                   CRM_RESEND_WEBHOOK_SECRET=SECRET)
class SenderPoolDatabaseTests(TestCase):
    def setUp(self):
        self.now = timezone.now()
        self.admin = User.objects.create_user('admin@example.test', is_active=True, is_superuser=True)
        self.fake = FakeProviders()
        self.enterContext(patch('integrations.http_client.request', new=self.fake))
        self.mailboxes = [SenderMailbox.objects.create(email=email, display_name='MLAI',
            domain='mlai.au', route='resend', reply_to=email, daily_limit=30, enabled=True)
            for email in SENDERS]
        self.people = [Contact.objects.create(email=f'person{i}@example.test', source='manual',
            marketing_consent='granted', marketing_consent_source='test', marketing_consent_at=self.now)
            for i in range(17)]

    def queue(self, *, key=None):
        parent = Blast.objects.create(purpose='marketing', channels=['email'],
            subject='MLAI news', body_markdown='Hello', mailbox=self.mailboxes[0],
            created_by=self.admin, stats={sender_pool.PARENT: {'version': 1}},
            segment_definition={'match': 'all', 'rules': [], 'exclude': []})
        count = blasts.blast_audience(parent, now=self.now)['channels']['email']['reachable']
        return blasts.queue_blast(parent.pk, confirm={'email': count},
            idempotency_key=key or str(parent.pk), actor=self.admin, now=self.now)

    def ready_child(self, parent):
        sender_pool.admit(self.now)
        return sender_pool.children(parent.pk).filter(status='queued').order_by('pk').first()

    def attempt(self, mailbox, *, age=timedelta(hours=2)):
        row = CrmAuditEvent.objects.create(action='send', target_type=sender_pool.ATTEMPT,
            target_id=str(mailbox.pk), detail={'kind': 'test'})
        CrmAuditEvent.objects.filter(pk=row.pk).update(created_at=self.now-age)
        return row

    def test_queue_snapshots_once_above_daily_capacity_and_freezes_senders(self):
        Contact.objects.bulk_create([Contact(email=f'more{i}@example.test', source='manual',
            marketing_consent='granted') for i in range(300)])
        parent = self.queue(key='once')
        original = list(sender_pool.children(parent.pk).values_list('pk', 'mailbox_id'))
        self.assertEqual(len(original), 317)
        self.assertFalse(parent.deliveries.exists())
        blasts.queue_blast(parent.pk, confirm={}, idempotency_key='once', actor=self.admin)
        self.assertCountEqual(list(sender_pool.children(parent.pk).values_list('pk', 'mailbox_id')), original)
        self.assertEqual(queries.blasts_queryset().count(), 1)
        self.assertEqual(queries.blast_stats([parent])[parent.pk]['channels']['email']['recipients'], 317)
        self.assertEqual(queries.blast_deliveries(parent).count(), 317)
        self.assertTrue(all(v == 0 for v in queries.mailbox_sent_last_24h([m.pk for m in self.mailboxes], self.now).values()))

    def test_unknown_or_withdrawn_consent_is_never_snapshotted(self):
        for consent in ('unknown', 'withdrawn'):
            Contact.objects.create(email=f'{consent}@example.test', source='manual',
                marketing_consent=consent, last_attended_at=self.now)
        parent = self.queue()
        self.assertEqual(sender_pool.children(parent.pk).count(), 17)

    def test_two_campaigns_share_one_outstanding_message_per_sender(self):
        one, two = self.queue(), self.queue()
        sender_pool.admit(self.now)
        sender_pool.admit(self.now)
        self.assertEqual(Blast.objects.filter(status='queued').exclude(stats__has_key=sender_pool.PARENT).count(), 8)
        self.assertEqual(sender_pool.children(two.pk).filter(status='queued').count(), 0)
        self.assertEqual(queries.mailbox_remaining_today(self.mailboxes[0], self.now), 30)

    def test_budget_is_shared_between_campaigns_and_includes_test_attempts(self):
        parent = self.queue()
        for mailbox in self.mailboxes:
            for _ in range(30): self.attempt(mailbox)
        sender_pool.admit(self.now)
        self.assertFalse(sender_pool.children(parent.pk).exclude(status='draft').exists())
        self.assertEqual(queries.mailbox_remaining_today(self.mailboxes[0], self.now), 0)
        with self.assertRaises(blasts.BlastError):
            sender_pool.reserve_test(self.mailboxes[0].pk, actor=self.admin, now=self.now)
        sender_pool.admit(self.now+timedelta(hours=25))
        self.assertEqual(sender_pool.children(parent.pk).filter(status='queued').count(), 8)

    def test_start_consumes_quota_and_sends_once_with_matching_reply_to(self):
        parent = self.queue(); child = self.ready_child(parent)
        blasts.dispatch_blast(child.pk, now=self.now)
        blasts.dispatch_blast(child.pk, now=self.now)
        child.refresh_from_db()
        self.assertEqual(CrmAuditEvent.objects.filter(target_type=sender_pool.ATTEMPT).count(), 1)
        self.assertEqual(self.fake.count('PUT', f'/api/campaigns/{child.listmonk_campaign_id}/status'), 1)
        campaign = self.fake.campaigns[child.listmonk_campaign_id]
        self.assertEqual(campaign['headers'], [{'Reply-To': child.mailbox.email}])
        self.assertIn(child.mailbox.email, campaign['from_email'])

    def test_withdrawal_after_queue_prevents_external_start(self):
        child = self.ready_child(self.queue())
        delivery = child.deliveries.get()
        Contact.objects.filter(pk=delivery.contact_id).update(marketing_consent='withdrawn')
        blasts.dispatch_blast(child.pk, now=self.now)
        self.assertFalse(CrmAuditEvent.objects.filter(target_type=sender_pool.ATTEMPT).exists())
        self.assertEqual(len(self.fake.campaigns), 0)

    def test_consent_becoming_unknown_after_queue_is_not_treated_as_inferred(self):
        child = self.ready_child(self.queue())
        Contact.objects.filter(pk=child.deliveries.get().contact_id).update(
            marketing_consent='unknown', last_attended_at=self.now)
        blasts.dispatch_blast(child.pk, now=self.now)
        self.assertEqual(len(self.fake.campaigns), 0)
        self.assertEqual(child.deliveries.get().error_code, 'consent_changed')

    def test_safety_pause_between_test_reservation_and_submission_prevents_send(self):
        draft = Blast.objects.create(purpose='marketing', channels=['email'],
            subject='Test', body_markdown='Hello', mailbox=self.mailboxes[0], created_by=self.admin)
        reserve = sender_pool.reserve_test
        def reserve_then_pause(*args, **kwargs):
            reserve(*args, **kwargs)
            sender_pool.pause('test', now=self.now)
        with patch('crm.sender_pool.reserve_test', side_effect=reserve_then_pause), \
                patch('crm.blast_email.send_test_email') as send:
            with self.assertRaises(blasts.BlastError):
                sender_pool.send_test(draft, actor=self.admin, now=self.now)
        send.assert_not_called()
        self.assertEqual(CrmAuditEvent.objects.filter(target_type=sender_pool.ATTEMPT).count(), 1)

    def test_uncertain_test_send_keeps_quota_and_commits_safety_pause(self):
        from crm.listmonk import ListmonkError
        draft = Blast.objects.create(purpose='marketing', channels=['email'],
            subject='Test', body_markdown='Hello', mailbox=self.mailboxes[0], created_by=self.admin)
        with patch('crm.blast_email.send_test_email', side_effect=ListmonkError('timeout')):
            with self.assertRaises(blasts.BlastError):
                sender_pool.send_test(draft, actor=self.admin, now=self.now)
        self.assertEqual(CrmAuditEvent.objects.filter(target_type=sender_pool.ATTEMPT).count(), 1)
        self.assertFalse(SenderMailbox.objects.filter(email__in=SENDERS, enabled=True).exists())

    def test_parent_cancellation_prevents_future_admissions(self):
        parent = self.queue(); self.ready_child(parent)
        blasts.cancel_blast(parent.pk, actor=self.admin, now=self.now)
        sender_pool.admit(self.now+timedelta(days=2))
        self.assertEqual(set(sender_pool.children(parent.pk).values_list('status', flat=True)), {'cancelled'})
        self.assertEqual(len(self.fake.campaigns), 0)

    def test_ambiguous_start_is_never_retried_and_pauses_only_pool(self):
        warm = SenderMailbox.objects.create(email=WARMUP[0], domain='mlai-aus.com',
            display_name='MLAI', route='resend', enabled=True, daily_limit=20)
        child = self.ready_child(self.queue())
        self.fake.fail_next('PUT', r'/api/campaigns/\d+/status', status=503)
        blasts.dispatch_blast(child.pk, now=self.now)
        child.refresh_from_db()
        self.assertEqual(child.deliveries.get().status, 'unknown')
        self.assertFalse(SenderMailbox.objects.filter(email__in=SENDERS, enabled=True).exists())
        warm.refresh_from_db(); self.assertTrue(warm.enabled)
        blasts.dispatch_blast(child.pk, now=self.now+timedelta(days=1))
        self.assertEqual(self.fake.count('PUT', f'/api/campaigns/{child.listmonk_campaign_id}/status'), 1)

    def test_complaint_deduplicates_and_suppresses_across_the_pool(self):
        child = self.ready_child(self.queue()); blasts.dispatch_blast(child.pk, now=self.now)
        delivery = child.deliveries.get()
        event = {'type': 'email.complained', 'sender': child.mailbox.email,
            'recipient': delivery.address, 'email_id': str(uuid.uuid4()), 'subject': child.subject, 'test': False}
        self.assertEqual(record_event(event, 'msg-test', now=self.now)['status'], 'recorded')
        self.assertEqual(record_event(event, 'msg-test', now=self.now)['status'], 'duplicate')
        delivery.contact.refresh_from_db()
        self.assertEqual(delivery.contact.email_status, 'complained')
        self.assertFalse(SenderMailbox.objects.filter(email__in=SENDERS, enabled=True).exists())
        self.assertEqual(CrmAuditEvent.objects.filter(target_type='resend_event').count(), 1)

    def test_recent_send_for_an_old_snapshot_correlates_feedback(self):
        child = self.ready_child(self.queue()); blasts.dispatch_blast(child.pk, now=self.now)
        delivery = child.deliveries.get()
        BlastDelivery.objects.filter(pk=delivery.pk).update(created_at=self.now-timedelta(days=14))
        event = {'type': 'email.delivered', 'sender': child.mailbox.email,
            'recipient': delivery.address, 'email_id': str(uuid.uuid4()), 'subject': child.subject, 'test': False}
        record_event(event, 'msg-late', now=self.now)
        delivery.refresh_from_db(); self.assertEqual(delivery.status, 'delivered')

    def test_paused_sender_is_not_reenabled_or_reassigned(self):
        parent = self.queue()
        original = list(sender_pool.children(parent.pk).values_list('pk', 'mailbox_id'))
        SenderMailbox.objects.filter(pk=self.mailboxes[0].pk).update(enabled=False)
        sender_pool.admit(self.now)
        self.assertCountEqual(list(sender_pool.children(parent.pk).values_list('pk', 'mailbox_id')), original)
        self.assertFalse(sender_pool.children(parent.pk).filter(mailbox=self.mailboxes[0], status='queued').exists())

    def test_final_start_rechecks_budget_under_lock(self):
        child = self.ready_child(self.queue())
        for _ in range(30): self.attempt(child.mailbox)
        blasts.dispatch_blast(child.pk, now=self.now)
        self.assertFalse(any(path.endswith('/status') and method=='PUT' for method,path in self.fake.calls))

    @override_settings(CRM_SENDER_POOL_DAILY_LIMIT=2)
    def test_provider_budget_is_shared_across_all_pool_senders_and_tests(self):
        child = self.ready_child(self.queue())
        other = next(m for m in self.mailboxes if m.pk != child.mailbox_id)
        self.attempt(other)
        self.attempt(other)
        blasts.dispatch_blast(child.pk, now=self.now)
        self.assertFalse(any(path.endswith('/status') and method == 'PUT' for method, path in self.fake.calls))
        with self.assertRaises(blasts.BlastError):
            sender_pool.reserve_test(child.mailbox_id, actor=self.admin, now=self.now)
        self.assertEqual(CrmAuditEvent.objects.filter(target_type=sender_pool.ATTEMPT).count(), 2)

    def test_sender_spacing_prevents_catch_up_bursts(self):
        parent=self.queue()
        for mailbox in self.mailboxes: self.attempt(mailbox, age=timedelta(minutes=10))
        sender_pool.admit(self.now)
        self.assertFalse(sender_pool.children(parent.pk).exclude(status='draft').exists())

    def test_new_drafts_use_pool_and_report_routing(self):
        draft=save_blast_draft(None, {'purpose':'marketing', 'channels':['email'],
            'email':{'subject':'News','body_markdown':'Hello'}}, actor=self.admin, now=self.now)
        self.assertTrue(sender_pool.is_parent(draft))
        self.assertEqual(sender_pool.describe(draft)['senders'], list(SENDERS))

    def test_old_attempts_with_pending_feedback_trigger_a_pool_stop(self):
        child=self.ready_child(self.queue()); blasts.dispatch_blast(child.pk, now=self.now)
        CrmAuditEvent.objects.filter(target_type=sender_pool.ATTEMPT).update(created_at=self.now-timedelta(hours=2))
        self.assertEqual(sender_pool.health_problem(self.now), 'feedback_missing')
        sender_pool.admit(self.now)
        self.assertFalse(SenderMailbox.objects.filter(email__in=SENDERS, enabled=True).exists())

    def test_removed_sender_cannot_fall_back_to_unlimited_single_sender_delivery(self):
        child=self.ready_child(self.queue())
        with override_settings(CRM_CAMPAIGN_SENDERS=tuple(s for s in SENDERS if s != child.mailbox.email)):
            blasts.dispatch_blast(child.pk, now=self.now)
        self.assertEqual(len(self.fake.campaigns), 0)

    def test_pool_pending_children_are_not_reported_finished_when_sender_is_paused(self):
        parent=self.queue(); child=self.ready_child(parent)
        SenderMailbox.objects.filter(pk=child.mailbox_id).update(enabled=False)
        self.assertEqual(blasts.refresh_status(child.pk, now=self.now), 'sending')
        self.assertEqual(blasts.refresh_status(parent.pk, now=self.now), 'sending')

    def test_mailbox_config_cannot_raise_pool_cap_or_redirect_replies(self):
        from community_chat.crm.services import save_mailbox, CrmError
        for data in ({'daily_limit': 31}, {'reply_to': 'shared@example.test'}):
            with self.assertRaises(CrmError): save_mailbox(self.mailboxes[0], data, actor=self.admin)


from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest import skipUnless
from django.db import connection, connections
from django.test import TransactionTestCase


@skipUnless(connection.vendor == 'postgresql', 'Row-lock concurrency requires PostgreSQL')
@override_settings(**SENDING, CRM_CAMPAIGN_SENDERS=SENDERS,
                   CRM_RESEND_MONITORED_SENDERS=WARMUP, CRM_SENDER_POOL_ENABLED=True,
                   CRM_RESEND_WEBHOOK_SECRET=SECRET)
class SenderPoolConcurrencyTests(TransactionTestCase):
    def test_overlapping_campaigns_cannot_take_the_same_final_quota_slot(self):
        now = timezone.now()
        admin = User.objects.create_user('operator@example.test', is_active=True, is_superuser=True)
        mailbox = SenderMailbox.objects.create(email=SENDERS[0], domain='mlai.au', route='resend',
            display_name='MLAI', reply_to=SENDERS[0], enabled=True, daily_limit=1)
        child_ids = []
        for i in range(2):
            person = Contact.objects.create(email=f'concurrent{i}@example.test', source='manual',
                                            marketing_consent='granted')
            parent = Blast.objects.create(purpose='marketing', created_by=admin, channels=['email'],
                status='queued', stats={sender_pool.PARENT: {'version':1}}, mailbox=mailbox)
            child = Blast.objects.create(purpose='marketing', created_by=admin, channels=['email'],
                status='queued', queued_at=now, mailbox=mailbox,
                stats={sender_pool.CHILD: {'parent_id':str(parent.pk)}})
            BlastDelivery.objects.create(blast=child, contact=person, channel='email',
                address=person.email, consent_basis='explicit', status='submitting')
            child_ids.append(child.pk)
        barrier = Barrier(2)
        def start(child_id):
            try:
                barrier.wait(timeout=10)
                return sender_pool.start_campaign(child_id, 123, now=now)
            finally:
                connections.close_all()
        with patch('crm.listmonk.set_campaign_status') as send:
            with ThreadPoolExecutor(max_workers=2) as workers:
                result = list(workers.map(start, child_ids))
        self.assertCountEqual(result, ['running', 'held'])
        self.assertEqual(send.call_count, 1)
        self.assertEqual(CrmAuditEvent.objects.filter(target_type=sender_pool.ATTEMPT).count(), 1)
