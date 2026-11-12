"""Explicit production authority, separate from the isolated staging profile."""
from uuid import UUID


def _refuse():
    # Configurations contain credentials; never echo their values.
    raise ValueError('Vibe Security production configuration is invalid or incomplete.')


def _flag(values, name):
    value = values.get(name, 'false')
    if value not in ('true', 'false'):
        _refuse()
    return value == 'true'


def production_entry_settings(values):
    """Validate explicit production opt-in without changing legacy/local defaults.

    Disabled billing permits deployment and login checks before purchases open.
    Receipt recovery keeps its existing independent settlement authorization.
    """
    result = {'CYBERTEST_ENTRY_PRODUCTION_ENABLED': False,
              'CYBERTEST_PRODUCTION_COMPANY_IDS': []}
    if not _flag(values, 'CYBERTEST_ENTRY_PRODUCTION_ENABLED'):
        return result
    if (values.get('APP_ENV') not in ('production', 'prod')
            or _flag(values, 'DEBUG')
            or _flag(values, 'CYBERTEST_ENTRY_STAGING_ENABLED')
            or _flag(values, 'CYBERTEST_ENTRY_LOCAL_ENABLED')
            or values.get('CYBERTEST_APP_ORIGIN') != 'https://chat.mlai.au'
            or values.get('CYBERTEST_BASE_PATH') != '/cybertest'):
        _refuse()
    mode = values.get('CYBERTEST_BILLING_MODE', 'disabled')
    live = _flag(values, 'CYBERTEST_LIVE_BILLING_ENABLED')
    phased = _flag(values, 'CYBERTEST_PHASED_SCANS_ENABLED')
    if mode not in ('disabled', 'live') or live != (mode == 'live') or (live and not phased):
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
        companies = values.get('CYBERTEST_PRODUCTION_COMPANY_IDS', '').split(',')
        if not 1 <= len(companies) <= 100:
            _refuse()
        companies = sorted({str(UUID(value.strip())) for value in companies})
    except (ValueError, TypeError, AttributeError):
        _refuse()
    result.update(
        CYBERTEST_ENTRY_PRODUCTION_ENABLED=True,
        CYBERTEST_ENTRY_LOCAL_ENABLED=False,
        CYBERTEST_ENTRY_STAGING_ENABLED=False,
        CYBERTEST_PERSISTENT_BINDINGS_ENABLED=True,
        CYBERTEST_BILLING_MODE=mode,
        CYBERTEST_LIVE_BILLING_ENABLED=live,
        CYBERTEST_PHASED_SCANS_ENABLED=phased,
        CYBERTEST_BILLING_API_KEY=key,
        CYBERTEST_APP_ORIGIN='https://chat.mlai.au',
        CYBERTEST_BASE_PATH='/cybertest',
        CYBERTEST_PRODUCTION_COMPANY_IDS=companies,
    )
    return result
