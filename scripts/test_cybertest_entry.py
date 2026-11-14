"""Isolated local-entry contracts: no migrations, DB or external network."""
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
os.environ.update(DJANGO_SETTINGS_MODULE='mlai.settings',DEBUG='True',APP_ENV='test',
                  SECRET_KEY='cybertest-local-entry-tests-only',DATABASE_URL='sqlite:///:memory:',REDIS_URL='')
with patch('dotenv.load_dotenv',return_value=False), patch('socket.socket.connect',side_effect=AssertionError('No network in local entry tests')):
    import django
    django.setup()
    with patch('django.db.backends.base.base.BaseDatabaseWrapper.ensure_connection',side_effect=AssertionError('No database in local entry tests')):
        suite=unittest.defaultTestLoader.loadTestsFromNames(['roo.test_cybertest_entry','roo.tests_cybertest_entry_views'])
        result=unittest.TextTestRunner(verbosity=2).run(suite)
raise SystemExit(not result.wasSuccessful())
