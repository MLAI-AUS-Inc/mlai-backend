"""Cached recipient-domain checks, without changing contact addresses or consent."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from django.core.cache import cache

from crm.models import MailDomainInfo
from crm.sending_domains import lookup

from . import policy

DATA = Path(__file__).resolve().parents[1] / 'data'


def _lines(name):
    return [line.split() for line in (DATA / name).read_text().splitlines() if line.strip() and not line.lstrip().startswith('#')]


DISPOSABLE = frozenset(parts[0] for parts in _lines('disposable_domains.txt'))
TYPO = {parts[0]: parts[1] for parts in _lines('typo_domains.txt')}
BLOCKED_ROLES = frozenset(('noreply', 'no-reply', 'postmaster', 'abuse', 'mailer-daemon'))
WARN_ROLES = frozenset(('info', 'admin', 'sales', 'support', 'contact'))


def disposable(domain):
    labels = domain.lower().rstrip('.').split('.')
    return any('.'.join(labels[index:]) in DISPOSABLE for index in range(len(labels) - 1))


@dataclass(frozen=True)
class DomainInfo:
    domain: str
    provider: str
    mx: tuple
    has_mx: bool | None
    disposable: bool
    checked_at: datetime


def provider_for_mx(records):
    """Match MX label boundaries, never a lookalike suffix."""
    hosts = [str(record).split()[-1].rstrip('.').lower() for record in records if str(record).split()]
    for provider, suffixes in {'google': ('google.com', 'googlemail.com'),
            'microsoft': ('outlook.com', 'hotmail.com'), 'yahoo': ('yahoodns.net',),
            'apple': ('icloud.com', 'me.com')}.items():
        if any(host == suffix or host.endswith('.' + suffix) for host in hosts for suffix in suffixes):
            return provider
    return 'other'


def inspect_domain(domain, now):
    records = lookup(domain, 'MX')
    has_mx = None if records is None else any(len(str(record).split()) >= 2 and str(record).split()[-1].strip('.') for record in records)
    info = DomainInfo(domain, provider_for_mx(records or ()), tuple(records or ()), has_mx, disposable(domain), now)
    cache.set('crm:recipient-domain:' + domain, info,
        timeout=policy.value('CRM_DOMAIN_ERROR_CACHE_SECONDS') if has_mx is None else int(timedelta(days=policy.value('CRM_DOMAIN_CACHE_DAYS')).total_seconds()))
    return info


def domain_infos(addresses, now, *, cached_only=False):
    """Preview may refresh the external cache; it writes no CRM rows."""
    domains = sorted({address.rpartition('@')[2].lower() for address in addresses})
    if not domains:
        return {}
    cutoff = now - timedelta(days=policy.value('CRM_DOMAIN_CACHE_DAYS'))
    found = {row.domain: DomainInfo(row.domain, row.provider, tuple(row.mx), row.has_mx,
        row.disposable or disposable(row.domain), row.checked_at)
        for row in MailDomainInfo.objects.filter(domain__in=domains, checked_at__gte=cutoff, checked_at__lte=now)}
    for domain in domains:
        if domain in found:
            continue
        saved = cache.get('crm:recipient-domain:' + domain)
        if isinstance(saved, DomainInfo) and cutoff <= saved.checked_at <= now:
            found[domain] = saved
    missing = [domain for domain in domains if domain not in found]
    if not cached_only and missing:
        with ThreadPoolExecutor(max_workers=policy.value('CRM_DOMAIN_CHECK_WORKERS')) as pool:
            for info in pool.map(lambda domain: inspect_domain(domain, now), missing):
                found[info.domain] = info
    return found


def exclusion(contact, info, purpose):
    """First matching reason wins; unanswered MX never becomes a false no-MX result."""
    domain = contact.email.rpartition('@')[2].lower()
    if info is None or info.has_mx is None:
        return 'domain_unverified'
    if not info.has_mx:
        return 'no_mx'
    if info.disposable or disposable(domain):
        return 'disposable'
    if domain in TYPO:
        return 'likely_typo'
    if purpose == 'marketing' and contact.email.partition('@')[0].lower() in BLOCKED_ROLES:
        return 'role_address'
    if contact.soft_bounce_streak >= policy.value('CRM_GUARD_SOFT_BOUNCE_LIMIT'):
        return 'repeated_soft_bounce'
    return None


def remember(infos):
    """Persist only the verified cached facts used by an admitted snapshot."""
    for info in infos.values():
        if info.has_mx is not None:
            MailDomainInfo.objects.update_or_create(domain=info.domain, defaults={
                'provider': info.provider, 'mx': list(info.mx), 'has_mx': info.has_mx,
                'disposable': info.disposable, 'checked_at': info.checked_at})
