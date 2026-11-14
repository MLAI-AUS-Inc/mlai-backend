"""Explicit configuration applied after the isolated staging provider scrub."""
from urllib.parse import urlsplit
from uuid import UUID
from .cybertest_paths import base_path


def _refuse():
    # Never include configuration values: this code handles private credentials.
    raise ValueError('Vibe Security staging configuration is invalid or incomplete.')


def _flag(values, name):
    value = values.get(name, 'false')
    if value not in ('true', 'false'):
        _refuse()
    return value == 'true'


def staging_entry_settings(values):
    """Return only test-service settings; never restore other provider authority.

    Called last by the hosted staging settings profile. The operator supplies
    independent service credentials through the staging runtime, not a production
    dotenv. Disabled mode discards any stale service authority.
    """
    result = {
        'CYBERTEST_ENTRY_STAGING_ENABLED': False,
        'CYBERTEST_ENTRY_LOCAL_ENABLED': False,
        'CYBERTEST_LIVE_BILLING_ENABLED': False,
        'CYBERTEST_PERSISTENT_BINDINGS_ENABLED': False,
        'CYBERTEST_BILLING_API_KEY': '',
        'CYBERTEST_BILLING_MODE': 'disabled',
        'CYBERTEST_APP_ORIGIN': '',
        'CYBERTEST_BASE_PATH': '',
        'CYBERTEST_STAGING_COMPANY_IDS': [],
    }
    if not _flag(values, 'CYBERTEST_ENTRY_STAGING_ENABLED'):
        return result
    if (values.get('APP_ENV') != 'chat-staging' or _flag(values, 'DEBUG')
            or _flag(values, 'CYBERTEST_ENTRY_LOCAL_ENABLED')
            or _flag(values, 'CYBERTEST_LIVE_BILLING_ENABLED')):
        _refuse()
    mode = values.get('CYBERTEST_BILLING_MODE', 'disabled')
    if mode not in ('disabled', 'test'):
        _refuse()
    key = values.get('CYBERTEST_BILLING_API_KEY', '')
    if not isinstance(key, str) or not 32 <= len(key) <= 512 or any(ord(c) < 33 or ord(c) > 126 for c in key):
        _refuse()
    for name, value in values.items():
        if (name != 'CYBERTEST_BILLING_API_KEY'
                and any(word in name for word in ('SECRET', 'PASSWORD', 'TOKEN', 'API_KEY'))
                and value == key):
            _refuse()
    try:
        prefix=base_path(values.get('CYBERTEST_BASE_PATH', ''))
        origin = urlsplit(values.get('CYBERTEST_APP_ORIGIN', ''))
        origin.port
        if (origin.scheme != 'https' or not origin.hostname or origin.username or origin.password
                or origin.path not in ('', '/') or origin.query or origin.fragment
                or (origin.hostname == 'chat-staging.mlai.au' and (prefix != '/cybertest' or origin.port not in (None, 443)))
                or origin.hostname in {'chat.mlai.au', 'api.mlai.au',
                                       'api-staging.mlai.au', 'cybertest-attack-loop-zzuwy.ondigitalocean.app'}):
            _refuse()
        companies = values.get('CYBERTEST_STAGING_COMPANY_IDS', '').split(',')
        if not 1 <= len(companies) <= 100:
            _refuse()
        companies = sorted({str(UUID(value.strip())) for value in companies})
    except (ValueError, TypeError, AttributeError):
        _refuse()
    result.update(CYBERTEST_ENTRY_STAGING_ENABLED=True,
                  CYBERTEST_PERSISTENT_BINDINGS_ENABLED=True,
                  CYBERTEST_BILLING_API_KEY=key, CYBERTEST_BILLING_MODE=mode,
                  CYBERTEST_APP_ORIGIN=origin.geturl().rstrip('/'),
                  CYBERTEST_BASE_PATH=prefix,
                  CYBERTEST_STAGING_COMPANY_IDS=companies)
    return result
