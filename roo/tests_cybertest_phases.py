"""Run only in the specifically approved disposable database inventory."""
import secrets
import uuid
from datetime import timedelta
from django.test import TestCase, override_settings
from django.utils import timezone
from core.models import User
from roo.models import CybertestCheckout, Ledger, PointsAccount
from unittest.mock import patch
from roo import cybertest_checkout as checkout
from roo.cybertest_policy import CheckoutError
from roo.test_cybertest_delivery import basic, upgrade


@override_settings(CYBERTEST_BILLING_MODE='test', CYBERTEST_PHASED_SCANS_ENABLED=True,
                   CYBERTEST_TEST_BALANCE_POINTS=1000, CYBERTEST_AUDIT_POINTS=10,
                   CYBERTEST_USD_PER_AUD='0.5', CYBERTEST_AUD_PER_POINT='2')
class PhaseCheckoutTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(email='phase-payer@example.invalid')
        self.other = User.objects.create_user(email='other-phase-payer@example.invalid')
        self.intent = {'github_actor_id': '123', 'repository_id': '456',
                       'workspace_id': str(uuid.uuid4()), 'repository': 'fixture/project',
                       'scope': ['get-client-referrals'], 'mlai_user_id': str(self.user.pk),
                       'mlai_company_id': str(uuid.uuid4())}

    def purchase(self, contract):
        return {**self.intent, 'purchase': {**contract, 'commitSha': 'c'*40, 'sourceManifestSha256': 'd'*64}}

    def start(self, contract, expected_points):
        quote, audit, token = uuid.uuid4(), uuid.uuid4(), secrets.token_urlsafe(32)
        intent = self.purchase(contract)
        result = checkout.create_quote(quote, token, intent)
        self.assertEqual(result['points'], expected_points)
        checkout.confirm(quote, token, self.user, expected_points=expected_points)
        checkout.charge(quote, audit, intent)
        checkout.start_dispatch(quote, audit)
        return quote, audit, intent

    def result(self, audit, contract, *, missing_required=False):
        return {'auditId': str(audit), 'planId': contract['planId'],
                'checks': [{'id': c['id'], 'status': 'error' if missing_required and c['required'] else 'completed'} for c in contract['checks']],
                'report': {'persisted': True, 'sha256': 'b'*64}}

    def delivered_basic(self):
        quote, audit, intent = self.start(basic(), 10)
        checkout.settle_delivery(quote, audit, self.result(audit, basic()), None)
        return quote, audit, intent

    def test_upgrade_plan_recovery_is_bound_to_parent_and_survives_settlement_without_repurchase(self):
        import json
        from pathlib import Path
        parent, base_audit, _ = self.delivered_basic()
        snapshot = json.loads((Path(__file__).parent/'fixtures/upgrade-execution-plan-v1.json').read_text())
        contract = snapshot['contract']
        contract.update(parentCheckoutId=str(parent), commitSha='c'*40, sourceManifestSha256='d'*64)
        parent_receipt = checkout.status(parent)
        snapshot.update(contract=contract, commitSha='c'*40, sourceManifestSha256='d'*64,
                        parentAuditId=str(base_audit), scope=self.intent['scope'],
                        baseReportSha256='b'*64,
                        baseEvidenceSha256=parent_receipt['settlement']['delivery']['evidenceSha256'])
        quote, token, audit = uuid.uuid4(), secrets.token_urlsafe(32), uuid.uuid4()
        intent = self.purchase(contract)
        checkout.create_quote(quote, token, intent, execution_plan=snapshot)
        checkout.confirm(quote, token, self.user, expected_points=24)
        recover = lambda selection=self.intent, parent_id=str(parent): checkout.recover_quote(
            selection, secrets.token_urlsafe(32), parent_checkout_id=parent_id)
        self.assertIsNone(checkout.recover_quote(self.intent, secrets.token_urlsafe(32)))
        self.assertIsNone(recover({**self.intent, 'mlai_user_id': str(self.other.pk)}))
        self.assertIsNone(recover(parent_id=str(uuid.uuid4())))
        self.assertEqual(recover()['execution_plan'], snapshot)
        self.assertEqual(recover()['status'], 'authorized')
        self.assertEqual(checkout.balance(self.user, test_mode=True), 990)
        checkout.charge(quote, audit, intent)
        checkout.start_dispatch(quote, audit)
        checkout.settle_delivery(quote, audit, self.result(audit, contract), None)
        self.assertEqual(recover()['checkout_id'], str(quote))
        self.assertEqual(recover()['settlement']['basis'], 'phase_delivered')
        self.assertEqual(CybertestCheckout.objects.count(), 2)
        self.assertEqual(checkout.balance(self.user, test_mode=True), 966)

    def test_upgrade_execution_plan_must_reference_the_actual_delivered_base(self):
        import json
        from pathlib import Path
        parent, _, _ = self.delivered_basic()
        snapshot = json.loads((Path(__file__).parent/'fixtures/upgrade-execution-plan-v1.json').read_text())
        contract = snapshot['contract']
        contract.update(parentCheckoutId=str(parent), commitSha='c'*40, sourceManifestSha256='d'*64)
        snapshot.update(contract=contract, commitSha='c'*40, sourceManifestSha256='d'*64, scope=self.intent['scope'])
        with self.assertRaises(CheckoutError) as caught:
            checkout.create_quote(uuid.uuid4(), secrets.token_urlsafe(32), self.purchase(contract), execution_plan=snapshot)
        self.assertEqual(caught.exception.code, 'UPGRADE_BASE_CHANGED')

    def test_plan_and_authorized_receipt_recover_after_service_restart_without_repurchase(self):
        from roo.test_cybertest_plan import plan
        quote, token = uuid.uuid4(), secrets.token_urlsafe(32)
        intent, snapshot = self.purchase(basic()), plan()
        checkout.create_quote(quote, token, intent, execution_plan=snapshot)
        checkout.confirm(quote, token, self.user, expected_points=10)
        restored = checkout.recover_quote(self.intent, secrets.token_urlsafe(32))
        self.assertEqual(restored['checkout_id'], str(quote))
        self.assertEqual(restored['execution_plan'], snapshot)
        self.assertEqual(restored['status'], 'authorized')
        self.assertEqual(CybertestCheckout.objects.count(), 1)
        self.assertEqual(Ledger.objects.count(), 0)
        self.assertEqual(checkout.balance(self.user, test_mode=True), 1000)
        self.assertNotIn('executionPlan', restored['phase_contract'])

    def test_architecture_change_replaces_only_unaccepted_basic_quote(self):
        from roo.test_cybertest_plan import plan
        legacy, token = uuid.uuid4(), secrets.token_urlsafe(32)
        intent = self.purchase(basic())
        checkout.create_quote(legacy, token, intent, execution_plan=plan())
        self.assertIsNone(checkout.recover_quote(self.intent, token, backend_answer='no'))
        self.assertEqual(CybertestCheckout.objects.get(pk=legacy).status, 'cancelled')
        new, token = uuid.uuid4(), secrets.token_urlsafe(32)
        updated = {**plan(), 'algorithm': 'basic-source-v2', 'backendAnswer': 'no'}
        checkout.create_quote(new, token, intent, execution_plan=updated)
        self.assertEqual(checkout.recover_quote(self.intent, token, backend_answer='no')['checkout_id'], str(new))
        checkout.confirm(new, token, self.user, expected_points=10)
        audit = uuid.uuid4()
        for status in ('authorized', 'charged', 'started'):
            if status == 'charged':
                checkout.charge(new, audit, intent)
            elif status == 'started':
                checkout.start_dispatch(new, audit)
            with self.assertRaises(CheckoutError) as error:
                checkout.recover_quote(self.intent, token, backend_answer='yes')
            self.assertEqual(error.exception.code, 'BACKEND_ANSWER_LOCKED')
            self.assertEqual(CybertestCheckout.objects.get(pk=new).status, status)
        self.assertEqual(CybertestCheckout.objects.count(), 2)

    def test_unconfirmed_recovery_rotates_link_without_changing_scope_or_allowing_other_payer(self):
        from roo.test_cybertest_plan import plan
        quote, old_token, new_token = uuid.uuid4(), secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        checkout.create_quote(quote, old_token, self.purchase(basic()), execution_plan=plan())
        self.assertIsNone(checkout.recover_quote({**self.intent, 'mlai_user_id': str(self.other.pk)}, new_token))
        restored = checkout.recover_quote(self.intent, new_token)
        self.assertEqual(restored['checkout_id'], str(quote))
        with self.assertRaises(CheckoutError):
            checkout.preview(quote, old_token, self.user)
        self.assertEqual(checkout.preview(quote, new_token, self.user)['status'], 'quoted')
        row = CybertestCheckout.objects.get(pk=quote)
        row.expires_at = timezone.now()-timedelta(seconds=1)
        row.save(update_fields=['expires_at'])
        self.assertIsNone(checkout.recover_quote(self.intent, new_token))

    def test_execution_snapshot_cannot_change_on_quote_replay_or_use_another_scope(self):
        from roo.test_cybertest_plan import plan
        quote, token = uuid.uuid4(), secrets.token_urlsafe(32)
        checkout.create_quote(quote, token, self.purchase(basic()), execution_plan=plan())
        changed = plan()
        changed['execution']['maxModelUsd'] = 6
        with self.assertRaises(CheckoutError):
            checkout.create_quote(quote, token, self.purchase(basic()), execution_plan=changed)
        changed = plan()
        changed['scope'] = ['other']
        with self.assertRaises(CheckoutError):
            checkout.create_quote(uuid.uuid4(), secrets.token_urlsafe(32), self.purchase(basic()), execution_plan=changed)

    def test_different_basic_quote_ids_share_one_pending_purchase_until_it_is_settled(self):
        from roo.test_cybertest_plan import plan
        quote, token, audit = uuid.uuid4(), secrets.token_urlsafe(32), uuid.uuid4()
        intent = self.purchase(basic())
        checkout.create_quote(quote, token, intent, execution_plan=plan())
        with self.assertRaises(CheckoutError) as caught:
            checkout.create_quote(uuid.uuid4(), secrets.token_urlsafe(32), intent, execution_plan=plan())
        self.assertEqual(caught.exception.code, 'BASIC_PURCHASE_PENDING')
        self.assertEqual(CybertestCheckout.objects.count(), 1)
        checkout.confirm(quote, token, self.user, expected_points=10)
        checkout.charge(quote, audit, intent)
        checkout.start_dispatch(quote, audit)
        CybertestCheckout.objects.filter(pk=quote).update(expires_at=timezone.now()-timedelta(seconds=1))
        with self.assertRaises(CheckoutError) as caught:
            checkout.create_quote(uuid.uuid4(), secrets.token_urlsafe(32), intent, execution_plan=plan())
        self.assertEqual(caught.exception.code, 'BASIC_PURCHASE_PENDING')
        self.assertEqual(checkout.balance(self.user, test_mode=True), 990)
        checkout.settle_delivery(quote, audit, self.result(audit, basic()), None)
        checkout.create_quote(uuid.uuid4(), secrets.token_urlsafe(32), intent, execution_plan=plan())
        self.assertEqual(CybertestCheckout.objects.count(), 2)

    def test_expired_uncharged_basic_quote_and_other_company_do_not_block_new_purchase(self):
        from roo.test_cybertest_plan import plan
        quote = uuid.uuid4()
        intent = self.purchase(basic())
        checkout.create_quote(quote, secrets.token_urlsafe(32), intent, execution_plan=plan())
        checkout.create_quote(uuid.uuid4(), secrets.token_urlsafe(32),
                              {**intent, 'mlai_company_id': str(uuid.uuid4())}, execution_plan=plan())
        CybertestCheckout.objects.filter(pk=quote).update(expires_at=timezone.now()-timedelta(seconds=1))
        checkout.create_quote(uuid.uuid4(), secrets.token_urlsafe(32), intent, execution_plan=plan())
        self.assertEqual(CybertestCheckout.objects.count(), 3)

    def test_basic_purchase_is_10_and_zero_findings_delivery_settles_once(self):
        quote, audit, intent = self.start(basic(), 10)
        evidence = self.result(audit, basic())
        first = checkout.settle_delivery(quote, audit, evidence, None)
        repeated = checkout.settle_delivery(quote, audit, evidence, None)
        self.assertEqual(first, repeated)
        self.assertEqual(first['settlement']['charge_microroo'], 10_000_000)
        self.assertEqual(checkout.balance(self.user, test_mode=True), 990)
        self.assertEqual(Ledger.objects.count(), 0)
        stored = CybertestCheckout.objects.get(pk=quote).phase_contract
        self.assertEqual(stored['commitSha'], 'c'*40)
        self.assertEqual(stored['sourceManifestSha256'], 'd'*64)
        self.assertEqual(stored['phase'], 'basic')

    def test_basic_bought_before_a_price_change_keeps_its_agreed_price(self):
        quote, audit, intent = self.start(basic(), 10)
        CybertestCheckout.objects.filter(pk=quote).update(points=24)
        receipt = checkout.status(quote)
        self.assertEqual((receipt['points'], receipt['total_points']), (24, 24))
        self.assertEqual(receipt['purchase_scope']['total_points'], 24)
        settled = checkout.settle_delivery(quote, audit, self.result(audit, basic()), None)
        self.assertEqual(settled['settlement']['charge_microroo'], 24_000_000)
        contract = {**upgrade(), 'parentCheckoutId': str(quote)}
        upgraded, _, _ = self.start(contract, 24)
        self.assertEqual(checkout.status(upgraded)['purchase_scope']['total_points'], 48)

    def test_upgrade_total_is_the_basic_paid_plus_the_upgrade(self):
        base, _, _ = self.delivered_basic()
        quote, _, _ = self.start({**upgrade('large'), 'parentCheckoutId': str(base)}, 100)
        receipt = checkout.status(quote)
        self.assertEqual((receipt['points'], receipt['total_points']), (100, 110))
        self.assertEqual(receipt['purchase_scope']['total_points'], 110)

    def test_upgrade_debits_only_difference_and_refunds_it_without_touching_basic(self):
        base, _, _ = self.delivered_basic()
        for tier, points in [('standard', 24), ('large', 100)]:
            contract = {**upgrade(tier), 'parentCheckoutId': str(base)}
            quote, audit, _ = self.start(contract, points)
            self.assertEqual(checkout.balance(self.user, test_mode=True), 990-points)
            evidence = self.result(audit, contract, missing_required=True)
            first = checkout.settle_delivery(quote, audit, evidence, None)
            self.assertEqual(checkout.settle_delivery(quote, audit, evidence, None), first)
            self.assertEqual(first['settlement']['refund_microroo'], points*1_000_000)
            self.assertEqual(checkout.balance(self.user, test_mode=True), 990)
        self.assertEqual(CybertestCheckout.objects.get(pk=base).settlement['charge_microroo'], 10_000_000)
        self.assertEqual(Ledger.objects.count(), 0)

    def test_distinct_quote_ids_cannot_buy_two_upgrades(self):
        base, _, _ = self.delivered_basic()
        contract = {**upgrade(), 'parentCheckoutId': str(base)}
        quote, audit, intent = self.start(contract, 24)
        with self.assertRaises(CheckoutError):
            checkout.create_quote(uuid.uuid4(), secrets.token_urlsafe(32), intent)
        checkout.settle_delivery(quote, audit, self.result(audit, contract), None)
        with self.assertRaises(CheckoutError):
            checkout.create_quote(uuid.uuid4(), secrets.token_urlsafe(32), intent)
        self.assertEqual(checkout.balance(self.user, test_mode=True), 966)

    def test_incomplete_base_cannot_upgrade_even_when_it_has_findings(self):
        base, audit, _ = self.start(basic(), 10)
        costs = {'auditId': str(audit), 'status': 'complete', 'modelCostUsd': 2, 'pendingCalls': 0, 'unpricedCalls': 0}
        checkout.settle_delivery(base, audit, self.result(audit, basic(), missing_required=True), costs)
        self.assertEqual(checkout.balance(self.user, test_mode=True), 998)
        with self.assertRaises(CheckoutError):
            checkout.create_quote(uuid.uuid4(), secrets.token_urlsafe(32), self.purchase({**upgrade(), 'parentCheckoutId': str(base)}))

    def test_upgrade_cannot_switch_payer_company_scope_commit_or_manifest(self):
        base, _, _ = self.delivered_basic()
        intent = self.purchase({**upgrade(), 'parentCheckoutId': str(base)})
        changes = [{'mlai_user_id': str(self.other.pk)}, {'mlai_company_id': str(uuid.uuid4())},
                   {'scope': 'all'}, {'repository_id': '457'},
                   {'purchase': {**intent['purchase'], 'commitSha': 'e'*40}},
                   {'purchase': {**intent['purchase'], 'sourceManifestSha256': 'e'*64}}]
        for change in changes:
            with self.subTest(change=change), self.assertRaises(CheckoutError):
                checkout.create_quote(uuid.uuid4(), secrets.token_urlsafe(32), {**intent, **change})

    def test_optional_failure_is_partial_but_does_not_refund_delivered_purchase(self):
        quote, audit, _ = self.start(basic(), 10)
        evidence = self.result(audit, basic()); evidence['checks'][-1]['status'] = 'error'
        result = checkout.settle_delivery(quote, audit, evidence, None)
        self.assertEqual(result['settlement']['delivery']['status'], 'partial')
        self.assertEqual(result['settlement']['refund_microroo'], 0)

    def test_expired_uncharged_upgrade_quote_can_be_replaced(self):
        base, _, _ = self.delivered_basic()
        intent = self.purchase({**upgrade(), 'parentCheckoutId': str(base)})
        old = uuid.uuid4()
        checkout.create_quote(old, secrets.token_urlsafe(32), intent)
        CybertestCheckout.objects.filter(pk=old).update(expires_at=timezone.now()-timedelta(seconds=1))
        checkout.create_quote(uuid.uuid4(), secrets.token_urlsafe(32), intent)
        self.assertEqual(CybertestCheckout.objects.get(pk=old).status, 'cancelled')

    def test_replaying_closed_quote_returns_it_even_after_a_retry_was_purchased(self):
        base, _, _ = self.delivered_basic()
        intent = self.purchase({**upgrade(), 'parentCheckoutId': str(base)})
        old, token, audit = uuid.uuid4(), secrets.token_urlsafe(32), uuid.uuid4()
        checkout.create_quote(old, token, intent)
        checkout.confirm(old, token, self.user, expected_points=24)
        checkout.charge(old, audit, intent)
        checkout.start_dispatch(old, audit)
        checkout.settle_delivery(old, audit, self.result(audit, intent['purchase'], missing_required=True), None)
        checkout.create_quote(uuid.uuid4(), secrets.token_urlsafe(32), intent)
        self.assertEqual(checkout.create_quote(old, token, intent)['status'], 'refunded')
        self.assertEqual(checkout.balance(self.user, test_mode=True), 990)

    def test_feature_off_blocks_new_phased_quotes_but_not_refunds(self):
        base, _, _ = self.delivered_basic()
        contract = {**upgrade(), 'parentCheckoutId': str(base)}
        quote, audit, intent = self.start(contract, 24)
        with override_settings(CYBERTEST_PHASED_SCANS_ENABLED=False, CYBERTEST_BILLING_MODE='disabled'):
            with self.assertRaises(CheckoutError):
                checkout.create_quote(uuid.uuid4(), secrets.token_urlsafe(32), self.purchase(basic()))
            result = checkout.settle_delivery(quote, audit, self.result(audit, contract, missing_required=True), None)
        self.assertEqual(result['settlement']['refund_microroo'], 24_000_000)

    def test_changed_delivery_and_legacy_settlement_cannot_rewrite_a_phase_receipt(self):
        base, audit, _ = self.delivered_basic()
        changed = self.result(audit, basic()); changed['report']['sha256'] = 'f'*64
        with self.assertRaises(CheckoutError): checkout.settle_delivery(base, audit, changed, None)
        costs = {'auditId': str(audit), 'status': 'complete', 'modelCostUsd': 0, 'pendingCalls': 0, 'unpricedCalls': 0}
        with self.assertRaises(CheckoutError): checkout.settle_failed_review(base, audit, 'EVIDENCE_REVIEW_FAILED', costs)


@override_settings(CYBERTEST_BILLING_MODE='live', CYBERTEST_LIVE_BILLING_ENABLED=True,
                   CYBERTEST_ENTRY_STAGING_ENABLED=False, CYBERTEST_PHASED_SCANS_ENABLED=True,
                   CYBERTEST_USD_PER_AUD='0.5', CYBERTEST_AUD_PER_POINT='2')
class PhaseSyntheticLedgerTests(TestCase):
    """Real ledger code, fabricated local users; no external payment calls."""
    purchase = PhaseCheckoutTests.purchase
    start = PhaseCheckoutTests.start
    result = PhaseCheckoutTests.result
    delivered_basic = PhaseCheckoutTests.delivered_basic

    def setUp(self):
        PhaseCheckoutTests.setUp(self)
        self.account = PointsAccount.objects.create(user=self.user, microroo_initialized=True,
            digital_balance_microroo=30_000_000, balance_microroo=970_000_000,
            purchased_topup_balance_microroo=150_000_000, earned_balance_microroo=820_000_000)

    def test_upgrade_refund_restores_only_upgrade_buckets_and_cannot_repeat(self):
        base, _, _ = self.delivered_basic()
        contract = {**upgrade('large'), 'parentCheckoutId': str(base)}
        quote, audit, _ = self.start(contract, 100)
        evidence = self.result(audit, contract, missing_required=True)
        checkout.settle_delivery(quote, audit, evidence, None)
        checkout.settle_delivery(quote, audit, evidence, None)
        self.account.refresh_from_db()
        self.assertEqual(self.account.digital_balance_microroo, 20_000_000)
        self.assertEqual(self.account.purchased_topup_balance_microroo, 150_000_000)
        self.assertEqual(self.account.earned_balance_microroo, 820_000_000)
        self.assertEqual(checkout.balance(self.user, test_mode=False), 990)
        refund = Ledger.objects.get(user=self.user, kind='REFUND')
        self.assertEqual(refund.refund_of_id, CybertestCheckout.objects.get(pk=quote).spend_ledger_id)
        self.assertIsNone(CybertestCheckout.objects.get(pk=base).refund_ledger_id)
        self.assertEqual(Ledger.objects.filter(user=self.user, kind='SPEND').count(), 2)

    def test_basic_retained_cost_and_refund_roll_back_together_then_settle_once(self):
        quote, audit, _ = self.start(basic(), 10)
        evidence = self.result(audit, basic(), missing_required=True)
        costs = {'auditId': str(audit), 'status': 'complete', 'modelCostUsd': 2.5, 'pendingCalls': 0, 'unpricedCalls': 0}
        with patch.object(checkout.PointsService, 'spend_microroo', side_effect=RuntimeError('synthetic ledger failure')):
            with self.assertRaisesRegex(RuntimeError, 'synthetic ledger failure'):
                checkout.settle_delivery(quote, audit, evidence, costs)
        self.assertEqual(checkout.balance(self.user, test_mode=False), 990)
        self.assertIsNone(CybertestCheckout.objects.get(pk=quote).settlement)
        self.assertEqual(Ledger.objects.filter(user=self.user).count(), 1)
        first = checkout.settle_delivery(quote, audit, evidence, costs)
        self.assertEqual(checkout.settle_delivery(quote, audit, evidence, costs), first)
        self.account.refresh_from_db()
        self.assertEqual(self.account.digital_balance_microroo, 27_500_000)
        self.assertEqual(self.account.purchased_topup_balance_microroo, 150_000_000)
        self.assertEqual(self.account.earned_balance_microroo, 820_000_000)
        self.assertEqual(Ledger.objects.filter(user=self.user, kind='REFUND').count(), 1)
        self.assertEqual(Ledger.objects.filter(user=self.user, kind='SPEND').count(), 2)



FREE_REPOSITORY = '1319031949'


@override_settings(CYBERTEST_BILLING_MODE='live', CYBERTEST_LIVE_BILLING_ENABLED=True,
                   CYBERTEST_ENTRY_STAGING_ENABLED=False, CYBERTEST_PHASED_SCANS_ENABLED=True,
                   CYBERTEST_USD_PER_AUD='0.5', CYBERTEST_AUD_PER_POINT='2',
                   CYBERTEST_FREE_REPOSITORY_IDS=['747450323', FREE_REPOSITORY])
class FreeRepositoryCheckTests(TestCase):
    """MLAI's own repositories keep their price on the receipt but never touch the ledger."""
    purchase = PhaseCheckoutTests.purchase
    start = PhaseCheckoutTests.start
    result = PhaseCheckoutTests.result

    def setUp(self):
        PhaseCheckoutTests.setUp(self)
        self.intent['repository_id'] = FREE_REPOSITORY
        # An empty account: a paid check would fail with INSUFFICIENT_ROO_POINTS.
        self.account = PointsAccount.objects.create(user=self.user, microroo_initialized=True)

    @override_settings(CYBERTEST_COMPLETION_NOTICES_ENABLED=True,
                       COMMUNITY_CHAT_FRONTEND_URL='https://chat.mlai.au',
                       ARTICLES_GOAL_NOTIFIER_PRIVATE_KEY='2'*64)
    def test_continued_report_queues_one_private_replayable_notice_and_keeps_receipt(self):
        from community_chat.models import CommunityChatDevice, DeviceBindingStatus
        from content_factory.models import ContentFactoryCallbackEvent
        from founder_tools.models import VibeRaisingCompany, VibeRaisingProfile
        from roo import cybertest_notifications as notices
        from roo.cybertest_models import CybertestCompanyBinding
        company = VibeRaisingCompany.objects.create(
            profile=VibeRaisingProfile.objects.create(user=self.user), name='Test team')
        self.intent['mlai_company_id'] = str(company.pk)
        CybertestCompanyBinding.objects.create(company=company, created_by=self.user,
            workspace_id=self.intent['workspace_id'], is_active=True, verified_at=timezone.now())
        CommunityChatDevice.objects.create(user=self.user, public_key='1'*64,
            status=DeviceBindingStatus.VERIFIED, verified_at=timezone.now())
        quote, audit, _ = self.start(basic(), 10)
        checkout.settle_delivery(quote, audit, self.result(audit, basic(), missing_required=True), None)
        queued = ContentFactoryCallbackEvent.objects.filter(event_type=notices.EVENT_TYPE)
        self.assertFalse(queued.exists())
        previous = checkout.status(quote)['settlement']
        full = self.result(audit, basic())
        for _ in range(2):
            self.assertEqual(notices.completed(quote, audit, full), {'status': 'queued'})
        self.assertEqual(queued.count(), 1)
        with (patch.object(notices.transport, 'open_dm', return_value=str(uuid.uuid4())),
              patch.object(notices.transport, 'post_event',
                  side_effect=[notices.transport.NotificationTransportError('reply_lost'), {}]) as post):
            self.assertEqual(notices.drain()['retry'], 1)
            self.assertIsNone(queued.get().processed_at)
            queued.update(claimed_at=timezone.now()-timedelta(hours=1))
            self.assertEqual(notices.drain()['sent'], 1)
            self.assertEqual(post.call_args_list[0].args[0]['id'], post.call_args_list[1].args[0]['id'])
            self.assertIn(f'/my-startup/security/checks/{audit}?company_id={company.pk}', post.call_args.args[0]['content'])
            self.assertEqual(notices.drain()['sent'], 0)
        self.assertIsNotNone(queued.get().processed_at)
        self.assertEqual(checkout.status(quote)['settlement'], previous)
        self.assertFalse(Ledger.objects.exists())

    def test_free_basic_and_upgrade_cost_nothing_and_settle_without_ledger_rows(self):
        quote, audit, _ = self.start(basic(), 10)
        receipt = checkout.status(quote)
        self.assertTrue(receipt['sponsored'])
        self.assertEqual((receipt['points'], receipt['due_points']), (10, 0))
        checkout.settle_delivery(quote, audit, self.result(audit, basic()), None)
        contract = {**upgrade('large'), 'parentCheckoutId': str(quote)}
        upgrade_quote, upgrade_audit, _ = self.start(contract, 100)
        checkout.settle_delivery(upgrade_quote, upgrade_audit,
                                 self.result(upgrade_audit, contract, missing_required=True), None)
        self.assertFalse(Ledger.objects.filter(user=self.user).exists())
        self.assertIsNone(CybertestCheckout.objects.get(pk=quote).spend_ledger_id)
        self.assertEqual(checkout.balance(self.user, test_mode=False), 0)

    def test_free_start_failure_refund_writes_no_ledger_row(self):
        quote, audit, token = uuid.uuid4(), uuid.uuid4(), secrets.token_urlsafe(32)
        intent = self.purchase(basic())
        checkout.create_quote(quote, token, intent)
        checkout.confirm(quote, token, self.user, expected_points=10)
        checkout.charge(quote, audit, intent)
        self.assertEqual(checkout.refund_start_failure(quote, audit, 'PREPARATION_FAILED')['status'], 'refunded')
        self.assertFalse(Ledger.objects.filter(user=self.user).exists())

    def test_every_other_repository_is_paid(self):
        self.intent['repository_id'] = '999'
        quote, token = uuid.uuid4(), secrets.token_urlsafe(32)
        self.assertFalse(checkout.create_quote(quote, token, self.purchase(basic()))['sponsored'])
        with self.assertRaises(CheckoutError) as raised:
            checkout.confirm(quote, token, self.user, expected_points=10)
        self.assertEqual(raised.exception.code, 'INSUFFICIENT_ROO_POINTS')

    def test_the_default_list_is_the_five_mlai_repositories(self):
        import mlai.settings as configured
        self.assertEqual(set(configured.CYBERTEST_FREE_REPOSITORY_IDS),
                         {'747450323', '918452761', '1319031949', '1112644407', '1190194664'})
        with override_settings(CYBERTEST_FREE_REPOSITORY_IDS=None):
            self.assertEqual(checkout.free_repository_ids(), set())
