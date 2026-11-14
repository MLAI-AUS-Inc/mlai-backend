"""Pure billing policy tests: no Django setup, database or provider calls."""
import unittest
import json
from pathlib import Path
try:
    from roo.cybertest_delivery import phase_price, assess_delivery, delivery_settlement
except ModuleNotFoundError:
    phase_price = assess_delivery = delivery_settlement = None

AUDIT = '00000000-0000-4000-8000-000000000001'
PARENT = '00000000-0000-4000-8000-000000000002'
PRICING = {'usd_per_aud': '0.5', 'aud_per_point': '2'}

def basic():
    return {'version': 1, 'phase': 'basic', 'tier': 'basic', 'planId': 'a'*64, 'checks': [
        {'id': 'basic:source:1', 'kind': 'source_batch', 'required': True},
        {'id': 'basic:credentials', 'kind': 'credential_patterns', 'required': True},
        {'id': 'basic:review', 'kind': 'evidence_review', 'required': False},
    ]}

def evidence():
    return {'auditId': AUDIT, 'planId': 'a'*64, 'checks': [
        {'id': 'basic:source:1', 'status': 'completed'},
        {'id': 'basic:credentials', 'status': 'completed'},
        {'id': 'basic:review', 'status': 'completed'},
    ], 'report': {'persisted': True, 'sha256': 'b'*64}}

def usage(cost=2):
    return {'auditId': AUDIT, 'status': 'complete', 'pendingCalls': 0,
            'unpricedCalls': 0, 'storageFailed': False, 'modelCostUsd': cost}

def upgrade(tier='standard'):
    return {'version': 1, 'phase': 'upgrade', 'tier': tier, 'parentCheckoutId': PARENT,
            'planId': 'a'*64, 'checks': [
                {'id': 'upgrade:target:referrals', 'kind': 'runtime_target', 'required': True},
                {'id': 'upgrade:review', 'kind': 'evidence_review', 'required': False}]}

class DeliveryPolicyTests(unittest.TestCase):
    def test_basic_cannot_omit_source_or_credentials_or_make_them_optional(self):
        original = basic()['checks']
        variants = [[c for c in original if c['kind'] != kind] for kind in ('source_batch', 'credential_patterns')]
        variants += [[{**c, 'required': False} if c['kind'] == kind else c for c in original] for kind in ('source_batch', 'credential_patterns')]
        for checks in variants:
            with self.subTest(checks=checks), self.assertRaises(ValueError):
                assess_delivery({**basic(), 'checks': checks}, {**evidence(), 'checks': [{'id': c['id'], 'status': 'completed'} for c in checks]}, AUDIT)

    def test_shared_wire_fixture_preserves_delivered_partial_contract(self):
        fixture = json.loads((Path(__file__).parent/'fixtures'/'phase-contract-v1.json').read_text())
        result = assess_delivery(fixture['contract'], fixture['evidence'], fixture['evidence']['auditId'])
        digest = result.pop('evidenceSha256')
        self.assertEqual(result, fixture['expected'])
        self.assertRegex(digest, r'^[a-f0-9]{64}$')

    def test_phase_prices_are_incremental(self):
        self.assertTrue(callable(phase_price), 'phase policy is not implemented')
        self.assertEqual(phase_price(basic()), {'totalPoints': 24, 'duePoints': 24})
        self.assertEqual(phase_price(upgrade()), {'totalPoints': 48, 'duePoints': 24})
        self.assertEqual(phase_price(upgrade('large')), {'totalPoints': 124, 'duePoints': 100})
        for change in [{'parentCheckoutId': None}, {'tier': 'enterprise'}, {'version': True}]:
            with self.subTest(change=change), self.assertRaises(ValueError):
                phase_price({**upgrade(), **change})

    def test_zero_findings_and_optional_errors_do_not_force_model_only_settlement(self):
        value = evidence()
        value['checks'][2]['status'] = 'error'
        result = delivery_settlement(basic(), value, AUDIT, PRICING, None)
        self.assertEqual(result['delivery']['status'], 'partial')
        self.assertTrue(result['delivery']['minimumMet'])
        self.assertEqual(result['charge_microroo'], 24_000_000)
        self.assertEqual(result['refund_microroo'], 0)

    def test_missing_required_basic_work_retains_only_actual_model_cost_capped_at_24(self):
        value = evidence(); value['checks'][0]['status'] = 'incomplete'
        result = delivery_settlement(basic(), value, AUDIT, PRICING, usage(2))
        self.assertEqual(result['charge_microroo'], 2_000_000)
        self.assertEqual(result['refund_microroo'], 22_000_000)
        capped = delivery_settlement(basic(), value, AUDIT, PRICING, usage(99))
        self.assertEqual(capped['charge_microroo'], 24_000_000)

    def test_failed_upgrade_refunds_whole_difference_even_if_costs_are_unknown(self):
        value = {**evidence(), 'checks': [{'id': 'upgrade:target:referrals', 'status': 'error'}]}
        for tier, refund in [('standard', 24_000_000), ('large', 100_000_000)]:
            result = delivery_settlement(upgrade(tier), value, AUDIT, {}, None)
            self.assertEqual(result['charge_microroo'], 0)
            self.assertEqual(result['refund_microroo'], refund)
            self.assertEqual(result['basis'], 'upgrade_not_delivered')

    def test_unknown_or_other_audit_usage_cannot_settle_a_failed_basic(self):
        value = evidence(); value['report'] = {'persisted': False}
        for costs in [None, {**usage(), 'pendingCalls': 1}, {**usage(), 'auditId': PARENT}]:
            with self.subTest(costs=costs), self.assertRaises(ValueError):
                delivery_settlement(basic(), value, AUDIT, PRICING, costs)

    def test_settlement_replay_is_idempotent_and_changed_evidence_is_rejected(self):
        result = delivery_settlement(basic(), evidence(), AUDIT, PRICING, None)
        existing = {**result, 'retained_ledger_id': 'ledger-reference'}
        self.assertEqual(delivery_settlement(basic(), evidence(), AUDIT, PRICING, None, existing=existing), existing)
        value = evidence(); value['report']['sha256'] = 'c'*64
        with self.assertRaises(ValueError):
            delivery_settlement(basic(), value, AUDIT, PRICING, None, existing=existing)

    def test_foreign_duplicate_and_stale_check_evidence_is_rejected(self):
        for change in [{'auditId': PARENT}, {'planId': 'c'*64},
                       {'checks': evidence()['checks']*2},
                       {'checks': [{'id': 'unknown', 'status': 'completed'}]},
                       {'report': {'persisted': True, 'sha256': ''}}]:
            with self.subTest(change=change), self.assertRaises(ValueError):
                assess_delivery(basic(), {**evidence(), **change}, AUDIT)

    def test_empty_or_optional_only_plans_cannot_deliver(self):
        for checks in [[], [{'id': 'basic:report', 'kind': 'report', 'required': True}],
                       [{'id': 'basic:review', 'kind': 'evidence_review', 'required': False}],
                       basic()['checks']*2]:
            with self.subTest(checks=checks), self.assertRaises(ValueError):
                assess_delivery({**basic(), 'checks': checks}, evidence(), AUDIT)

    def test_canonical_evidence_ignores_input_order_not_changed_coverage(self):
        plan, value = basic(), evidence()
        expected = assess_delivery(plan, value, AUDIT)['evidenceSha256']
        plan['checks'].reverse(); value['checks'].reverse()
        self.assertEqual(assess_delivery(plan, value, AUDIT)['evidenceSha256'], expected)
        value['checks'][0]['status'] = 'error'
        self.assertNotEqual(assess_delivery(plan, value, AUDIT)['evidenceSha256'], expected)

    def test_purchased_plan_is_bound_to_consent_and_rejects_changed_commit(self):
        from roo.cybertest_policy import binding, binding_digest
        purchase = {**basic(), 'commitSha': 'c'*40, 'sourceManifestSha256': 'd'*64}
        intent = {'github_actor_id': '123', 'repository_id': '456', 'workspace_id': PARENT,
                  'repository': 'fixture/project', 'scope': 'all', 'mlai_user_id': '9',
                  'mlai_company_id': AUDIT, 'purchase': purchase}
        self.assertIn('purchase', binding(intent))
        self.assertNotEqual(binding_digest(intent), binding_digest({**intent, 'purchase': {**purchase, 'commitSha': 'e'*40}}))
        self.assertNotEqual(binding_digest(intent), binding_digest({**intent, 'purchase': {**purchase, 'planId': 'f'*64}}))
        for change in [{'commitSha': 'main'}, {'sourceManifestSha256': None}, {'checks': []}]:
            with self.subTest(change=change), self.assertRaises(ValueError):
                binding({**intent, 'purchase': {**purchase, **change}})

if __name__ == '__main__':
    unittest.main()
