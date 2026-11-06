"""Domain blocklists and full-URL reputation; refusal and missing tests remain unknown."""

import hashlib
import ipaddress
import re
from datetime import timedelta

from django.conf import settings
from django.core.cache import cache

from crm.models import ReputationCheck

from .. import observations, policy, scopes
from ..preflight.links import parsed_url
from .http import Unavailable, json_request, remaining

SOURCES = ('spamhaus_dbl', 'surbl', 'uribl', 'safe_browsing')


def decode(source, addresses):
    """Only documented positive codes count. An invalid answer never proves clean."""
    if addresses is None:
        return None, 'lookup_unavailable'
    if not addresses:
        return False, 'not_listed'
    positives = []
    for raw in addresses:
        try:
            address = ipaddress.IPv4Address(raw)
        except ipaddress.AddressValueError:
            return None, 'invalid_response'
        if source == 'spamhaus_dbl':
            if str(address).startswith('127.255.255.') or str(address) == '127.0.1.255':
                return None, 'lookup_refused'
            if str(address) not in {f'127.0.1.{n}' for n in (2, 4, 5, 6, 102, 103, 104, 105, 106)}:
                return None, 'invalid_response'
        else:
            if str(address) in ('127.0.0.1', '127.0.0.255'):
                return None, 'lookup_refused'
            last = int(str(address).split('.')[-1])
            mask = 14 if source == 'uribl' else 254
            if not str(address).startswith('127.0.0.') or not last & mask or last & ~mask:
                return None, 'invalid_response'
        positives.append(str(address))
    return bool(positives), 'listed'


def lookup(name):
    """Query names can include a DQS secret and must never be logged or retained."""
    import dns.exception
    import dns.resolver
    resolver = dns.resolver.Resolver()
    servers = policy.value('CRM_REPUTATION_NAMESERVERS')
    if servers:
        resolver.nameservers = list(servers)
    resolver.lifetime = resolver.timeout = remaining(policy.value('CRM_REPUTATION_DNS_SECONDS'))
    try:
        return [str(row) for row in resolver.resolve(name, 'A')]
    except dns.resolver.NXDOMAIN:
        return []
    except (dns.exception.DNSException, OSError):
        return None


def zone(source):
    if source == 'spamhaus_dbl':
        key = getattr(settings, 'SPAMHAUS_DQS_KEY', '')
        return key + '.dbl.dq.spamhaus.net' if re.fullmatch(r'[A-Za-z0-9]{26}', key) else None
    if source == 'surbl':
        return 'multi.surbl.org' if policy.value('CRM_SURBL_ENABLED') else None
    if source == 'uribl' and policy.value('CRM_URIBL_ENABLED') and policy.value('CRM_REPUTATION_NAMESERVERS'):
        return 'multi.uribl.com'
    return None


def test_path(source, *, now):
    suffix = zone(source)
    if not suffix:
        return False, 'not_configured'
    cache_key = 'crm:deliverability:blocklist-test:' + source + ':' + now.date().isoformat()
    result = cache.get(cache_key)
    if result:
        return result
    subject, expected = {'spamhaus_dbl': ('dbltest.com', '127.0.1.2'),
        'surbl': ('test.surbl.org', '127.0.0.126'), 'uribl': ('test.uribl.com', '127.0.0.14')}[source]
    addresses = lookup(subject + '.' + suffix)
    listed, reason = decode(source, addresses)
    # Both a positive test and a known negative are needed to detect blanket DNS filtering.
    negative, _ = decode(source, lookup('example.com.' + suffix)) if listed is True and expected in addresses else (None, '')
    result = (listed is True and expected in (addresses or []) and negative is False,
        'pass' if listed is True and expected in (addresses or []) and negative is False else 'test_path_unavailable')
    cache.set(cache_key, result, timeout=86400 if result[0] else 300)
    return result


def registrable(domain):
    import tldextract
    # Offline bundled PSL only, including hosted private suffixes. Never downloads a list at request time.
    result = tldextract.TLDExtract(cache_dir=None, suffix_list_urls=(), include_psl_private_domains=True)(domain)
    return result.top_domain_under_public_suffix or domain


def domain_check(subject, source, *, now, refresh=False):
    if not refresh:
        recent = ReputationCheck.objects.filter(subject=subject, source=source,
            checked_at__gte=now - timedelta(hours=policy.value('CRM_REPUTATION_MAX_AGE_HOURS'))).order_by('-checked_at').first()
        if recent:
            return recent
    working, reason = test_path(source, now=now)
    listed = None
    if working:
        domains = {subject, registrable(subject)}
        values = [decode(source, lookup(domain + '.' + zone(source))) for domain in domains]
        listed = True if any(value is True for value, _ in values) else None if any(value is None for value, _ in values) else False
        reason = 'listed' if listed is True else 'not_listed' if listed is False else 'lookup_unavailable'
    return ReputationCheck.objects.create(subject=subject, source=source, listed=listed,
        detail={'status': reason}, checked_at=now)


def urls_digest(urls):
    return hashlib.sha256('\n'.join(sorted(set(urls))).encode()).hexdigest()


def url_check(subject, urls, *, now, refresh=False):
    """A clean root URL cannot be reused to approve an unrelated path on that host."""
    digest = urls_digest(urls)
    if not refresh:
        recent = ReputationCheck.objects.filter(subject=subject, source='safe_browsing',
            detail__urls_hash=digest, checked_at__gte=now - timedelta(hours=policy.value('CRM_REPUTATION_MAX_AGE_HOURS'))).order_by('-checked_at').first()
        if recent:
            return recent
    web_risk = getattr(settings, 'WEB_RISK_API_KEY', '')
    safe = getattr(settings, 'SAFE_BROWSING_API_KEY', '') if policy.value('CRM_SAFE_BROWSING_NONCOMMERCIAL') else ''
    listed, status = None, 'not_configured'
    try:
        if web_risk:
            results = [json_request('GET', 'https://webrisk.googleapis.com/v1/uris:search',
                headers={'X-Goog-Api-Key': web_risk}, params={'uri': url, 'threatTypes': ['MALWARE', 'SOCIAL_ENGINEERING', 'UNWANTED_SOFTWARE']}) for url in urls]
            listed = any(bool(row.get('threat', {}).get('threatTypes')) for row in results)
        elif safe:
            data = json_request('POST', 'https://safebrowsing.googleapis.com/v4/threatMatches:find',
                headers={'X-Goog-Api-Key': safe}, json={'client': {'clientId': 'mlai-email', 'clientVersion': '1.0'},
                    'threatInfo': {'threatTypes': ['MALWARE', 'SOCIAL_ENGINEERING', 'UNWANTED_SOFTWARE'],
                        'platformTypes': ['ANY_PLATFORM'], 'threatEntryTypes': ['URL'], 'threatEntries': [{'url': url} for url in urls]}})
            matches = data.get('matches', [])
            if not isinstance(matches, list):
                raise Unavailable('invalid_response')
            listed = bool(matches)
        if listed is not None:
            status = 'listed' if listed else 'not_listed'
    except Unavailable:
        status = 'lookup_unavailable'
    return ReputationCheck.objects.create(subject=subject, source='safe_browsing', listed=listed,
        detail={'status': status, 'urls_hash': digest, 'urls_checked': len(urls), 'service': 'web_risk' if web_risk else 'safe_browsing' if safe else 'none'}, checked_at=now)


def check_subjects(subjects, *, now, urls=(), refresh=False):
    by_host = {subject: [] for subject in subjects}
    for url in urls:
        try:
            _, host = parsed_url(url)
            by_host.setdefault(host, []).append(url)
        except ValueError:
            continue
    if len(by_host) > policy.value('CRM_REPUTATION_MAX_SUBJECTS'):
        raise ValueError('Too many reputation subjects')
    results = []
    for subject, targets in sorted(by_host.items()):
        parsed_url('https://' + subject + '/')
        for source in SOURCES[:-1]:
            results.append(domain_check(subject, source, now=now, refresh=refresh))
        results.append(url_check(subject, targets or ['https://' + subject + '/'], now=now, refresh=refresh))
    return results


def check(stream, *, now):
    checks = check_subjects({stream.domain, 'mlai.au', 'lists.mlai.au'}, now=now)
    listed = sorted({row.subject for row in checks if row.listed is True})
    if listed:
        from ..guardian import Signal, detect
        detect(None, Signal('blocklist', 'red', len(listed), 1, len(checks), 'stop_stream',
            evidence_key=scopes.evidence_key([f'{row.subject}:{row.source}' for row in checks if row.listed is True])), stream=stream, now=now)
    return observations.record(stream, 'reputation', {'status': 'fail' if listed else 'unknown' if any(row.listed is None for row in checks) else 'pass',
        'checks': [{'subject': row.subject, 'source': row.source, 'listed': row.listed, 'status': row.detail.get('status')} for row in checks]}, now=now)
