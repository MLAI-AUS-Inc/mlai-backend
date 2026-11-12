"""396 migrations approved 2026-11-13; isolated local PostgreSQL, no inherited secrets."""
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
       'SECRET_KEY': 'synthetic-local-concurrency-only', 'REDIS_URL': '',
       'DATABASE_URL': 'postgresql://cybertest_phase_test@127.0.0.1:55473/cybertest_phase_seed?connect_timeout=5'}
sys.argv = [sys.argv[0], 'test']
with patch.dict(os.environ, env, clear=True), patch('dotenv.load_dotenv', return_value=False), \
        patch('socket.socket.connect', side_effect=AssertionError('External network forbidden in local concurrency tests')):
    import django
    django.setup()
    if not hasattr(os, 'register_at_fork'):
        with patch.object(os, 'register_at_fork', lambda **kwargs: None, create=True):
            import content_factory.website_runtime_observation
    from django.db.migrations.loader import MigrationLoader
    from django.db import connection
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
        raise RuntimeError('Migration inventory changed; no migration is permitted.')
    db = settings.DATABASES['default']
    if (connection.vendor, db['HOST'], str(db['PORT']), db['NAME'], db['USER']) != (
            'postgresql', '127.0.0.1', '55473', 'cybertest_phase_seed', 'cybertest_phase_test'):
        raise RuntimeError('Only the dedicated disposable local PostgreSQL instance is allowed.')
    with connection.cursor() as cursor:
        cursor.execute('SELECT datname FROM pg_database WHERE NOT datistemplate ORDER BY datname')
        if cursor.fetchall() != [('cybertest_phase_seed',), ('postgres',)]:
            raise RuntimeError('Fresh instance required; do not overwrite or drop an existing database.')
        cursor.execute("SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public'")
        if cursor.fetchone()[0] != 0:
            raise RuntimeError('The local seed database is not empty.')
    db.setdefault('TEST', {})['NAME'] = 'test_cybertest_phase_concurrency'
    failed = DiscoverRunner(verbosity=1, interactive=False).run_tests(['roo.tests_cybertest_concurrency.PhaseConcurrencyTests'])
raise SystemExit(bool(failed))
