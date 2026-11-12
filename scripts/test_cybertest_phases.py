"""Run approved migrations/tests in a new in-memory SQLite database only.

No dotenv, inherited credentials, outbound network, staging or production DB.
The 396-migration inventory was specifically authorized by Eva on 2026-11-13.
"""
import hashlib
import inspect
import json
import os
from pathlib import Path
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
APPROVED = '7b0e11050cc25c68fe060d8c1444e1c9d9bf92a968c3660fe787125200feca3f'
env = {'DJANGO_SETTINGS_MODULE': 'mlai.settings', 'APP_ENV': 'test', 'DEBUG': 'True',
       'SECRET_KEY': 'synthetic-two-phase-tests-only', 'DATABASE_URL': 'sqlite:///:memory:', 'REDIS_URL': ''}
service_only = '--service-only' in sys.argv
sys.argv = [sys.argv[0], 'test']
with patch.dict(os.environ, env, clear=True), patch('dotenv.load_dotenv', return_value=False), \
        patch('socket.socket.connect', side_effect=AssertionError('External network forbidden in local billing tests')):
    import django
    django.setup()
    if not hasattr(os, 'register_at_fork'):
        with patch.object(os, 'register_at_fork', lambda **kwargs: None, create=True):
            import content_factory.website_runtime_observation
    from django.db.migrations.loader import MigrationLoader
    from django.conf import settings
    from django.test.runner import DiscoverRunner
    loader = MigrationLoader(connection=None)
    rows = []
    for (app, name), migration in sorted(loader.disk_migrations.items()):
        file = Path(inspect.getmodule(type(migration)).__file__).resolve()
        label = str(file.relative_to(ROOT)).replace('\\', '/') if file.is_relative_to(ROOT) else app+'/'+file.name
        rows.append({'app': app, 'name': name, 'file': label, 'sha256': hashlib.sha256(file.read_bytes()).hexdigest()})
    digest = hashlib.sha256(json.dumps(rows, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    if digest != APPROVED or len(rows) != 396 or loader.detect_conflicts():
        raise RuntimeError('Migration inventory changed; do not initialize the test database.')
    if settings.DATABASES['default']['ENGINE'] != 'django.db.backends.sqlite3' or settings.DATABASES['default']['NAME'] != ':memory:':
        raise RuntimeError('Tests require a fresh in-memory SQLite database.')
    settings.DATABASES['default'].setdefault('TEST', {})['NAME'] = ':memory:'
    labels = ['roo.test_cybertest_phase_api'] if service_only else [
        'roo.tests_cybertest_phases', 'roo.tests_cybertest_checkout',
        'roo.test_cybertest_policy', 'roo.test_cybertest_delivery', 'roo.test_cybertest_plan', 'roo.test_cybertest_phase_api']
    failed = DiscoverRunner(verbosity=1, interactive=False).run_tests(labels)
raise SystemExit(bool(failed))
