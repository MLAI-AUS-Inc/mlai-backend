"""Durable owner-only notify alerts with retries inside Resend's 24-hour receipt window."""

import hashlib
from datetime import timedelta

from django.conf import settings
from django.core.validators import validate_email
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils.dateparse import parse_datetime

from crm import blast_email, sending_domains
from crm.models import CrmAuditEvent, SendingStream

from . import policy
from .streams import protected_domain, stream_configured
from .monitors.http import Unavailable, json_request


def send(subject, body, *, key, now):
    """No campaign audience, no marketing admission and no fallback to a marketing API key."""
    if not policy.value('CRM_DELIVERABILITY_EMAIL_ALERTS_ENABLED') or not getattr(settings, 'CRM_NOTIFY_API_KEY', ''):
        return False
    owners = list(dict.fromkeys(str(value).strip().lower() for value in policy.value('CRM_DELIVERABILITY_OWNERS')))
    try:
        for address in owners:
            validate_email(address)
        if not owners or len(owners) > 20:
            return False
    except ValidationError:
        return False
    stream = SendingStream.objects.select_related('mailbox').filter(key='notify', transactional=True, mailbox__enabled=True).first()
    if not stream or not stream_configured(stream) or protected_domain(stream.domain):
        return False
    if sending_domains.domain_health(stream.domain, stream.mailbox.route).get('status') != 'verified':
        return False
    success = True
    for owner in owners:
        identity = hashlib.sha256((key + ':' + owner).encode()).hexdigest()
        with transaction.atomic():
            SendingStream.objects.select_for_update().get(pk=stream.pk)
            row = CrmAuditEvent.objects.select_for_update().filter(target_type='deliverability_alert_email', target_id=identity).order_by('-created_at', '-pk').first()
            if row is None:
                row = CrmAuditEvent.objects.create(action='send', target_type='deliverability_alert_email', target_id=identity,
                    detail={'state': 'pending', 'first_attempt_at': now.isoformat(), 'attempts': 0,
                        'subject': subject[:200], 'body': body[:20000], 'credential_hash': hashlib.sha256(settings.CRM_NOTIFY_API_KEY.encode()).hexdigest(), 'from': blast_email.from_address('MLAI', stream.mailbox.email)})
            state = dict(row.detail)
            if state.get('state') == 'sent':
                continue
            first = parse_datetime(state['first_attempt_at'])
            next_try = parse_datetime(state.get('next_attempt_at', ''))
            if now >= first + timedelta(hours=23) or state.get('credential_hash') != hashlib.sha256(settings.CRM_NOTIFY_API_KEY.encode()).hexdigest():
                CrmAuditEvent.objects.create(action='sync', target_type='deliverability_alert_email', target_id=identity, detail={**state, 'state': 'needs_review'})
                success = False; continue
            if next_try and now < next_try:
                success = False; continue
            attempts = state.get('attempts', 0) + 1
            state.update(state='attempting', attempts=attempts,
                next_attempt_at=(now + timedelta(seconds=min(3600, 60 * 2 ** min(attempts, 6)))).isoformat())
            CrmAuditEvent.objects.create(action='sync', target_type='deliverability_alert_email', target_id=identity, detail=state)
            payload = {'from': state['from'], 'to': [owner], 'subject': state['subject'], 'text': state['body']}
        outcome = 'uncertain'
        try:
            result = json_request('POST', 'https://api.resend.com/emails', json=payload,
                headers={'Authorization': 'Bearer ' + settings.CRM_NOTIFY_API_KEY, 'Idempotency-Key': 'crm-owner-' + identity})
            if isinstance(result.get('id'), str) and result['id']:
                outcome = 'sent'
        except Unavailable:
            pass
        with transaction.atomic():
            SendingStream.objects.select_for_update().get(pk=stream.pk)
            current = CrmAuditEvent.objects.filter(target_type='deliverability_alert_email', target_id=identity).order_by('-created_at', '-pk').first()
            if current.detail.get('state') != 'sent' and (outcome == 'sent' or current.detail.get('attempts') == attempts):
                CrmAuditEvent.objects.create(action='sync', target_type='deliverability_alert_email', target_id=identity,
                    detail={**current.detail, 'state': outcome})
        success &= outcome == 'sent'
    return success
