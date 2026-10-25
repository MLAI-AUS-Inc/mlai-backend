"""Apply the specifically approved inventory to a fresh disposable SQLite DB only.

Requires explicit user approval per AGENTS.md. Never reads .env, accepts an
external database URL, or permits network calls. Supplying a digest guards the
approved scope; it is not a substitute for user authorization.
"""
import argparse
import hashlib
import importlib
import json
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
INVENTORY = ROOT / 'docs/pulse-readability-test-migrations-2026-10-25.json'
LABELS = ['community_chat.tests.test_startup_readability_database']
REGRESSION_LABELS = [
    'community_chat.tests.test_startup_updates_database',
    'startup_updates.tests_revisions.MonthlyRevisionTests',
    'startup_updates.tests_revisions.MonthlyEvidencePipelineTests',
    'startup_updates.tests_revisions.FounderEvidenceApiTests',
    'startup_updates.tests_update_identity',
    'startup_updates.tests_reporting_canaries', 'startup_updates.tests_founder_metrics',
    'vibe_raising.tests_company_scoping', 'vibe_raising.tests_progress_api',
]


def load_generated_narrative(path):
    """Import only narrative from an explicitly synthetic worker sample."""
    sample = json.loads(path.read_text())
    source = sample.get('source') or sample
    if source.get('synthetic') is not True or not str(source.get('domain', '')).endswith('.invalid'):
        raise ValueError('Only a synthetic sample with an .invalid domain may be replayed.')
    memo = sample['draft']['structured_memo']

    def plain(value):
        if isinstance(value, list):
            return '\n'.join(plain(item) for item in value)
        if isinstance(value, dict):
            return str(value.get('text') or '')
        return str(value or '')

    mapping = {'startupOverview': 'startupOverview', 'summary': 'topline', 'highlights': 'highlights',
        'challenges': 'lowlights', 'learnings': 'learnings', 'next30Days': 'next_30_days', 'asks': 'asks'}
    narrative = {field: plain(memo.get(key)) for field, key in mapping.items()}
    narrative['highlights'] = '\n'.join(filter(None, (plain(memo.get('highlights')), plain(memo.get('operations')))))
    if not narrative['summary'] or len(narrative['startupOverview']) > 2000:
        raise ValueError('The sample must have a summary and a bounded startup overview.')
    return narrative, {
        'generationModel': sample.get('generation_model'), 'reviewModel': sample.get('review_model'),
        'workerReviews': sample.get('reviews'),
        'sampleSha256': hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--approved-inventory-sha256', required=True)
    parser.add_argument('--generated-sample', type=Path, help='Replay narrative from the approved synthetic Valley preflight JSON.')
    parser.add_argument('--include-regressions', action='store_true', help='Also run existing startup identity, review, financial and progress regressions.')
    parser.add_argument('--sample-output', type=Path, help='Optional JSON trace containing synthetic fixture data only.')
    args = parser.parse_args()
    raw = INVENTORY.read_bytes()
    if hashlib.sha256(raw).hexdigest() != args.approved_inventory_sha256:
        raise SystemExit('The inventory does not match the explicitly approved digest.')
    inventory = json.loads(raw)
    sys.path.insert(0, str(ROOT))
    with tempfile.TemporaryDirectory(prefix='mlai-startup-tests-') as directory:
        environment = {
            'PATH': os.defpath, 'APP_ENV': 'test', 'APP_RELEASE': 'startup-synthetic-tests',
            'DEBUG': 'true', 'SECRET_KEY': 'startup-synthetic-test-key',
            'DATABASE_URL': f'sqlite:///{directory}/startup.sqlite3',
            'DJANGO_SETTINGS_MODULE': 'mlai.settings', 'ALLOWED_HOSTS': 'testserver,localhost',
            'CONNECTOR_CREDENTIAL_KEYS': '{"test":"MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="}',
            'CONNECTOR_CREDENTIAL_ACTIVE_KEY_ID': 'test',
        }
        with patch.dict(os.environ, environment, clear=True), patch('dotenv.load_dotenv', return_value=False), patch('socket.socket.connect', side_effect=AssertionError('Network forbidden')), patch('socket.create_connection', side_effect=AssertionError('Network forbidden')):
            import django
            from django.conf import settings
            settings.DATABASES['default']['TEST'] = {'NAME': f'{directory}/test_startup.sqlite3'}
            settings.CACHES = {alias: {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache', 'LOCATION': f'startup-tests-{alias}'} for alias in settings.CACHES}
            settings.EMAIL_BACKEND = 'django.core.mail.backends.locmem.EmailBackend'
            django.setup()
            if args.generated_sample:
                narrative, provenance = load_generated_narrative(args.generated_sample)
                fixture_module = importlib.import_module(LABELS[0])
                fixture_module.FIXTURE['narrative'] = narrative
                fixture_module.FIXTURE['authorship'] = 'Actual configured-model output from the approved synthetic Valley preflight.'
                fixture_module.FIXTURE['model_provenance'] = provenance
            from django.db.backends.base.base import BaseDatabaseWrapper
            from django.db.migrations.loader import MigrationLoader
            from django.test.runner import DiscoverRunner
            expected = {(row['app'], row['name']): row['sha256'] for row in inventory['migrations']}
            loader = MigrationLoader(None)
            if set(loader.disk_migrations) != set(expected):
                raise RuntimeError('Migration inventory changed; obtain new approval.')
            for key, migration in loader.disk_migrations.items():
                module = importlib.import_module(migration.__module__)
                if hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest() != expected[key]:
                    raise RuntimeError(f'Migration changed: {key}; obtain new approval.')
            original = BaseDatabaseWrapper.ensure_connection

            def only_disposable_sqlite(wrapper):
                config = wrapper.settings_dict
                if config['ENGINE'] != 'django.db.backends.sqlite3' or not str(config['NAME']).startswith(directory + '/'):
                    raise RuntimeError('Refusing a non-disposable database.')
                return original(wrapper)

            with patch.object(BaseDatabaseWrapper, 'ensure_connection', only_disposable_sqlite):
                failures = DiscoverRunner(verbosity=2, interactive=False).run_tests(LABELS + (REGRESSION_LABELS if args.include_regressions else []))
                if not failures and args.sample_output:
                    sample = importlib.import_module(LABELS[0]).SAMPLE_FLOW
                    if not sample.get('synthetic'):
                        raise RuntimeError('Refusing to export a non-synthetic sample.')
                    args.sample_output.write_text(json.dumps(sample, indent=2, default=str) + '\n')
                return failures


if __name__ == '__main__':
    raise SystemExit(main())
