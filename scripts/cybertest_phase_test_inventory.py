"""Prepare the exact disposable-test migration inventory, never execute it."""
import hashlib
import inspect
import json
import os
from pathlib import Path
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
environment = {'DJANGO_SETTINGS_MODULE': 'mlai.settings', 'APP_ENV': 'test',
               'DEBUG': 'True', 'SECRET_KEY': 'synthetic-two-phase-tests-only',
               'DATABASE_URL': 'sqlite:///:memory:', 'REDIS_URL': ''}
with patch.dict(os.environ, environment, clear=True), patch('dotenv.load_dotenv', return_value=False), \
        patch('socket.socket.connect', side_effect=AssertionError('No network in inventory')):
    import django
    django.setup()
    from django.db.migrations.loader import MigrationLoader
    with patch('django.db.backends.base.base.BaseDatabaseWrapper.ensure_connection',
               side_effect=AssertionError('No database in inventory')):
        loader = MigrationLoader(connection=None)
        if loader.detect_conflicts():
            raise RuntimeError('Migration graph has conflicts.')
        rows = []
        for (app, name), migration in sorted(loader.disk_migrations.items()):
            module = inspect.getmodule(type(migration))
            file = Path(module.__file__).resolve()
            label = str(file.relative_to(ROOT)).replace('\\', '/') if file.is_relative_to(ROOT) else app+'/'+file.name
            rows.append({'app': app, 'name': name, 'file': label,
                         'sha256': hashlib.sha256(file.read_bytes()).hexdigest()})
        digest = hashlib.sha256(json.dumps(rows, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        result = {'purpose': 'Fresh disposable local test database only; no staging or production',
                  'migration_count': len(rows), 'inventory_sha256': digest,
                  'roo_leaf': loader.graph.leaf_nodes('roo'), 'migrations': rows}
        destination = ROOT/'docs'/'cybertest-two-phase-test-migrations.json'
        destination.write_text(json.dumps(result, indent=2)+'\n', encoding='utf-8')
        print(json.dumps({k: v for k, v in result.items() if k != 'migrations'}))
