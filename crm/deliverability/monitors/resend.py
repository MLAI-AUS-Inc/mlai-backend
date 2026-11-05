"""GET-only Resend inspection. No sending-key fallback and no suppression removal."""

import re
from urllib.parse import quote

from django.conf import settings
from django.db import transaction

from crm.models import Contact, ContactConsentEvent, CrmAuditEvent

from .. import policy
from .http import Unavailable, json_request

EVENTS = {'email.sent', 'email.delivered', 'email.delivery_delayed', 'email.bounced',
    'email.complained', 'email.failed', 'email.suppressed'}


def get(path, *, params=None):
    key = getattr(settings, 'RESEND_READ_API_KEY', '')
    if not key:
        raise Unavailable('not_configured')
    if not re.fullmatch(r'/(?:domains(?:/[a-fA-F0-9-]{36})?|webhooks|suppressions)', path):
        raise Unavailable('invalid_path')
    return json_request('GET', 'https://api.resend.com' + path,
        headers={'Authorization': 'Bearer ' + key}, params=params or {})


def listing(path):
    result, seen, params = [], set(), {'limit': 100}
    for _ in range(policy.value('CRM_MONITOR_MAX_PAGES')):
        data = get(path, params=params)
        rows = data.get('data')
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise Unavailable('invalid_response')
        result += rows
        if not data.get('has_more'):
            return result
        cursor = rows[-1].get('id') if rows else None
        if not isinstance(cursor, str) or cursor in seen or not re.fullmatch(r'[a-fA-F0-9-]{36}', cursor):
            raise Unavailable('pagination_incomplete')
        seen.add(cursor); params['after'] = cursor
    raise Unavailable('pagination_incomplete')


def contract(domain, webhooks, *, expected_domain):
    critical, warnings = [], []
    if domain.get('name') != expected_domain or domain.get('status') != 'verified':
        critical.append('resend_domain_unverified')
    if domain.get('open_tracking') is not False or domain.get('click_tracking') is not False:
        critical.append('resend_tracking')
    expected = policy.value('CRM_LISTMONK_PUBLIC_URL').rstrip('/') + '/webhooks/resend'
    valid = [row for row in webhooks if row.get('endpoint', '').rstrip('/') == expected
        and row.get('status') == 'enabled' and EVENTS <= set(row.get('events') or [])]
    if not valid:
        critical.append('resend_webhook')
    return {'status': 'fail' if critical else 'pass', 'critical': critical, 'warnings': warnings}


def check(stream):
    if not getattr(settings, 'RESEND_READ_API_KEY', ''):
        return {'status': 'not_configured', 'critical': [], 'warnings': ['resend_not_configured']}
    domains = [row for row in listing('/domains') if row.get('name') == stream.domain]
    if len(domains) != 1 or not re.fullmatch(r'[a-fA-F0-9-]{36}', str(domains[0].get('id', ''))):
        return {'status': 'fail', 'critical': ['resend_domain_unverified'], 'warnings': []}
    domain = get('/domains/' + quote(domains[0]['id'], safe=''))
    return contract(domain, listing('/webhooks'), expected_domain=stream.domain)


def reconcile_suppressions(*, now):
    """Confirmed bounce/complaint suppression wins; manual suppression creates a review hold."""
    if not getattr(settings, 'RESEND_READ_API_KEY', ''):
        return {'status': 'not_configured', 'updated': 0}
    records = listing('/suppressions')  # A truncated response is never treated as a complete reconciliation.
    updated = apply_suppressions(records, now=now)
    CrmAuditEvent.objects.create(action='sync', target_type='deliverability_reconciliation',
        detail={'operation': 'resend_suppressions', 'updated': updated, 'checked_at': now.isoformat()})
    return {'status': 'completed', 'updated': updated}


def apply_suppressions(records, *, now):
    """Apply strong negative evidence only; bounce status does not withdraw permission."""
    updated = 0
    for record in records:
        email, origin = record.get('email'), record.get('origin')
        if not isinstance(email, str) or origin not in ('bounce', 'complaint', 'manual'):
            continue
        with transaction.atomic():
            for contact in Contact.objects.select_for_update().filter(email__iexact=email.strip()):
                if origin == 'manual':
                    attrs = dict(contact.attributes or {})
                    if attrs.get('marketing_review_hold'):
                        continue
                    attrs['marketing_review_hold'] = {'reason': 'provider_manual_suppression', 'at': now.isoformat()}
                    contact.attributes = attrs
                    contact.save(update_fields=['attributes', 'updated_at'])
                else:
                    from crm.listmonk_sync import record_email_opt_out
                    if not record_email_opt_out(contact.email, 'complained' if origin == 'complaint' else 'bounced',
                            source='resend:suppression_reconciliation', now=now):
                        continue
                updated += 1
    return updated



def suppression_page(stream, *, now):
    """One durable cursor page per pass so a large list cannot stall the scheduler."""
    from .. import observations, volume
    if not getattr(settings, 'RESEND_READ_API_KEY', ''):
        return observations.record(stream, 'suppressions', {'status': 'not_configured'}, now=now)
    prior = observations.latest(stream, 'suppressions')
    today = now.astimezone(volume.MELBOURNE).date().isoformat()
    continuing = prior.get('status') == 'partial'
    params = {'limit': 100}
    if continuing and prior.get('cursor'):
        params['after'] = prior['cursor']
    data = get('/suppressions', params=params)
    records = data.get('data')
    if not isinstance(records, list) or any(not isinstance(row, dict) for row in records):
        raise Unavailable('invalid_response')
    cursor = records[-1].get('id') if records else None
    if data.get('has_more') and (not isinstance(cursor, str) or cursor == prior.get('cursor')
            or not re.fullmatch(r'[a-fA-F0-9-]{36}', cursor)):
        raise Unavailable('pagination_incomplete')
    updated = apply_suppressions(records, now=now)
    return observations.record(stream, 'suppressions', {'status': 'partial' if data.get('has_more') else 'complete',
        'day': prior.get('day', today) if continuing else today, 'cursor': cursor if data.get('has_more') else None,
        'updated': (prior.get('updated', 0) if continuing else 0) + updated,
        'pages': (prior.get('pages', 0) if continuing else 0) + 1}, now=now)
