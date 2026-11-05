"""Provider-rendered preview, content identity and bounded MIME quality scoring."""

import hashlib
import json
import math
from email.message import EmailMessage
from email.utils import format_datetime

from django.core.cache import cache

from crm import blast_email, listmonk, merge_tags
from crm.models import BlastWave
from integrations import http_client

from .. import policy, waves
from .content import plain_text


def event_context(blast):
    event = blast.event if blast.event_id else None
    return {key: str(getattr(event, key, '') or '') for key in ('pk', 'name', 'url', 'start_at', 'timezone')}


def content_hash(blast, template_body):
    """Bind event substitutions and template bytes as well as the requested fields."""
    data = {key: getattr(blast, key) for key in ('subject', 'preview_text', 'body_markdown', 'sign_off_name')}
    data.update(stream=blast.stream.key, domain=blast.stream.domain, from_address=blast.mailbox.email,
        reply_to=blast.mailbox.reply_to, template_id=listmonk.template_id(),
        template_hash=hashlib.sha256(template_body.encode()).hexdigest(), event=event_context(blast))
    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def template_body(*, refresh=False):
    origin = listmonk._setting('LISTMONK_BASE_URL')
    template_id = listmonk.template_id()
    key = 'crm:preflight:template:' + hashlib.sha256(f'{origin}:{template_id}'.encode()).hexdigest()
    cached = cache.get(key) if not refresh else None
    if cached is not None:
        return cached
    if template_id:
        record = listmonk._request('GET', f'/api/templates/{template_id}')
    else:
        records = listmonk._request('GET', '/api/templates')
        record = next((row for row in records if isinstance(row, dict) and row.get('is_default') and row.get('type') == 'campaign'), None) if isinstance(records, list) else None
    if not isinstance(record, dict) or not isinstance(record.get('body'), str):
        raise listmonk.ListmonkError('template_unavailable')
    cache.set(key, record['body'], timeout=policy.value('CRM_TEMPLATE_CACHE_SECONDS'))
    return record['body']


def preview_payload(blast, list_id, name):
    wave = BlastWave(blast=blast, index=0, listmonk_list_id=list_id)
    payload = waves.campaign_payload(wave, blast)
    payload['name'] = name
    return payload


def preview(blast, digest):
    """A private empty preview list never loads the intended audience or starts a send."""
    name = f'crm-blast-{blast.pk}-preflight-{digest}'
    list_id = listmonk.find_list(name) or listmonk.create_list(name, tags=['crm', 'preview'])
    payload = preview_payload(blast, list_id, name)
    campaign = listmonk.find_campaign(name)
    if campaign:
        if campaign.get('status') != 'draft':
            raise listmonk.ListmonkError('preview_resource_not_draft')
        campaign_id = int(campaign['id'])
        listmonk.update_campaign(campaign_id, payload)
    else:
        campaign_id = int(listmonk.create_campaign(payload)['id'])
    return listmonk.preview_campaign(campaign_id)


def mime_message(blast, html, now):
    message = EmailMessage()
    message['From'] = blast_email.from_address('MLAI', blast.mailbox.email)
    message['To'] = 'Inbox check <preview@example.invalid>'
    message['Subject'] = merge_tags.render(blast.subject, None, blast.event)
    message['Date'] = format_datetime(now)
    message['Message-ID'] = f'<preflight-{blast.pk}@{blast.stream.domain}>'
    if blast.mailbox.reply_to:
        message['Reply-To'] = blast.mailbox.reply_to
    message['List-Unsubscribe'] = '<https://lists.mlai.au/subscription/preview>'
    message['List-Unsubscribe-Post'] = 'List-Unsubscribe=One-Click'
    plain = merge_tags.render(blast.body_markdown, None, blast.event)
    if blast.sign_off_name:
        plain += '\n\n— ' + blast.sign_off_name
    message.set_content(plain_text(plain) + '\n\nThis is an inbox check for MLAI.\n')
    message.add_alternative(html, subtype='html')
    return message.as_bytes()


def spam_score(blast, html, digest, now):
    key = 'crm:preflight:spam:' + digest
    cached = cache.get(key)
    if cached is not None:
        return cached
    origin = policy.value('RSPAMD_URL').rstrip('/')
    if not origin or html is None:
        return {'status': 'unavailable', 'reason': 'not_configured' if not origin else 'preview_unavailable'}
    try:
        response = http_client.request('POST', origin + '/checkv2', data=mime_message(blast, html, now),
            timeout=policy.value('CRM_SPAMCHECK_TIMEOUT_SECONDS'),
            headers={'Content-Type': 'message/rfc822', 'Settings-ID': 'crm_preflight', 'Flags': 'no_log'})
        if response.status_code != 200:
            return {'status': 'unavailable', 'reason': f'http_{response.status_code}'}
        data = response.json()
        score = data.get('score')
        if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score) or data.get('is_skipped'):
            return {'status': 'unavailable', 'reason': 'invalid_score'}
        symbols = data.get('symbols') or {}
        positive = [(name, row['score']) for name, row in symbols.items() if isinstance(row, dict)
            and isinstance(row.get('score'), (int, float)) and math.isfinite(row['score']) and row['score'] > 0]
        result = {'status': 'complete', 'score': float(score),
            'top_symbols': [name for name, _ in sorted(positive, key=lambda row: (-row[1], row[0]))[:3]]}
        cache.set(key, result, timeout=policy.value('CRM_TEMPLATE_CACHE_SECONDS'))
        return result
    except (http_client.RequestException, ValueError, TypeError, AttributeError):
        return {'status': 'unavailable', 'reason': 'check_unavailable'}
