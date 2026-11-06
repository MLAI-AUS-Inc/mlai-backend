"""Consecutive DNS drift checks and bounded sender-authentication advisories."""

import base64
import re
from datetime import timedelta

from cryptography.hazmat.primitives.serialization import load_der_public_key
from django.db import transaction
from django.utils.dateparse import parse_datetime

from crm import sending_domains
from crm.models import SendingStream

from .. import observations, policy, scopes
from .http import remaining


def spf_lookups(domain, *, lookup=sending_domains.lookup, seen=None, depth=0):
    """Count DNS-causing SPF terms recursively, detecting loops and the 10-lookup ceiling."""
    seen = set() if seen is None else seen
    if domain in seen or depth > 10:
        return {'status': 'warn', 'lookups': 11, 'reason': 'loop_or_limit'}
    seen = seen | {domain}
    remaining(1)
    txt = lookup(domain, 'TXT')
    if txt is None:
        return {'status': 'unknown', 'lookups': None}
    rows = [row.strip('"') for row in txt if row.strip('"').lower().startswith('v=spf1')]
    if not rows:
        return {'status': 'missing', 'lookups': None}
    if len(rows) != 1:
        return {'status': 'warn', 'lookups': None, 'reason': 'multiple_records'}
    count = 0
    for term in rows[0].split()[1:]:
        term = term.lstrip('+-~?').lower()
        mechanism = re.split(r'[:=/]', term, 1)[0]
        if mechanism not in ('include', 'a', 'mx', 'ptr', 'exists', 'redirect'):
            continue
        count += 1
        if mechanism in ('include', 'redirect'):
            target = term.split(':' if mechanism == 'include' else '=', 1)[-1]
            if not re.fullmatch(r'[a-z0-9.-]{1,253}', target):
                return {'status': 'unknown', 'lookups': None}
            child = spf_lookups(target, lookup=lookup, seen=seen, depth=depth + 1)
            if child['lookups'] is None:
                return {'status': 'unknown', 'lookups': None}
            count += child['lookups']
        if count > 10:
            return {'status': 'warn', 'lookups': count, 'reason': 'lookup_limit'}
    return {'status': 'pass' if count <= 10 else 'warn', 'lookups': count}


def advisories(domain):
    root = sending_domains.lookup('_dmarc.mlai.au', 'TXT')
    dmarc = next((row for row in root or [] if row.strip('"').lower().startswith('v=dmarc1')), '')
    match = re.search(r'(?:^|;)\s*sp\s*=\s*([a-z]+)', dmarc, re.I)
    bits = None
    try:
        records = sending_domains.lookup('resend._domainkey.' + domain, 'TXT') or []
        for row in records:
            key = re.search(r'(?:^|;)\s*p\s*=\s*([^;\s"]+)', row.strip('"'))
            if key:
                bits = getattr(load_der_public_key(base64.b64decode(key[1], validate=True)), 'key_size', None)
                break
    except (ValueError, TypeError):
        pass
    return {'root_dmarc': 'present' if dmarc else 'unknown' if root is None else 'missing',
        'root_subdomain_policy': match[1].lower() if match else 'inherits_root',
        'dkim_bits': bits, 'dkim_key': 'pass' if bits and bits >= 1024 else 'warn' if bits else 'unknown',
        'root_spf': spf_lookups('mlai.au')}


def check(stream, *, now):
    result = sending_domains.domain_health(stream.domain, stream.mailbox.route, refresh=True, now=now)
    passed = result.get('status') == 'verified'
    with transaction.atomic():
        locked = SendingStream.objects.select_for_update().get(pk=stream.pk)
        state = dict((locked.state_detail or {}).get('dns_checks') or {})
        previous = parse_datetime(state.get('checked_at', ''))
        interval = timedelta(seconds=policy.value('CRM_DNS_CHECK_SECONDS'))
        # Manual repeated checks and concurrent schedulers cannot count as distinct recovery passes.
        if previous and now > previous + interval * 3:
            state['passes'], state['failures'] = 0, 0
        spaced = previous is None or now >= previous + interval
        if spaced:
            state['passes'] = (state.get('passes', 0) + 1) if passed else 0
            state['failures'] = 0 if passed else state.get('failures', 0) + 1
            state['checked_at'] = now.isoformat()
            locked.state_detail = {**(locked.state_detail or {}), 'dns_checks': state}
            locked.save(update_fields=['state_detail', 'updated_at'])
    status = 'pass' if passed and state.get('passes', 0) >= policy.value('CRM_DNS_RECOVERY_PASSES') else 'recovering' if passed else 'fail'
    output = {'status': status, 'verified': passed, 'passes': state.get('passes', 0), 'failures': state.get('failures', 0),
        'records': [{key: row.get(key) for key in ('key', 'required', 'status')} for row in result.get('records') or []],
        'advisories': advisories(stream.domain)}
    if not passed and state.get('failures', 0) >= policy.value('CRM_DNS_FAILURE_CHECKS'):
        from ..guardian import Signal, detect
        detect(None, Signal('dns_drift', 'amber', state['failures'], policy.value('CRM_DNS_FAILURE_CHECKS'), state['failures'],
            'hold_stream', evidence_key=scopes.evidence_key([row['key'] for row in output['records'] if row.get('required') and row.get('status') != 'pass'])), stream=stream, now=now)
    return observations.record(stream, 'dns', output, now=now)
