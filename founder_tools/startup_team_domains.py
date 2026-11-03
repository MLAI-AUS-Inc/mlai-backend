"""Offline registrable-domain matching for the separately gated team join path.

The full ICANN and private PSL snapshot is vendored with provenance and license.
No request downloads domain rules, and unknown suffixes fail closed here.
"""
from functools import lru_cache
import hashlib
import ipaddress
import json
from pathlib import Path

import idna
from django.conf import settings

DATA = Path(__file__).with_name('data')
SHARED = frozenset('vercel.app netlify.app github.io wixsite.com squarespace.com pages.dev'.split())
MAIL = frozenset('gmail.com googlemail.com yahoo.com yahoo.com.au outlook.com hotmail.com live.com msn.com '
    'icloud.com me.com mac.com aol.com proton.me protonmail.com pm.me fastmail.com hey.com mail.com '
    'gmx.com gmx.de web.de yandex.com zoho.com bigpond.com bigpond.net.au telstra.com optusnet.com.au '
    'iinet.net.au westnet.com.au tpg.com.au internode.on.net aapt.net.au dodo.com.au netspace.net.au '
    'ozemail.com.au exemail.com.au yahoo.co.uk hotmail.co.uk btinternet.com virginmedia.com comcast.net '
    'verizon.net att.net sbcglobal.net bellsouth.net orange.fr wanadoo.fr free.fr laposte.net'.split())


def canonical_host(value):
    """Accept a bare public DNS name and return its canonical ASCII spelling."""
    if not isinstance(value, str) or not value or value != value.strip() or any(c in value for c in '/:@?#\\'):
        return None
    try:
        host = idna.encode(value.rstrip('.'), uts46=True, std3_rules=True).decode('ascii').lower()
    except idna.IDNAError:
        return None
    try:
        ipaddress.ip_address(host)
        return None
    except ValueError:
        pass
    if len(host) > 253 or '.' not in host:
        return None
    return host


@lru_cache(maxsize=1)
def _rules():
    raw = (DATA / 'public_suffix_list.dat').read_bytes()
    manifest = json.loads((DATA / 'public_suffix_list.json').read_text())
    if hashlib.sha256(raw).hexdigest() != manifest['sha256']:
        raise ValueError('The pinned public suffix list failed verification.')
    exact, wildcard, exceptions = set(), set(), set()
    for line in raw.decode('utf8').splitlines():
        if not line or line.startswith('//'):
            continue
        rule = line.split()[0]
        target = exceptions if rule.startswith('!') else wildcard if rule.startswith('*.') else exact
        rule = rule[1:] if rule.startswith('!') else rule[2:] if rule.startswith('*.') else rule
        target.add(idna.encode(rule, uts46=True, std3_rules=True).decode('ascii').lower())
    return exact, wildcard, exceptions


def registrable_domain(value):
    """Apply longest-rule and exception semantics, including private suffixes."""
    host = canonical_host(value)
    if host is None:
        return None
    labels = host.split('.')
    exact, wildcard, exceptions = _rules()
    count = 0
    for index in range(len(labels)):
        suffix = '.'.join(labels[index:])
        if suffix in exceptions:
            count = len(labels) - index - 1
            break
        if suffix in exact:
            count = max(count, len(labels) - index)
        if index > 0 and suffix in wildcard:
            count = max(count, len(labels) - index + 1)
    return '.'.join(labels[-count - 1:]) if count and len(labels) > count else None


def denied_domain(value):
    """Refuse shared hosts, free mail, ISP mail and operator-added suffixes."""
    host = canonical_host(value)
    if host is None:
        return True
    raw = getattr(settings, 'MY_STARTUP_TEAM_DOMAIN_DENYLIST', '') or ''
    extra = raw if isinstance(raw, (list, tuple, set)) else raw.split(',')
    blocked = SHARED | MAIL | {item.strip().lower().lstrip('*.') for item in extra if isinstance(item, str) and item.strip()}
    from content_factory.preview_domain_lists import FREE_MAIL_BRANDS, FREE_MAIL_DOMAINS, SHARED_HOST_SUFFIXES, UNCLAIMABLE_ROOTS
    apex = registrable_domain(host)
    return (any(host == item or host.endswith('.' + item) for item in blocked | FREE_MAIL_DOMAINS | SHARED_HOST_SUFFIXES | UNCLAIMABLE_ROOTS)
            or apex is None or apex.split('.')[0] in FREE_MAIL_BRANDS)
