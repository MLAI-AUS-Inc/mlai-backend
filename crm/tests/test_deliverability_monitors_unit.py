"""Primary provider contracts and protection boundaries, with no database or network."""

from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase, override_settings

from crm.deliverability import policy, scopes
from crm.deliverability.guardian import Metrics, evaluate
from crm.deliverability.health import rates
from crm.deliverability.monitors import dns, postmaster, reputation, resend
from crm.deliverability.monitors.http import Unavailable, budget, json_request
from crm.deliverability.monitors.runner import run_deliverability_monitors
from crm.deliverability.preflight.seed_inbox import grade


class WindowPolicyTests(SimpleTestCase):
    def test_unsubscribe_warning_and_stop_use_handoffs_and_inclusive_thresholds(self):
        self.assertEqual(scopes.extra_signals(Metrics(attempted=199, unsubscribed=10), blast=True), [])
        signal, = scopes.extra_signals(Metrics(attempted=200, unsubscribed=2), blast=True)
        self.assertEqual((signal.severity, signal.action), ('amber', 'slow_blast'))
        signal, = scopes.extra_signals(Metrics(attempted=200, unsubscribed=5), blast=True)
        self.assertEqual((signal.severity, signal.action), ('red', 'pause_blast'))

    def test_provider_warning_does_not_slow_the_whole_stream(self):
        self.assertEqual(scopes.extra_signals(Metrics(attempted=49, deferred=30), provider=True), [])
        signal, = scopes.extra_signals(Metrics(attempted=50, deferred=5), provider=True)
        self.assertEqual(signal.action, 'throttle_provider')
        signal, = scopes.extra_signals(Metrics(attempted=50, deferred=10), provider=True)
        self.assertEqual((signal.action, signal.severity), ('hold_provider', 'amber'))

    def test_five_rate_limit_codes_hold_even_with_a_small_attempt_sample(self):
        signal, = scopes.extra_signals(Metrics(attempted=5, rate_limited=5), provider=True)
        self.assertEqual(signal.action, 'hold_provider')

    def test_only_the_matching_provider_codes_count(self):
        def event(provider, code='', diagnostic=''):
            return SimpleNamespace(recipient_provider=provider, enhanced_code=code, diagnostic=diagnostic)
        for row in (event('google', '4.7.28'), event('microsoft', '4.7.650'), event('yahoo', diagnostic='TSS04')):
            self.assertTrue(scopes.rate_limit(row))
        self.assertFalse(scopes.rate_limit(event('google', '4.7.650')))

    def test_owner_overrides_affect_thresholds_but_cannot_enable_features(self):
        stream = SimpleNamespace(policy_overrides={'CRM_GUARD_HARD_BOUNCE_RED': 0.1, 'CRM_GUARDIAN_ENABLED': True})
        self.assertEqual(evaluate(Metrics(attempted=100, hard_bounces=6), stream=stream)[0].severity, 'amber')
        with override_settings(CRM_GUARDIAN_ENABLED=False):
            self.assertFalse(policy.value('CRM_GUARDIAN_ENABLED', stream))

    def test_negative_evidence_identity_is_order_independent_but_detects_new_failures(self):
        self.assertEqual(scopes.evidence_key(['b', 'a']), scopes.evidence_key(['a', 'b']))
        self.assertNotEqual(scopes.evidence_key(['a']), scopes.evidence_key(['b']))

    def test_no_denominator_is_unknown_rather_than_zero_percent(self):
        self.assertEqual(rates({}), {'delivered': None, 'hard_bounce': None, 'complaint': None, 'unsubscribe': None})


class PostmasterContractTests(SimpleTestCase):
    def test_auth_tls_and_feedback_metrics_have_documented_required_filters(self):
        rows = {row['name']: row for row in postmaster.definitions()}
        for kind in ('spf', 'dkim', 'dmarc'):
            self.assertEqual(rows[kind + '_rate']['filter'], f'auth_type = "{kind}"')
        self.assertEqual(rows['tls_rate']['filter'], 'traffic_direction = "outbound"')
        self.assertEqual(rows['feedback_ids']['baseMetric']['standardMetric'], 'FEEDBACK_LOOP_ID')
        self.assertTrue(postmaster.SCOPE.endswith('/postmaster.traffic.readonly'))

    def test_daily_values_use_metric_field_and_never_parse_the_resource_name(self):
        day = date(2026, 11, 3)
        rows = [{'name': 'opaque-spam-rate-is-not-a-contract', 'metric': 'spam_rate',
            'date': {'year': 2026, 'month': 11, 'day': 3}, 'value': {'doubleValue': 0.0018}}]
        self.assertEqual(postmaster.parse_stats(rows, {'spam_rate'}, [day]), {'2026-11-03': {'spam_rate': 0.0018}})
        rows[0]['metric'] = 'unknown'
        self.assertEqual(postmaster.parse_stats(rows, {'spam_rate'}, [day]), {'2026-11-03': {}})

    def test_no_data_invalid_dates_and_invalid_rates_are_not_clean_zeroes(self):
        day = date(2026, 11, 3)
        for value in (float('nan'), float('inf'), True, -0.1, 1.1, None):
            row = {'metric': 'spam_rate', 'date': {'year': 2026, 'month': 11, 'day': 3}, 'value': {'doubleValue': value}}
            self.assertEqual(postmaster.parse_stats([row], {'spam_rate'}, [day])['2026-11-03'], {})
        self.assertEqual(postmaster.parse_stats([], {'spam_rate'}, [day])['2026-11-03'], {})

    def test_root_compliance_does_not_imply_subdomain_compliance(self):
        data = {'complianceData': {'domainId': 'mlai.au', 'rowData': [
            {'requirement': 'DKIM', 'status': {'status': 'COMPLIANT'}}]}}
        self.assertEqual(postmaster.compliance(data, 'news.mlai.au')['status'], 'no_data')
        data['subdomainComplianceData'] = {'domainId': 'news.mlai.au', 'rowData': [
            {'requirement': 'DKIM', 'status': {'status': 'NEEDS_WORK'}}]}
        self.assertEqual(postmaster.compliance(data, 'news.mlai.au')['status'], 'needs_work')

    def test_pagination_is_bounded_and_requires_a_new_cursor(self):
        day = date(2026, 11, 3)
        with patch.object(postmaster, 'json_request', return_value={'nextPageToken': 'same'}):
            with self.assertRaises(Unavailable):
                postmaster.query('news.mlai.au', 'synthetic-token', [day], postmaster.definitions())


class ReputationContractTests(SimpleTestCase):
    def test_refused_and_malformed_dns_are_never_positive_or_clean(self):
        for source, answers in (('spamhaus_dbl', ['127.255.255.254']), ('spamhaus_dbl', ['127.0.1.255']),
                ('surbl', ['127.0.0.1']), ('uribl', ['127.0.0.1']), ('surbl', ['8.8.8.8']), ('uribl', None)):
            self.assertIsNone(reputation.decode(source, answers)[0])
        self.assertIsNone(reputation.decode('spamhaus_dbl', ['127.0.1.2', '127.255.255.254'])[0])

    def test_only_documented_codes_and_nxdomain_count(self):
        for source, address in (('spamhaus_dbl', '127.0.1.2'), ('spamhaus_dbl', '127.0.1.102'),
                ('surbl', '127.0.0.126'), ('uribl', '127.0.0.14')):
            self.assertTrue(reputation.decode(source, [address])[0])
            self.assertFalse(reputation.decode(source, [])[0])

    @override_settings(CRM_URIBL_ENABLED=True, CRM_REPUTATION_NAMESERVERS=())
    def test_uribl_requires_an_explicit_resolver(self):
        self.assertIsNone(reputation.zone('uribl'))

    @override_settings(SPAMHAUS_DQS_KEY='a' * 26)
    def test_daily_positive_and_negative_tests_detect_blanket_nxdomain_filtering(self):
        from django.core.cache import cache
        cache.clear()
        with patch.object(reputation, 'lookup', return_value=[]):
            self.assertEqual(reputation.test_path('spamhaus_dbl', now=datetime(2026, 11, 5, tzinfo=timezone.utc)),
                (False, 'test_path_unavailable'))

    def test_full_url_paths_have_separate_receipts(self):
        self.assertNotEqual(reputation.urls_digest(['https://mlai.au/a']), reputation.urls_digest(['https://mlai.au/b']))
        self.assertEqual(reputation.urls_digest(['https://mlai.au/a', 'https://mlai.au/b']),
            reputation.urls_digest(['https://mlai.au/b', 'https://mlai.au/a']))

    def test_spf_advisory_counts_nested_includes_and_detects_cycles(self):
        rows = {'mlai.au': ['v=spf1 include:a.example -all'], 'a.example': ['v=spf1 a mx -all']}
        self.assertEqual(dns.spf_lookups('mlai.au', lookup=lambda domain, _: rows[domain]), {'status': 'pass', 'lookups': 3})
        rows['a.example'] = ['v=spf1 include:mlai.au -all']
        self.assertEqual(dns.spf_lookups('mlai.au', lookup=lambda domain, _: rows[domain])['status'], 'warn')


class ProviderConfigTests(SimpleTestCase):
    def test_resend_requires_verified_domain_tracking_off_and_all_feedback_events(self):
        domain = {'name': 'news.mlai.au', 'status': 'verified', 'open_tracking': False, 'click_tracking': False}
        hook = {'endpoint': 'https://lists.mlai.au/webhooks/resend', 'status': 'enabled', 'events': list(resend.EVENTS)}
        self.assertEqual(resend.contract(domain, [hook], expected_domain='news.mlai.au')['status'], 'pass')
        self.assertIn('resend_tracking', resend.contract({**domain, 'click_tracking': True}, [hook], expected_domain='news.mlai.au')['critical'])
        self.assertIn('resend_webhook', resend.contract(domain, [{**hook, 'events': ['email.sent']}], expected_domain='news.mlai.au')['critical'])

    @override_settings(RESEND_READ_API_KEY='', RESEND_API_KEY='sending-key-must-not-be-used')
    def test_read_client_does_not_fall_back_to_a_sending_key(self):
        with patch.object(resend, 'json_request') as request:
            with self.assertRaises(Unavailable):
                resend.get('/domains')
        request.assert_not_called()

    def test_notify_seeds_require_authentication_but_not_marketing_unsubscribe_headers(self):
        rows = [{'provider': provider, 'folder': 'inbox', 'arrived_at': '2026-11-05T01:00:00Z',
            'spf': 'pass', 'dkim': 'pass', 'dmarc': 'pass'} for provider in ('gmail', 'outlook', 'yahoo')]
        self.assertEqual(grade(rows, marketing=False)[0], 'pass')
        self.assertEqual(grade(rows)[0], 'block')

    @override_settings(CRM_MONITORS_ENABLED=False)
    def test_monitor_flag_off_never_reads_a_database_or_provider(self):
        self.assertEqual(run_deliverability_monitors(), {'status': 'disabled'})

    def test_provider_redirects_and_oversized_or_invalid_success_are_unknown(self):
        for response in (SimpleNamespace(status_code=302, content=b'', json=lambda: {}),
                SimpleNamespace(status_code=200, content=b'[]', json=lambda: [])):
            with patch('integrations.http_client.request', return_value=response) as request:
                with self.assertRaises(Unavailable):
                    json_request('GET', 'https://api.resend.com/domains')
                self.assertFalse(request.call_args.kwargs['allow_redirects'])
