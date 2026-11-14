import base64
import hashlib
import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Lock

from roo.cybertest_entry import EntryAuthority, EntryError


class Cache:
    def __init__(self): self.values, self.lock = {}, Lock()
    def get(self, key): return self.values.get(key)
    def set(self, key, value, timeout): self.values[key] = value
    def add(self, key, value, timeout):
        with self.lock:
            if key in self.values: return False
            self.values[key] = value
            return True


class EntryTests(unittest.TestCase):
    def setUp(self):
        self.now = 1000
        self.revoked = False
        self.company = '10000000-0000-4000-8000-000000000001'
        self.identity = dict(user_id='mlai-user-1', company_id=self.company,
                             workspace_id='20000000-0000-4000-8000-000000000001', company_name='Test company')
        def authorize(session, company):
            if self.revoked or session != 'session-1' or company != self.company:
                raise EntryError('ACCESS_REVOKED', 'Sign in again.', 401)
            return dict(self.identity)
        self.authority = EntryAuthority(Cache(), authorize, clock=lambda: self.now)
        self.verifier = 'v' * 43
        self.challenge = base64.urlsafe_b64encode(hashlib.sha256(self.verifier.encode()).digest()).decode().rstrip('=')
        self.state = 's' * 43

    def issue(self):
        return self.authority.issue('session-1', 'mlai-user-1', self.company, self.state, self.challenge)

    def redeem(self, code):
        return self.authority.exchange(code, self.state, self.verifier)

    def test_exchange_preserves_separate_identity_and_is_one_use(self):
        code = self.issue()
        result = self.redeem(code)
        self.assertEqual(result['user_id'], 'mlai-user-1')
        self.assertEqual(result['company_id'], self.company)
        self.assertNotIn('session_id', result)
        self.assertEqual(self.authority.inspect(result['grant'])['workspace_id'], self.identity['workspace_id'])
        with self.assertRaises(EntryError): self.redeem(code)

    def test_provisioning_authority_comes_from_current_server_identity(self):
        self.identity['workspace_provisioning'] = True
        result = self.redeem(self.issue())
        self.assertIs(result.get('workspace_provisioning'), True)
        self.identity.pop('workspace_provisioning')
        self.assertNotIn('workspace_provisioning', self.authority.inspect(result['grant']))

    def test_wrong_state_or_verifier_does_not_consume_valid_code(self):
        code = self.issue()
        for state, verifier in [('x'*43, self.verifier), (self.state, 'x'*43)]:
            with self.assertRaises(EntryError): self.authority.exchange(code, state, verifier)
        self.redeem(code)

    def test_expiry_is_enforced_even_if_cache_has_not_evicted(self):
        code = self.issue()
        self.now += 120
        with self.assertRaises(EntryError): self.redeem(code)

    def test_grant_rechecks_revocation_and_mapping(self):
        grant = self.redeem(self.issue())['grant']
        self.revoked = True
        with self.assertRaises(EntryError): self.authority.inspect(grant)
        self.revoked = False
        self.identity['workspace_id'] = '30000000-0000-4000-8000-000000000001'
        with self.assertRaises(EntryError): self.authority.inspect(grant)

    def test_concurrent_redemption_has_one_winner(self):
        code = self.issue()
        def exchange(_):
            try: return bool(self.redeem(code))
            except EntryError: return False
        with ThreadPoolExecutor(max_workers=8) as pool:
            self.assertEqual(sum(pool.map(exchange, range(8))), 1)

    def test_invalid_input_and_cross_account_are_rejected(self):
        for args in [('session-1','other',self.company,self.state,self.challenge),
                     ('session-1','mlai-user-1','bad',self.state,self.challenge),
                     ('session-1','mlai-user-1',self.company,'bad',self.challenge)]:
            with self.assertRaises(EntryError): self.authority.issue(*args)

    def test_grant_expires(self):
        grant = self.redeem(self.issue())['grant']
        self.now += 3600
        with self.assertRaises(EntryError): self.authority.inspect(grant)

    def test_quote_binding_keeps_mlai_identity_separate_from_github(self):
        from roo.cybertest_policy import binding, CheckoutError
        quote = dict(github_actor_id='42', repository_id='10', workspace_id=self.identity['workspace_id'],
                     repository='example/repo', scope='all', mlai_user_id='mlai-user-1', mlai_company_id=self.company)
        self.assertEqual(binding(quote)['mlai_user_id'], 'mlai-user-1')
        del quote['mlai_company_id']
        with self.assertRaises(CheckoutError): binding(quote)
