"""Parse the pinned listmonk v6.2 settings contract without retaining credentials."""

import re

from django.core.cache import cache

from crm import listmonk

from .. import policy


def listmonk_contract(data, stream, template):
    """Return names of failed requirements, never settings blobs or SMTP secrets."""
    if not isinstance(data, dict):
        return {'status': 'unavailable', 'critical': ['settings_unavailable'], 'warnings': []}
    failures = []
    if data.get('privacy.unsubscribe_header') is not True:
        failures.append('unsubscribe_header')
    if str(data.get('app.root_url') or '').rstrip('/') != policy.value('CRM_LISTMONK_PUBLIC_URL').rstrip('/'):
        failures.append('public_url')
    rate = data.get('app.message_rate')
    if type(rate) is not int or not 0 < rate <= policy.value('CRM_LISTMONK_MAX_RATE'):
        failures.append('message_rate')
    actions = data.get('bounce.actions')
    if data.get('bounce.enabled') is not True or not isinstance(actions, dict):
        failures.append('bounce_processing')
    else:
        for kind in ('hard', 'complaint'):
            if not isinstance(actions.get(kind), dict) or actions[kind].get('action') != 'blocklist':
                failures.append(kind + '_bounce_action')
        if isinstance(actions.get('soft'), dict) and actions['soft'].get('action') not in ('none', ''):
            failures.append('soft_bounce_action')
    routes = [row for row in data.get('smtp') or [] if isinstance(row, dict) and row.get('enabled') is True
        and stream.domain in [str(value).strip().lower() for value in row.get('from_addresses') or []]]
    if len(routes) != 1:
        failures.append('smtp_domain_route')
    elif routes[0].get('tls_type') not in ('TLS', 'STARTTLS') or routes[0].get('tls_skip_verify') is not False:
        failures.append('smtp_tls')
    if not isinstance(template, str) or not re.search(r'{{\s*UnsubscribeURL\s*}}', template):
        failures.append('unsubscribe_template')
    return {'status': 'fail' if failures else 'pass', 'critical': failures, 'warnings': []}


def check_listmonk(stream, *, now, template, refresh=False):
    key = 'crm:deliverability:config:' + str(stream.pk)
    if not refresh:
        cached = cache.get(key)
        if isinstance(cached, dict):
            return cached
    try:
        data = listmonk._request('GET', '/api/settings')
        result = listmonk_contract(data, stream, template)
    except listmonk.ListmonkError as exc:
        result = {'status': 'unavailable', 'critical': [exc.code], 'warnings': []}
    result['checked_at'] = now.isoformat()
    # Only the intentionally small result is cached; the provider response can
    # include masked passwords and unrelated operational configuration.
    cache.set(key, result, timeout=policy.value('CRM_CONFIG_CHECK_SECONDS'))
    return result


def check(stream, *, now):
    """Inspect both providers without persisting their settings, keys or seed addresses."""
    from crm.models import SeedMailbox
    from .. import observations, scopes
    from ..preflight import render, seed_inbox
    from . import resend
    from .http import Unavailable
    critical, warnings = [], []
    results = {}
    if not stream.transactional:
        try:
            template = render.template_body(refresh=True)
            results['listmonk'] = check_listmonk(stream, now=now, template=template, refresh=True)
        except listmonk.ListmonkError:
            results['listmonk'] = {'status': 'unavailable', 'critical': ['listmonk_unavailable'], 'warnings': []}
        critical += results['listmonk'].get('critical', [])
    try:
        results['resend'] = resend.check(stream)
    except Unavailable:
        results['resend'] = {'status': 'unknown', 'critical': [], 'warnings': ['resend_unavailable']}
    critical += results['resend']['critical']
    warnings += results['resend']['warnings']
    seeds = [{'provider': row.provider, 'status': seed_inbox.login_check(row)}
        for row in SeedMailbox.objects.filter(enabled=True).order_by('pk')]
    if not seeds or any(row['status'] != 'pass' for row in seeds):
        warnings.append('seed_access_incomplete')
    result = {'status': 'fail' if critical else 'unknown' if warnings else 'pass',
        'critical': sorted(set(critical)), 'warnings': sorted(set(warnings)), 'providers': results, 'seeds': seeds}
    if critical:
        from ..guardian import Signal, detect
        detect(None, Signal('listmonk_down', 'amber', len(set(critical)), 1, len(set(critical)), 'hold_stream',
            evidence_key=scopes.evidence_key(critical)), stream=stream, now=now)
    return observations.record(stream, 'config', result, now=now)
