"""Receiver placement requirements for recovery of elective campaigns."""

from collections import Counter
from datetime import timedelta

from django.utils.dateparse import parse_datetime

from crm import sending_domains

from .. import policy

INBOX = {'inbox', 'promotions', 'updates'}


def enforces(blast):
    """Recovery blocks marketing releases, including legacy email campaigns."""
    return blast.purpose == 'marketing' and policy.value('CRM_DELIVERABILITY_RECOVERY_MODE') == 'enforce'


def required_providers(blast):
    required = set(policy.value('CRM_RECOVERY_REQUIRED_INBOXES'))
    if (blast.plan or {}).get('providers', {}).get('yahoo'):
        required.add('yahoo')
    return required


def assess(blast, seed, now):
    if blast.purpose != 'marketing' or policy.value('CRM_DELIVERABILITY_RECOVERY_MODE') == 'off':
        return None
    rows = seed.get('results') or []
    required = required_providers(blast)
    expected_route = None
    if getattr(blast, 'stream_id', None) and any(entry.startswith(blast.stream.domain + ':') for entry in policy.value('CRM_RESEND_DNS_ROUTES')):
        expected_route = sending_domains.resend_dns_route(blast.stream.domain)
    healthy = {row['provider'] for row in rows if row.get('arrived_at') and row.get('provider')
        and row.get('folder') in INBOX and all(row.get(key) == 'pass' for key in ('spf', 'dkim', 'dmarc'))
        and row.get('list_unsubscribe') and row.get('list_unsubscribe_post') and row.get('one_click_signed')
        and (expected_route is None or row.get('return_path_domain') == expected_route[0]
            and expected_route[1] in row.get('dkim_selectors', []))}
    missing = sorted(required - healthy)
    try:
        sent = parse_datetime(seed.get('sent_at') or '')
    except (TypeError, ValueError):
        sent = None
    reason, detail = None, 'The required test inboxes received this version.'
    if not rows:
        reason, detail = 'inbox_test_unavailable', 'Inbox placement is unavailable. Configure and run the test inboxes.'
    elif any(row.get('folder') == 'spam' and row.get('provider') in required for row in rows):
        reason, detail = 'inbox_test_spam', 'A required test mailbox put this email in Spam. Review the message and rerun the inbox check.'
    elif seed.get('grade') == 'block':
        reason, detail = 'inbox_test_failed', 'The email team needs to fix authentication or unsubscribe before sending.'
    elif seed.get('status') != 'complete' or missing:
        reason, detail = 'inbox_test_incomplete', 'Wait for a result from each required test provider, or rerun the inbox check.'
    elif (not sent or sent.tzinfo is None or not now - timedelta(hours=policy.value('CRM_PREFLIGHT_MAX_AGE_HOURS')) < sent <= now):
        reason, detail = 'inbox_test_stale', 'This inbox check has expired. Run a new check before sending.'
    return {'enforcing': enforces(blast), 'reason': reason, 'detail': detail, 'missing_providers': missing,
        'source': 'controlled_mailboxes', 'checked_at': seed.get('sent_at'), 'sample': len(rows),
        'folders': dict(Counter(row.get('folder') or 'unknown' for row in rows))}
