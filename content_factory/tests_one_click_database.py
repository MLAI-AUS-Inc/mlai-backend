"""Persisted one-click billing, delivery and cancellation regressions."""
from copy import deepcopy
from datetime import timedelta
from threading import Barrier, Event, Thread
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import close_old_connections, connection, connections, transaction
from django.test import TestCase, TransactionTestCase
from django.utils import timezone

from core.actor_ids import preferred_actor_id_for_user
from founder_tools.models import VibeRaisingCompany, VibeRaisingProfile
from roo.models import Ledger, PointsAccount
from roo.services import PointsService
from workflow_runs.models import ContentFactoryRun

from . import dispatch_outbox, vibe_marketing_views as views
from .dispatch_models import ContentFactoryDispatchOutbox
from .reconciliation import _settle_sweep_refunds
from .tests_website_connections import WebsiteDatabaseFixture
from .website_connections import transition_connection
from .website_contract import WebsiteAuthorityError
from .website_disconnect import refund_cancelled_runs, start_disconnect
from .website_models import WebsiteConnectionOperation


class OneClickBillingFixture(WebsiteDatabaseFixture):
    def setUp(self):
        super().setUp()
        User = get_user_model()
        self.requester = User.objects.create_user(email='requester@synthetic.example.test')
        self.payer = User.objects.create_user(email='payer@synthetic.example.test')
        for user in (self.requester, self.payer):
            profile = VibeRaisingProfile.objects.create(user=user, role='founder')
            company = VibeRaisingCompany.objects.create(profile=profile, organization=self.org,
                name='Synthetic', domain=self.org.domain)
            if user == self.requester:
                self.company = company
        self.org.billing_user = self.payer
        self.org.save(update_fields=['billing_user'])
        PointsService.credit_purchased_topup(user=self.payer, delta=20,
            description='Synthetic test funding', idempotency_key='synthetic-funding')
        self.actor = preferred_actor_id_for_user(self.requester)
        self.context = SimpleNamespace(organization=self.org, company=self.company)
        self.request = SimpleNamespace(user=self.requester, data={})

    def payload(self, key='synthetic-article'):
        return {**self.binding, 'domain': self.org.domain, 'github_repo': self.website.github_repo,
            'client_request_id': key, 'topic': 'Synthetic article', 'expected_cost_points': 6}

    def charge(self, payload):
        # Only the provider-backed readiness read is mocked. Wallet choice,
        # local consent locks, spend, operation and outbox all use the database.
        with patch.object(views, '_validate_article_start_admission'):
            return views._charge_roo_points_for_article(self.request, context=self.context, payload=payload)

    def balance(self, user):
        return PointsAccount.objects.get(user=user).balance_microroo

    def spend(self):
        return Ledger.objects.get(idempotency_key='content_factory:charge:synthetic-article')


class OneClickPersistenceTests(OneClickBillingFixture, TestCase):
    def test_repeated_start_commits_one_spend_and_one_original_payer_outbox(self):
        first = self.payload()
        payer, ledger, _, error = self.charge(first)
        self.assertIsNone(error)
        _, repeated_ledger, _, error = self.charge(self.payload())
        self.assertIsNone(error)
        self.assertEqual(ledger.pk, repeated_ledger.pk)
        self.assertEqual(payer.pk, self.payer.pk)
        self.assertEqual(self.balance(self.payer), 14_000_000)
        self.assertFalse(Ledger.objects.filter(user=self.requester, kind='SPEND').exists())
        self.assertEqual(Ledger.objects.filter(kind='SPEND').count(), 1)
        row = ContentFactoryDispatchOutbox.objects.get()
        self.assertEqual(row.payload['billing_user_id'], str(self.payer.pk))
        self.assertEqual(row.payload['request']['roo_points_requested_by_user_id'], self.requester.pk)
        self.assertEqual(row.payload['request']['operation_id'], first['operation_id'])
        self.assertEqual(ledger.created_by_slack_id, self.actor)
        self.assertEqual(WebsiteConnectionOperation.objects.filter(action='workflow').count(), 1)

    def test_outbox_insert_failure_rolls_back_the_real_spend_and_wallet(self):
        original = dispatch_outbox.reserve_dispatch
        def insert_then_fail(**kwargs):
            original(**kwargs)
            raise RuntimeError('Synthetic outbox persistence failure')
        with patch.object(dispatch_outbox, 'reserve_dispatch', side_effect=insert_then_fail):
            with self.assertRaisesRegex(RuntimeError, 'persistence failure'):
                self.charge(self.payload())
        self.assertEqual(self.balance(self.payer), 20_000_000)
        self.assertFalse(Ledger.objects.filter(kind='SPEND').exists())
        self.assertFalse(ContentFactoryDispatchOutbox.objects.exists())
        # Admission reservation persists safely before the charge transaction.
        self.assertEqual(WebsiteConnectionOperation.objects.filter(action='workflow').count(), 1)

    def test_repeated_start_after_company_payer_change_returns_typed_conflict(self):
        original_payload = self.payload()
        self.charge(original_payload)
        self.org.billing_user = None
        self.org.save(update_fields=['billing_user'])
        _, _, _, error = self.charge(self.payload())
        self.assertEqual(error.status_code, 409)
        self.assertEqual(error.data['code'], 'content_factory_billing_key_conflict')
        self.assertEqual(Ledger.objects.filter(kind='SPEND').count(), 1)
        self.assertEqual(self.balance(self.payer), 14_000_000)
        self.assertFalse(Ledger.objects.filter(user=self.requester).exists())
        row = ContentFactoryDispatchOutbox.objects.get()
        self.assertEqual(row.payload['billing_user_id'], str(self.payer.pk))
        self.assertEqual(row.payload['request']['roo_points_requested_by_user_id'], self.requester.pk)

    def test_conflicting_article_key_cannot_reuse_a_charge_or_outbox(self):
        self.charge(self.payload())
        drift = self.payload()
        drift['topic'] = 'A different article'
        _, _, _, error = self.charge(drift)
        self.assertEqual(error.status_code, 409)
        self.assertEqual(error.data['code'], 'operation_key_conflict')
        self.assertEqual(Ledger.objects.filter(kind='SPEND').count(), 1)
        self.assertEqual(ContentFactoryDispatchOutbox.objects.count(), 1)
        self.assertEqual(self.balance(self.payer), 14_000_000)

    def test_disconnect_during_admission_fences_the_transaction_before_any_spend(self):
        def disconnect(*args, **kwargs):
            transition_connection(self.config, action='disconnect', expected=self.binding)
        with patch.object(views, '_validate_article_start_admission', side_effect=disconnect):
            _, _, _, error = views._charge_roo_points_for_article(self.request, context=self.context, payload=self.payload())
        self.assertEqual(error.status_code, 409)
        self.assertFalse(Ledger.objects.filter(kind='SPEND').exists())
        self.assertFalse(ContentFactoryDispatchOutbox.objects.exists())
        self.assertEqual(self.balance(self.payer), 20_000_000)
        self.assertEqual(WebsiteConnectionOperation.objects.get(action='workflow').state, 'cancelled')

    def test_disconnect_refunds_cancelled_original_payer_once_and_keeps_pages(self):
        payload = self.payload()
        self.charge(payload)
        run = ContentFactoryRun.objects.create(run_id='synthetic-child', organization=self.org,
            domain=self.org.domain, github_repo=self.website.github_repo, workflow='article_generation',
            status='running', run_request=payload)
        op = start_disconnect(self.config, user=self.requester, data={**self.binding, 'remove_setup': False})
        run.refresh_from_db()
        self.website.refresh_from_db()
        self.assertEqual(run.status, 'cancelled')
        self.assertEqual(self.website.generation, self.binding['connection_generation'] + 1)
        self.assertEqual(op.payload['refund_run_ids'], [run.run_id])
        self.assertTrue(op.receipt['keep_pages'])
        self.org.billing_user = None
        self.org.save(update_fields=['billing_user'])
        self.assertTrue(refund_cancelled_runs(op))
        self.assertTrue(refund_cancelled_runs(op))
        refund = Ledger.objects.get(kind='REFUND')
        self.assertEqual(refund.user_id, self.payer.pk)
        self.assertEqual(refund.refund_of_id, self.spend().pk)
        self.assertEqual(refund.created_by_slack_id, self.actor)
        self.assertEqual(self.balance(self.payer), 20_000_000)
        self.assertFalse(Ledger.objects.filter(user=self.requester).exists())
        run.refresh_from_db()
        self.assertEqual(run.run_request['roo_points_billing_status'], 'refunded')

    def test_lost_delivery_lookup_binds_once_without_dispatching_or_charging_again(self):
        payload = self.payload()
        self.charge(payload)
        ContentFactoryRun.objects.create(run_id=payload['client_request_id'], organization=self.org,
            domain=self.org.domain, github_repo=self.website.github_repo, workflow='article_generation',
            status='queued', run_request=payload)
        now = timezone.now() + timedelta(minutes=1)
        with patch.object(views, '_content_factory_remote_config', return_value={'enabled': True, 'base_url': 'https://worker.invalid'}), \
                patch.object(views, '_lookup_content_factory_dispatch_by_key', return_value=('dispatched', {'run_id': 'actual-worker-child'})), \
                patch('integrations.http_client.post') as post:
            self.assertEqual(dispatch_outbox.process_dispatch_outbox(now=now)['delivered'], 1)
            self.assertEqual(dispatch_outbox.process_dispatch_outbox(now=now)['processed'], 0)
        post.assert_not_called()
        self.assertEqual(ContentFactoryRun.objects.count(), 1)
        self.assertEqual(ContentFactoryRun.objects.get().run_id, 'actual-worker-child')
        row = ContentFactoryDispatchOutbox.objects.get()
        self.assertEqual(row.state, 'delivered')
        self.assertEqual(row.attempts, 1)
        self.assertEqual(WebsiteConnectionOperation.objects.get(action='workflow').payload['run_id'], row.run_id)
        self.assertEqual(Ledger.objects.filter(kind='SPEND').count(), 1)

    def test_rejected_outbox_retries_failed_refund_without_another_delivery(self):
        self.charge(self.payload())
        now = timezone.now() + timedelta(minutes=1)
        with patch.object(dispatch_outbox, '_deliver', return_value=('failed', 'dispatch_rejected', '')) as deliver, \
                patch('integrations.services.article_generation.refund_content_factory_request_for_user', side_effect=RuntimeError('Synthetic ledger unavailable')):
            self.assertEqual(dispatch_outbox.process_dispatch_outbox(now=now)['pending'], 1)
        row = ContentFactoryDispatchOutbox.objects.get()
        self.assertEqual(row.state, 'refund_pending')
        self.assertEqual(row.attempts, 1)
        with patch.object(dispatch_outbox, '_deliver') as retry_delivery:
            self.assertEqual(dispatch_outbox.process_dispatch_outbox(now=row.next_attempt_at + timedelta(seconds=1))['failed'], 1)
            self.assertEqual(dispatch_outbox.process_dispatch_outbox(now=now + timedelta(days=1))['processed'], 0)
        retry_delivery.assert_not_called()
        deliver.assert_called_once()
        self.assertEqual(Ledger.objects.filter(kind='REFUND').count(), 1)
        self.assertEqual(self.balance(self.payer), 20_000_000)
        with self.assertRaises(WebsiteAuthorityError) as denied:
            self.charge(self.payload())
        self.assertEqual(denied.exception.code, 'dispatch_already_refunded')
        self.assertEqual(Ledger.objects.filter(kind='SPEND').count(), 1)
        self.assertEqual(self.balance(self.payer), 20_000_000)

    def test_late_outbox_lookup_after_disconnect_preserves_completed_history_without_refund(self):
        payload = self.payload()
        self.charge(payload)
        run = ContentFactoryRun.objects.create(run_id='completed-child', organization=self.org,
            domain=self.org.domain, github_repo=self.website.github_repo, workflow='article_generation',
            status='completed', run_request=payload, result={'draft': 'Retained reviewed article'})
        transition_connection(self.config, action='disconnect', expected=self.binding)
        with patch.object(views, '_content_factory_remote_config', return_value={'enabled': True, 'base_url': 'https://worker.invalid'}), \
                patch.object(views, '_lookup_content_factory_dispatch_by_key', return_value=('dispatched', {'run_id': run.run_id})), \
                patch('integrations.http_client.post') as post:
            self.assertEqual(dispatch_outbox.process_dispatch_outbox(now=timezone.now() + timedelta(minutes=1))['delivered'], 1)
        post.assert_not_called()
        run.refresh_from_db()
        self.assertEqual(run.status, 'completed')
        self.assertEqual(run.result['draft'], 'Retained reviewed article')
        self.assertEqual(ContentFactoryDispatchOutbox.objects.get().state, 'delivered')
        self.assertFalse(Ledger.objects.filter(kind='REFUND').exists())
        self.assertEqual(self.balance(self.payer), 14_000_000)

    def test_deferred_refund_retries_original_spend_and_preserves_newer_result(self):
        payload = self.payload()
        self.charge(payload)
        run = ContentFactoryRun.objects.create(run_id='failed-child', organization=self.org,
            domain=self.org.domain, workflow='article_generation', status='failed', run_request={**payload,
                'dispatch_pending_resolution': False, 'pending_billing_refund': {
                    'charged_user_id': self.payer.pk, 'actor_id': self.actor,
                    'article_request': deepcopy(payload), 'kind': 'article_generation'}},
            result={'reconciliation_refund_pending': True, 'original_receipt': 'retained'})
        with patch.object(views, '_refund_roo_points_for_article_start', side_effect=RuntimeError('Synthetic ledger unavailable')):
            views._process_pending_dispatch_refund(run)
        run.refresh_from_db()
        self.assertTrue(run.result['reconciliation_refund_pending'])
        self.assertFalse(run.result.get('dispatch_refund_processed'))
        original = views._refund_roo_points_for_article_start
        def refund_then_advance(**kwargs):
            ledger = original(**kwargs)
            ContentFactoryRun.objects.filter(pk=run.pk).update(status='cancelled',
                result={'original_receipt': 'retained', 'newer_receipt': 'kept', 'reconciliation_refund_pending': True})
            return ledger
        with patch.object(views, '_refund_roo_points_for_article_start', side_effect=refund_then_advance) as refund:
            _settle_sweep_refunds(limit=10)
            _settle_sweep_refunds(limit=10)
        refund.assert_called_once()
        run.refresh_from_db()
        self.assertEqual(run.status, 'cancelled')
        self.assertEqual(run.result['newer_receipt'], 'kept')
        self.assertTrue(run.result['dispatch_refund_processed'])
        self.assertFalse(run.result['reconciliation_refund_pending'])
        self.assertEqual(run.run_request['roo_points_requested_by_user_id'], self.requester.pk)
        self.assertEqual(run.run_request['roo_points_billing_user_id'], self.payer.pk)
        self.assertEqual(Ledger.objects.filter(kind='REFUND').count(), 1)
        self.assertEqual(self.balance(self.payer), 20_000_000)

    def paid_source(self):
        payload = self.payload()
        self.charge(payload)
        return ContentFactoryRun.objects.create(run_id='original-paid-source', organization=self.org,
            domain=self.org.domain, github_repo=self.website.github_repo, workflow='article_generation',
            status='failed', run_request=payload)

    def test_company_funded_terminal_failure_refunds_the_ledger_payer_after_opt_out(self):
        from .models import ContentFactoryJob
        from integrations.services.article_generation import maybe_auto_refund_terminal_failure
        source = self.paid_source()
        job = ContentFactoryJob.objects.create(job_id=source.run_id, domain=self.org.domain,
            slack_user_id=self.actor, client_request_id=source.run_request['client_request_id'],
            billing_status='charged', billing_amount=6, billing_ledger=self.spend(),
            request_meta={**source.run_request, 'requested_by_slack_user_id': self.actor,
                'user_email': self.requester.email})
        self.org.billing_user = None
        self.org.save(update_fields=['billing_user'])
        self.assertEqual(maybe_auto_refund_terminal_failure(job, error_code='WORKER_RECOVERY_EXHAUSTED',
            error_message='No draft delivered', refundable=True), (True, 6))
        job.refresh_from_db()
        self.assertEqual(job.billing_status, 'refunded')
        self.assertEqual(maybe_auto_refund_terminal_failure(job, error_code='WORKER_RECOVERY_EXHAUSTED',
            error_message='No draft delivered', refundable=True), (False, 0))
        refund = Ledger.objects.get(kind='REFUND')
        self.assertEqual(refund.user_id, self.payer.pk)
        self.assertEqual(refund.created_by_slack_id, self.actor)
        self.assertEqual(self.balance(self.payer), 20_000_000)
        self.assertFalse(Ledger.objects.filter(user=self.requester).exists())

    def test_terminal_refund_failure_is_retried_by_sweep_with_original_payer(self):
        from .models import ContentFactoryJob
        from .reconciliation import run_content_factory_reconciliation_sweep
        from integrations.services.article_generation import _handle_status_failure
        source = self.paid_source()
        job = ContentFactoryJob.objects.create(job_id=source.run_id, domain=self.org.domain,
            slack_user_id=self.actor, status='researching',
            client_request_id=source.run_request['client_request_id'], billing_status='charged',
            billing_amount=6, billing_ledger=self.spend(), request_meta=source.run_request)
        self.org.billing_user = None
        self.org.save(update_fields=['billing_user'])
        def unavailable_credit(**kwargs):
            ContentFactoryRun.objects.filter(pk=source.pk).update(status='cancelled',
                result={'worker_receipt': 'newer', 'reconciliation_refund_pending': True})
            raise RuntimeError('Synthetic transient refund service failure')
        with patch.object(PointsService, 'refund', side_effect=unavailable_credit), \
                patch('integrations.services.slack.SlackService.send_dm'):
            _handle_status_failure(job.job_id, {'error_code': 'RUN_BUDGET_EXHAUSTED', 'error': 'No draft delivered'})
        job.refresh_from_db()
        source.refresh_from_db()
        self.assertEqual(job.status, 'error')
        self.assertEqual(job.billing_status, 'charged')
        self.assertTrue(source.result['reconciliation_refund_pending'])
        self.assertEqual(source.result['article_terminal_refund']['status'], 'pending')
        self.assertFalse(Ledger.objects.filter(kind='REFUND').exists())
        # A repeat poll exits early; the durable scheduler owns the refund now.
        _handle_status_failure(job.job_id, {'error_code': 'RUN_BUDGET_EXHAUSTED'})
        self.org.billing_user = self.requester
        self.org.save(update_fields=['billing_user'])
        with patch('integrations.http_client.post') as dispatch:
            run_content_factory_reconciliation_sweep(now=timezone.now() + timedelta(minutes=10))
            run_content_factory_reconciliation_sweep(now=timezone.now() + timedelta(minutes=20))
        dispatch.assert_not_called()
        source.refresh_from_db()
        job.refresh_from_db()
        self.assertEqual(source.status, 'cancelled')
        self.assertEqual(source.result['worker_receipt'], 'newer')
        self.assertFalse(source.result['reconciliation_refund_pending'])
        self.assertEqual(source.result['article_terminal_refund']['status'], 'refunded')
        self.assertEqual(job.billing_status, 'refunded')
        self.assertEqual(self.balance(self.payer), 20_000_000)
        self.assertFalse(Ledger.objects.filter(user=self.requester).exists())
        self.assertEqual(Ledger.objects.filter(kind='REFUND').count(), 1)

    def test_terminal_refund_with_mismatched_charge_stays_pending_in_sweep(self):
        from .models import ContentFactoryJob
        from .reconciliation import run_content_factory_reconciliation_sweep
        from integrations.services.article_generation import maybe_auto_refund_terminal_failure
        source = self.paid_source()
        source.run_request = {**source.run_request, 'roo_points_billing_user_id': self.requester.pk}
        source.save(update_fields=['run_request'])
        job = ContentFactoryJob.objects.create(job_id=source.run_id, domain=self.org.domain,
            slack_user_id=self.actor, client_request_id=source.run_request['client_request_id'],
            billing_status='charged', billing_amount=6, billing_ledger=self.spend(), request_meta=source.run_request)
        self.assertEqual(maybe_auto_refund_terminal_failure(job, error_code='WORKER_RECOVERY_EXHAUSTED',
            error_message='No draft delivered', refundable=True), (False, 0))
        source.refresh_from_db()
        self.assertTrue(source.result['reconciliation_refund_pending'])
        run_content_factory_reconciliation_sweep(now=timezone.now() + timedelta(minutes=10))
        source.refresh_from_db()
        self.assertTrue(source.result['reconciliation_refund_pending'])
        self.assertEqual(source.result['article_terminal_refund']['status'], 'pending')
        self.assertFalse(Ledger.objects.filter(kind='REFUND').exists())
        self.assertEqual(self.balance(self.payer), 14_000_000)

    def test_terminal_paid_job_without_run_payment_metadata_stays_pending(self):
        from .models import ContentFactoryJob
        from .reconciliation import run_content_factory_reconciliation_sweep
        from integrations.services.article_generation import maybe_auto_refund_terminal_failure
        source = self.paid_source()
        source.run_request = dict(self.binding)
        source.save(update_fields=['run_request'])
        job = ContentFactoryJob.objects.create(job_id=source.run_id, domain=self.org.domain,
            slack_user_id=self.actor, client_request_id='unresolved-charge',
            billing_status='charged', billing_amount=6, request_meta={})
        self.assertEqual(maybe_auto_refund_terminal_failure(job, error_code='RUN_BUDGET_EXHAUSTED',
            error_message='No draft delivered', refundable=True), (False, 0))
        source.refresh_from_db()
        self.assertTrue(source.result['reconciliation_refund_pending'])
        run_content_factory_reconciliation_sweep(now=timezone.now() + timedelta(minutes=10))
        source.refresh_from_db()
        self.assertTrue(source.result['reconciliation_refund_pending'])
        self.assertEqual(source.result['article_terminal_refund']['status'], 'pending')
        self.assertFalse(Ledger.objects.filter(kind='REFUND').exists())
        self.assertEqual(self.balance(self.payer), 14_000_000)

    def test_disconnect_reused_child_refunds_original_charge_once_after_opt_out(self):
        source = self.paid_source()
        child = ContentFactoryRun.objects.create(run_id='restarted-child', organization=self.org,
            domain=self.org.domain, github_repo=self.website.github_repo, workflow='article_generation',
            status='running', run_request={**source.run_request, 'client_request_id': 'fresh-restart-key',
                'roo_points_billing_status': 'reused', 'original_billing_source_run_id': source.run_id})
        self.org.billing_user = None
        self.org.save(update_fields=['billing_user'])
        op = start_disconnect(self.config, user=self.requester, data=self.binding)
        self.assertTrue(refund_cancelled_runs(op))
        self.assertIn(child.run_id, op.receipt['refunded_run_ids'])
        # Re-read an unsaved receipt, simulating a crash after the ledger credit.
        op.refresh_from_db()
        self.assertTrue(refund_cancelled_runs(op))
        self.assertEqual(Ledger.objects.filter(kind='REFUND').count(), 1)
        refund = Ledger.objects.get(kind='REFUND')
        self.assertEqual(refund.user_id, self.payer.pk)
        self.assertEqual(refund.refund_of_id, self.spend().pk)
        self.assertEqual(refund.idempotency_key, 'content_factory:refund:synthetic-article')
        self.assertEqual(self.balance(self.payer), 20_000_000)
        child.refresh_from_db()
        self.assertEqual(child.run_request['client_request_id'], 'fresh-restart-key')
        self.assertEqual(child.run_request['roo_points_billing_status'], 'refunded')

    def test_disconnect_paid_unresolved_charge_remains_pending(self):
        run = ContentFactoryRun.objects.create(run_id='missing-paid-child', organization=self.org,
            domain=self.org.domain, github_repo=self.website.github_repo, workflow='article_generation',
            status='running', run_request={**self.binding, 'client_request_id': 'missing-charge',
                'roo_points_cost': 6, 'roo_points_billing_status': 'charged'})
        op = start_disconnect(self.config, user=self.requester, data=self.binding)
        self.assertFalse(refund_cancelled_runs(op))
        self.assertEqual(op.receipt['refund_pending_run_ids'], [run.run_id])
        self.assertNotIn(run.run_id, op.receipt['refunded_run_ids'])
        self.assertFalse(Ledger.objects.filter(kind='REFUND').exists())

    def test_paid_refund_without_original_spend_is_retryable_and_free_refund_is_empty(self):
        from integrations.services.article_generation import ArticleGenerationError, refund_content_factory_request_for_user
        kwargs = dict(user=self.payer, actor_id=self.actor, resolved_domain=self.org.domain, reason='Queue refused')
        with self.assertRaises(ArticleGenerationError):
            refund_content_factory_request_for_user(article_request={'client_request_id': 'missing-paid',
                'roo_points_cost': 6, 'roo_points_billing_status': 'charged'}, **kwargs)
        self.assertIsNone(refund_content_factory_request_for_user(article_request={'client_request_id': 'free-setup',
            'roo_points_cost': 0, 'roo_points_billing_status': 'free'}, **kwargs))
        self.assertFalse(Ledger.objects.filter(kind='REFUND').exists())

    def test_foreign_or_mismatched_reused_charge_cannot_credit_a_payer(self):
        from organizations.models import Organization
        source = self.paid_source()
        foreign_org = Organization.objects.create(domain='foreign.synthetic.example.test', name='Foreign synthetic')
        foreign = get_user_model().objects.create_user(email='foreign@synthetic.example.test')
        PointsService.credit_purchased_topup(user=foreign, delta=10, description='Synthetic funding', idempotency_key='foreign-funding')
        ledger, _ = PointsService.spend(user=foreign, delta=6, source='CONTENT_FACTORY', description='Synthetic foreign draft',
            created_by_slack_id='synthetic', idempotency_key='content_factory:charge:foreign-key',
            reference_type='CONTENT_FACTORY', reference_id='foreign-key')
        foreign_run = ContentFactoryRun.objects.create(run_id='foreign-source', organization=foreign_org,
            domain=foreign_org.domain, workflow='article_generation', status='failed',
            run_request={'client_request_id': 'foreign-key', 'roo_points_ledger_id': str(ledger.pk)})
        children = []
        for index, metadata in enumerate((
                {'roo_points_ledger_id': str(ledger.pk), 'original_billing_source_run_id': source.run_id},
                {'roo_points_ledger_id': str(ledger.pk), 'original_billing_source_run_id': foreign_run.run_id},
                {'roo_points_billing_user_id': self.requester.pk, 'original_billing_source_run_id': source.run_id})):
            children.append(ContentFactoryRun.objects.create(run_id=f'forged-child-{index}', organization=self.org,
                domain=self.org.domain, github_repo=self.website.github_repo, workflow='article_generation', status='running',
                run_request={**source.run_request, 'client_request_id': f'forged-restart-{index}',
                    'roo_points_billing_status': 'reused', **metadata}))
        op = start_disconnect(self.config, user=self.requester, data=self.binding)
        self.assertFalse(refund_cancelled_runs(op))
        self.assertEqual(set(op.receipt['refund_pending_run_ids']), {child.run_id for child in children})
        self.assertFalse(op.receipt['refunded_run_ids'])
        self.assertFalse(Ledger.objects.filter(kind='REFUND').exists())
        self.assertEqual(self.balance(foreign), 4_000_000)
        self.assertEqual(self.balance(self.payer), 14_000_000)


class OneClickTransactionTests(OneClickBillingFixture, TransactionTestCase):
    def setUp(self):
        super().setUp()
        if connection.vendor != 'postgresql':
            self.skipTest('Real row-lock guarantees require PostgreSQL')

    def test_parallel_repeated_start_commits_one_spend_and_one_outbox(self):
        ready = Barrier(2)
        errors, results = [], []
        def admission(*args, **kwargs):
            self.assertFalse(transaction.get_connection().in_atomic_block)
            ready.wait(timeout=10)
        def start():
            close_old_connections()
            try:
                results.append(views._charge_roo_points_for_article(self.request,
                    context=self.context, payload=self.payload()))
            except Exception as exc:
                errors.append(exc)
            finally:
                connections.close_all()
        with patch.object(views, '_validate_article_start_admission', side_effect=admission):
            workers = [Thread(target=start), Thread(target=start)]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(15)
        self.assertFalse(any(worker.is_alive() for worker in workers))
        self.assertFalse(errors)
        self.assertEqual(len(results), 2)
        self.assertTrue(all(result[3] is None for result in results))
        self.assertEqual(results[0][1].pk, results[1][1].pk)
        self.assertEqual(Ledger.objects.filter(kind='SPEND').count(), 1)
        self.assertEqual(ContentFactoryDispatchOutbox.objects.count(), 1)
        self.assertEqual(WebsiteConnectionOperation.objects.filter(action='workflow').count(), 1)
        self.assertEqual(self.balance(self.payer), 14_000_000)

    def test_disconnect_waits_for_complete_charge_and_outbox_transaction(self):
        entered, release, disconnect_started, disconnected = Event(), Event(), Event(), Event()
        errors = []
        original = dispatch_outbox.reserve_dispatch
        def pause_reservation(**kwargs):
            self.assertTrue(transaction.get_connection().in_atomic_block)
            entered.set()
            self.assertTrue(release.wait(10))
            return original(**kwargs)
        def charge_thread():
            close_old_connections()
            try:
                self.assertIsNone(self.charge(self.payload())[3])
            except Exception as exc:
                errors.append(exc)
            finally:
                connections.close_all()
        def disconnect_thread():
            close_old_connections()
            try:
                disconnect_started.set()
                transition_connection(self.config, action='disconnect', expected=self.binding)
                disconnected.set()
            except Exception as exc:
                errors.append(exc)
            finally:
                connections.close_all()
        with patch.object(dispatch_outbox, 'reserve_dispatch', side_effect=pause_reservation):
            charging = Thread(target=charge_thread)
            disconnecting = Thread(target=disconnect_thread)
            charging.start()
            try:
                self.assertTrue(entered.wait(10))
                disconnecting.start()
                self.assertTrue(disconnect_started.wait(10))
                self.assertFalse(disconnected.wait(.2), 'Disconnect must serialize after the whole charge/outbox transaction')
            finally:
                release.set()
                charging.join(10)
                if disconnecting.ident:
                    disconnecting.join(10)
        self.assertFalse(charging.is_alive())
        self.assertFalse(disconnecting.is_alive())
        self.assertFalse(errors)
        self.assertTrue(disconnected.is_set())
        self.assertEqual(Ledger.objects.filter(kind='SPEND').count(), 1)
        self.assertEqual(ContentFactoryDispatchOutbox.objects.count(), 1)
        self.website.refresh_from_db()
        self.assertEqual(self.website.state, 'disconnected')

    def test_two_outbox_sweeps_claim_only_one_remote_delivery(self):
        self.charge(self.payload())
        entered, release = Event(), Event()
        errors, results = [], []
        now = timezone.now() + timedelta(minutes=1)
        def deliver(row):
            self.assertFalse(transaction.get_connection().in_atomic_block)
            entered.set()
            self.assertTrue(release.wait(10))
            return 'delivered', '', 'only-child'
        def first_sweep():
            close_old_connections()
            try:
                results.append(dispatch_outbox.process_dispatch_outbox(now=now))
            except Exception as exc:
                errors.append(exc)
            finally:
                connections.close_all()
        with patch.object(dispatch_outbox, '_deliver', side_effect=deliver) as delivery:
            worker = Thread(target=first_sweep)
            worker.start()
            try:
                self.assertTrue(entered.wait(10))
                self.assertEqual(dispatch_outbox.process_dispatch_outbox(now=now)['processed'], 0)
            finally:
                release.set()
                worker.join(10)
        self.assertFalse(worker.is_alive())
        self.assertFalse(errors)
        delivery.assert_called_once()
        self.assertEqual(results[0]['delivered'], 1)
        row = ContentFactoryDispatchOutbox.objects.get()
        self.assertEqual((row.state, row.run_id, row.attempts), ('delivered', 'only-child', 1))
        self.assertEqual(Ledger.objects.filter(kind='SPEND').count(), 1)
