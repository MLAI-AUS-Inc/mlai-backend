"""Current-source fingerprint hints cannot substitute for native/live proofs."""
from contextlib import nullcontext
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase, override_settings

from . import website_site_fingerprint as fingerprint
from .website_discovery import discovery_snapshot
from .website_journey import project_journey
from .website_strategy import initial_rung
from . import website_deployment as deployment
from scripts import check_site_fingerprint_contract as provenance


def example():
    path = Path(__file__).resolve().parents[1] / 'contracts/site-fingerprint-v1.example.json'
    value = json.loads(path.read_text())
    value['captured_at'] = datetime.now(timezone.utc).isoformat()
    return value


def scope(value):
    return {key: value[key] for key in ('github_repo', 'source_sha', 'branch', 'app_root')}


def stamp(value):
    payload = {key: item for key, item in value.items() if key not in {'digest', 'captured_at'}}
    value['digest'] = hashlib.sha256(json.dumps(payload, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode()).hexdigest()
    return value


class SiteFingerprintTests(SimpleTestCase):
    def test_actual_producer_example_is_accepted_as_read_only_evidence(self):
        value = example()
        self.assertEqual(fingerprint.validate(value, **scope(value)), value)
        self.assertEqual(fingerprint.framework_hint(value), 'astro')
        self.assertEqual(fingerprint.hosting_hint(value), 'netlify')

    def test_source_repository_branch_and_app_root_are_exact(self):
        value = example()
        for change in ({'source_sha': 'b' * 40}, {'github_repo': 'other/site'},
                       {'branch': 'release'}, {'app_root': 'apps/admin'}):
            with self.subTest(change=change):
                self.assertIsNone(fingerprint.validate(value, **{**scope(value), **change}))

    def test_unknown_versions_and_changed_facts_are_rejected(self):
        value = example()
        for change in ({'schema_version': True}, {'schema_version': 2}, {'digest': '0' * 64},
                       {'capture_status': []}, {'public_status': {}}, {'captured_at': 'no-timezone'}):
            with self.subTest(change=change):
                self.assertIsNone(fingerprint.validate({**value, **change}, **scope(value)))

    def test_malformed_scope_is_rejected_even_when_the_supplied_scope_matches(self):
        for key, bad in (('github_repo', {}), ('github_repo', '../repo'), ('branch', []),
                         ('branch', 'refs/../main'), ('branch', ''), ('branch', 'main\n'),
                         ('app_root', {}), ('app_root', '../other'), ('app_root', 'a' * 501),
                         ('source_sha', 1)):
            with self.subTest(key=key, bad=bad):
                value = example()
                value[key] = bad
                self.assertIsNone(fingerprint.validate(stamp(value), **scope(value)))

    def test_malformed_nested_containers_and_credentials_fail_closed(self):
        value = example()
        for field in ({'value': None, 'confidence': [], 'evidence': []},
                      {'value': {'token': 'do-not-copy'}, 'confidence': 'high', 'evidence': []},
                      {'value': 10**1000, 'confidence': 'high', 'evidence': []},
                      {'value': 'x' * 2000, 'confidence': 'high', 'evidence': []}):
            candidate = deepcopy(value)
            candidate['fields']['framework'] = field
            self.assertIsNone(fingerprint.validate(candidate, **scope(value)))

    @override_settings(ARTICLES_SITE_FINGERPRINT_ENABLED=False)
    def test_disabled_consumers_do_not_change_discovery_contract(self):
        value = example()
        data = {'repository_inventory': {**scope(value), 'site_fingerprint': value, 'discovery_complete': True}}
        self.assertNotIn('siteFingerprint', discovery_snapshot(data))

    @override_settings(ARTICLES_SITE_FINGERPRINT_ENABLED=True)
    def test_current_framework_reaches_actual_journey_and_strategy_without_authority(self):
        value = example()
        data = {'repository_inventory': {**scope(value), 'site_fingerprint': value, 'discovery_complete': True}}
        discovery = discovery_snapshot(data)
        journey = project_journey(company_id='company', domain='example.test', discovery=discovery,
            website={'status': 'connected', 'verifiedSha': value['source_sha']})
        self.assertEqual(journey['repository']['framework'], 'astro')
        self.assertEqual(initial_rung(journey), 2)
        self.assertFalse(discovery['publishReady'])
        self.assertFalse(journey['capabilities']['canPublishArticle'])
        stale = project_journey(company_id='company', domain='example.test', discovery=discovery,
            website={'status': 'connected', 'verifiedSha': 'b' * 40})
        self.assertIsNone(stale['repository']['framework'])

    @override_settings(ARTICLES_SITE_FINGERPRINT_ENABLED=True, ARTICLES_DEPLOYMENT_DETECTION_ENABLED=True)
    def test_deploy_hint_keeps_automation_unknown_and_does_not_grant_readiness(self):
        value = example()
        connection = SimpleNamespace(generation=1, github_repo=value['github_repo'], branch='main',
            app_root='', capabilities={}, save=Mock(), scan_snapshots=Mock())
        connection.scan_snapshots.filter.return_value.order_by.return_value.first.return_value = SimpleNamespace(
            evidence={'repository_inventory': {'site_fingerprint': value}})
        with patch.object(deployment, 'authority_guard', return_value=nullcontext(connection)), \
             patch.object(deployment, 'read_files', return_value=({}, 'main')):
            result = deployment.ensure_fingerprint(connection, {'source_sha': value['source_sha']})
        self.assertEqual(result['provider'], 'netlify')
        self.assertEqual(result['automation'], 'unknown')
        self.assertNotIn('publishingReady', connection.capabilities)

    @override_settings(ARTICLES_SITE_FINGERPRINT_ENABLED=True, ARTICLES_DEPLOYMENT_DETECTION_ENABLED=True)
    def test_new_current_facts_invalidate_only_the_matching_deployment_cache(self):
        value = example()
        source = value['source_sha']
        saved = {'version': 1, 'provider': 'unknown', 'automation': 'unknown',
            'sourceSha': source, 'connectionGeneration': 1, 'appRoot': ''}
        connection = SimpleNamespace(generation=1, github_repo=value['github_repo'], branch='main',
            app_root='', capabilities={'deploymentFingerprint': saved}, save=Mock(), scan_snapshots=Mock())
        snapshot = SimpleNamespace(evidence={'repository_inventory': {'site_fingerprint': value}})
        connection.scan_snapshots.filter.return_value.order_by.return_value.first.return_value = snapshot
        with patch.object(deployment, 'authority_guard', return_value=nullcontext(connection)), \
             patch.object(deployment, 'read_files', return_value=({}, 'main')) as read:
            result = deployment.ensure_fingerprint(connection, {'source_sha': source})
            self.assertEqual(result['siteFingerprintDigest'], value['digest'])
            self.assertEqual(deployment.ensure_fingerprint(connection, {'source_sha': source}), result)
            read.assert_called_once()
            value['fields']['framework']['value'] = 'other'
            stamp(value)
            self.assertEqual(deployment.ensure_fingerprint(connection, {'source_sha': source})['siteFingerprintDigest'], value['digest'])
            self.assertEqual(read.call_count, 2)

    def test_non_string_hint_is_never_used_as_a_provider(self):
        value = example()
        value['fields']['hosting']['value']['provider'] = {}
        self.assertIsNone(fingerprint.hosting_hint(value))


class SiteFingerprintProvenanceTests(SimpleTestCase):
    def test_pinned_private_source_uses_exact_path_revision_and_read_only_secret(self):
        lock = provenance.verify()
        raw = (provenance.ROOT / provenance.EXAMPLE).read_bytes()
        fetch = Mock(return_value=raw)
        self.assertEqual(provenance.verify(remote=True, token='synthetic-token', fetch=fetch), lock)
        fetch.assert_called_once_with(provenance.SOURCE_PATH, lock['source_revision'], 'synthetic-token')
        with self.assertRaisesRegex(provenance.ContractCheckError, 'TOKEN_required'):
            provenance.verify(remote=True, fetch=Mock(side_effect=AssertionError('Must not fetch without a token')))
        with self.assertRaisesRegex(provenance.ContractCheckError, 'pinned_source_byte_mismatch'):
            provenance.verify(remote=True, token='synthetic-token', fetch=lambda *args: b'changed')

    def test_future_mutable_or_changed_local_contracts_fail_closed(self):
        original = provenance.verify()
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'contracts').mkdir()
            raw = (provenance.ROOT / provenance.EXAMPLE).read_bytes()
            (root / provenance.EXAMPLE).write_bytes(raw)
            lock_path = root / 'contracts/site-fingerprint-v1.lock.json'
            for change in ({'schema_version': 2}, {'schema_version': True}, {'source_revision': 'main'},
                           {'source_repository': 'other/repo'}, {'source_path': 'other.json'}, {'sha256': []}):
                with self.subTest(change=change):
                    lock_path.write_text(json.dumps({**original, **change}))
                    with self.assertRaises(provenance.ContractCheckError):
                        provenance.verify(root)
            lock_path.write_text(json.dumps(original))
            (root / provenance.EXAMPLE).write_bytes(b'changed')
            with self.assertRaisesRegex(provenance.ContractCheckError, 'local_digest_mismatch'):
                provenance.verify(root)
