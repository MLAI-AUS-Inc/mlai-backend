import unittest
from roo.cybertest_policy import CheckoutError, binding_digest, price_points, token_digest


class CheckoutBindingTests(unittest.TestCase):
    def setUp(self):
        self.quote = {'github_actor_id': '123', 'repository_id': '456',
            'workspace_id': 'ad284cd3-7bc3-48ec-8f17-d246d26c6a2a',
            'repository': 'customer/project', 'scope': ['get-client-referrals', 'other']}

    def test_reordering_scope_does_not_change_consent(self):
        self.assertEqual(binding_digest(self.quote), binding_digest({**self.quote, 'scope': list(reversed(self.quote['scope']))}))

    def test_all_functions_is_distinct_from_a_function_named_all(self):
        self.assertNotEqual(binding_digest({**self.quote,'scope':'all'}),binding_digest({**self.quote,'scope':['all']}))
        for value in [[],None,'scope']:
            with self.assertRaises(CheckoutError):binding_digest(value)

    def test_consent_is_bound_to_actor_company_repository_and_scope(self):
        for key, value in [('github_actor_id', '124'), ('repository_id', '457'),
                ('workspace_id', 'ad284cd3-7bc3-48ec-8f17-d246d26c6a2b'),
                ('repository', 'customer/other'), ('scope', ['other'])]:
            with self.subTest(key=key):
                self.assertNotEqual(binding_digest(self.quote), binding_digest({**self.quote, key:value}))

    def test_untrusted_identity_or_unbounded_source_is_rejected(self):
        for key, value in [('github_actor_id', 'some@email.test'), ('github_actor_id', True),
                ('repository_id', '9999999999999999999'), ('workspace_id', 'company name'),
                ('repository', 'https://github.com/user/project'), ('scope', []),
                ('scope', ['../../credentials']), ('scope', ['a']*101)]:
            with self.subTest(key=key,value=value), self.assertRaises(CheckoutError):
                binding_digest({**self.quote,key:value})

    def test_quote_amount_is_an_exact_positive_integer(self):
        self.assertEqual(price_points(10),10)
        for value in [0,-1,1.5,True,'10',None,100_001]:
            with self.subTest(value=value),self.assertRaises(CheckoutError): price_points(value)

    def test_handoff_requires_random_token_shape_and_is_stored_as_digest(self):
        token='a'*43
        self.assertNotIn(token,token_digest(token))
        self.assertEqual(len(token_digest(token)),64)
        with self.assertRaises(CheckoutError): token_digest('short')


if __name__ == '__main__': unittest.main()

class FailedSettlementPolicyTests(unittest.TestCase):
    def test_cost_only_fractional_refund_and_cap(self):
        from roo.cybertest_policy import failure_settlement
        usage={'status':'complete','pendingCalls':0,'unpricedCalls':0,'modelCostUsd':2,'auditId':'audit'}
        result=failure_settlement(10,{'usd_per_aud':'0.5','aud_per_point':'2'},usage)
        self.assertEqual(result['charge_microroo'],2_000_000)
        self.assertEqual(result['refund_microroo'],8_000_000)
        self.assertEqual(failure_settlement(10,{'usd_per_aud':'0.5','aud_per_point':'2'},{**usage,'modelCostUsd':20})['charge_microroo'],10_000_000)

    def test_uncertain_usage_and_legacy_quotes_cannot_settle(self):
        from roo.cybertest_policy import failure_settlement
        usage={'status':'complete','modelCostUsd':2,'pendingCalls':0,'unpricedCalls':0,'auditId':'audit'}
        for change in [{'status':'incomplete'},{'pendingCalls':1},{'unpricedCalls':1},{'modelCostUsd':None},{'modelCostUsd':True},{'modelCostUsd':float('nan')},{'storageFailed':True}]:
            with self.subTest(change=change),self.assertRaises(CheckoutError):failure_settlement(10,{'usd_per_aud':'0.5','aud_per_point':'2'},{**usage,**change})
        with self.assertRaises(CheckoutError):failure_settlement(10,{},usage)

    def test_same_settlement_is_idempotent_but_changed_usage_conflicts(self):
        from roo.cybertest_policy import failure_settlement
        usage={'status':'complete','modelCostUsd':2,'pendingCalls':0,'unpricedCalls':0,'auditId':'audit'}
        pricing={'usd_per_aud':'0.5','aud_per_point':'2'}
        existing=failure_settlement(10,pricing,usage)
        self.assertEqual(failure_settlement(10,pricing,usage,existing=existing),existing)
        with self.assertRaises(CheckoutError):failure_settlement(10,pricing,{**usage,'modelCostUsd':3},existing=existing)
