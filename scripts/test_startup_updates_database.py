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
INVENTORY = ROOT / 'docs/startup-update-pr-test-migrations-2026-09-20.json'
LABELS = ['community_chat.tests.test_startup_updates_database', 'startup_updates.tests_revisions', 'startup_updates.tests_update_identity', 'startup_updates.tests_reporting_canaries', 'vibe_raising.tests_company_scoping', 'vibe_raising.tests_progress_api']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--approved-inventory-sha256', required=True)
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
                return DiscoverRunner(verbosity=2, interactive=False).run_tests(LABELS)


if __name__ == '__main__':
    raise SystemExit(main())
