"""Load production Django settings with synthetic values; prohibit DB/network IO."""
import os
from pathlib import Path
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
enabled = '--enabled' in sys.argv
live = '--live' in sys.argv
if live and not enabled:
    raise ValueError('--live requires --enabled.')

environment = {
    'DJANGO_SETTINGS_MODULE': 'mlai.settings', 'APP_ENV': 'production',
    'DEBUG': 'false', 'SECRET_KEY': 'synthetic-profile-secret-' + 's' * 64,
    'DATABASE_URL': 'postgresql://synthetic:synthetic@127.0.0.1:5432/synthetic',
    'REDIS_URL': 'redis://127.0.0.1:6379/0',
    'HEALTH_HACK_API_KEY': 'synthetic-health-hack-' + 'h' * 48,
    'ROO_SIM_PATIENT_KEY': 'synthetic-patient-' + 'r' * 48,
    'ROO_API_KEY': 'synthetic-roo-' + 'a' * 48,
    'INTERNAL_API_KEY': 'synthetic-internal-' + 'i' * 48,
    'CYBERTEST_ENTRY_PRODUCTION_ENABLED': 'true' if enabled else 'false',
    'CYBERTEST_BILLING_MODE': 'live' if live else 'disabled',
    'CYBERTEST_LIVE_BILLING_ENABLED': 'true' if live else 'false',
    'CYBERTEST_PHASED_SCANS_ENABLED': 'true' if live else 'false',
    'CYBERTEST_BILLING_API_KEY': 'p' * 64,
    'CYBERTEST_APP_ORIGIN': 'https://chat.mlai.au',
    'CYBERTEST_BASE_PATH': '/cybertest',
    'CYBERTEST_PRODUCTION_COMPANY_IDS': '10000000-0000-4000-8000-000000000001',
}
with patch.dict(os.environ, environment, clear=True), patch('dotenv.load_dotenv', return_value=False), \
        patch('socket.socket.connect', side_effect=AssertionError('Network forbidden in profile check')):
    import django
    django.setup()
    from django.conf import settings
    from django.db.backends.base.base import BaseDatabaseWrapper
    with patch.object(BaseDatabaseWrapper, 'ensure_connection', side_effect=AssertionError('Database forbidden in profile check')):
        from roo.cybertest_entry_views import local_origin, entry_metadata
        from roo.cybertest_entry import EntryError
        from roo.cybertest_checkout import configured_mode
        from roo.cybertest_policy import CheckoutError
        assert settings.CYBERTEST_ENTRY_PRODUCTION_ENABLED is enabled
        assert settings.CYBERTEST_ENTRY_STAGING_ENABLED is False
        assert settings.CYBERTEST_ENTRY_LOCAL_ENABLED is False
        assert settings.CYBERTEST_LIVE_BILLING_ENABLED is live
        assert settings.CYBERTEST_PHASED_SCANS_ENABLED is live
        assert settings.CYBERTEST_PERSISTENT_BINDINGS_ENABLED is enabled
        if enabled:
            assert local_origin() == 'https://chat.mlai.au'
            assert entry_metadata() == {'local_test': False, 'entry_mode': 'production'}
        else:
            try:
                local_origin()
            except EntryError:
                pass
            else:
                raise AssertionError('Disabled entry accepted a login.')
        if live:
            assert configured_mode() == 'live'
        else:
            try:
                configured_mode()
            except CheckoutError:
                pass
            else:
                raise AssertionError('Disabled billing accepted a purchase.')
print('Production settings and entry checks passed using synthetic inputs; no database, network or points used.')
