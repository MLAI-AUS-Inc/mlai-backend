"""Daily seed-only probes. No CRM audience is selected or enrolled by this job."""

import hashlib

from django.conf import settings
from django.db import transaction

from crm import blast_email, sending_domains
from crm.models import SeedMailbox, SeedTest, SendingStream

from .. import observations, policy, scopes, volume
from ..preflight import seeds
from .http import Unavailable, json_request

MESSAGE = {'subject': 'A quick hello from the MLAI community',
    'body_markdown': 'Hello from MLAI. This daily message checks that community updates reach the inboxes enrolled by our email team. '
        'There is no news or action needed today. Thanks for helping us keep community email useful, reliable and easy to read.',
    'preview_text': 'A daily inbox check for the MLAI email team.', 'sign_off_name': 'MLAI team', 'event': {}}


def start(stream, *, now, actor=None):
    if not policy.value('CRM_SEED_HEARTBEAT_ENABLED'):
        return {'status': 'disabled'}
    if not stream.mailbox.enabled or sending_domains.domain_health(stream.domain, stream.mailbox.route).get('status') != 'verified':
        return {'status': 'waiting_for_identity'}
    if stream.transactional and not getattr(settings, 'CRM_NOTIFY_API_KEY', ''):
        return {'status': 'not_configured'}
    with transaction.atomic():
        current = SendingStream.objects.select_for_update().get(pk=stream.pk)
        today = now.astimezone(volume.MELBOURNE).date().isoformat()
        # Durable content identity serializes schedulers and repeated manual requests for this day.
        digest = hashlib.sha256(f'heartbeat:{stream.pk}:{today}'.encode()).hexdigest()
        probe = SeedTest.objects.filter(stream=stream, blast__isnull=True, content_hash=digest).first()
        if not probe:
            enrolled = list(SeedMailbox.objects.filter(enabled=True).order_by('pk'))
            if not enrolled:
                return {'status': 'not_configured'}
            message = {**MESSAGE, 'identity': {'email': current.mailbox.email,
                'reply_to': current.mailbox.reply_to, 'domain': current.domain}}
            probe = SeedTest.objects.create(stream=current, content_hash=digest, results={'heartbeat': True,
                'day': today, 'message': message, 'mailboxes': {str(row.pk): {'provider': row.provider, 'folder': 'missing'} for row in enrolled}})
    if stream.transactional:
        submit_notify(probe.pk, now=now)
    else:
        seeds.submit(probe.pk, now=now)
    probe.refresh_from_db()
    observations.record(stream, 'heartbeat', {'status': probe.status, 'probe_id': str(probe.pk), 'day': today}, now=now)
    return {'status': probe.status, 'id': str(probe.pk)}


def submit_notify(probe_id, *, now):
    """Commit each attempt before network I/O. Uncertain sends only poll, never resubmit."""
    if not policy.value('CRM_SEED_HEARTBEAT_ENABLED') or not getattr(settings, 'CRM_NOTIFY_API_KEY', ''):
        return
    for mailbox_id in SeedTest.objects.get(pk=probe_id).results.get('mailboxes', {}):
        with transaction.atomic():
            probe = SeedTest.objects.select_for_update().select_related('stream__mailbox').get(pk=probe_id)
            mailbox = SeedMailbox.objects.filter(pk=mailbox_id, enabled=True).first()
            attempts = dict(probe.results.get('attempts') or {})
            if not mailbox or mailbox_id in attempts or not probe.stream.transactional or not probe.results.get('heartbeat'):
                continue
            attempts[mailbox_id] = {'attempted_at': now.isoformat(), 'state': 'request_started'}
            probe.results = {**probe.results, 'attempts': attempts}
            probe.status, probe.sent_at = 'waiting', probe.sent_at or now
            probe.save(update_fields=['results', 'status', 'sent_at', 'updated_at'])
            payload = {'from': blast_email.from_address('MLAI', probe.stream.mailbox.email), 'to': [mailbox.address],
                'subject': MESSAGE['subject'], 'text': MESSAGE['body_markdown'] + '\n\nMLAI team',
                'headers': {'X-MLAI-Seed': str(probe.token)}}
            if probe.stream.mailbox.reply_to:
                payload['reply_to'] = probe.stream.mailbox.reply_to
        state = 'submitted'
        try:
            json_request('POST', 'https://api.resend.com/emails', json=payload, headers={
                'Authorization': 'Bearer ' + settings.CRM_NOTIFY_API_KEY,
                'Idempotency-Key': f'crm-seed-{probe.token}-{mailbox_id}'})
        except Unavailable:
            state = 'uncertain'
        with transaction.atomic():
            current = SeedTest.objects.select_for_update().get(pk=probe_id)
            attempts = current.results['attempts']
            attempts[mailbox_id] = {**attempts[mailbox_id], 'state': state}
            current.results = {**current.results, 'attempts': attempts}
            current.save(update_fields=['results', 'updated_at'])


def assess(probe, *, now):
    if not (probe.results or {}).get('heartbeat'):
        return
    rows = list(probe.results.get('mailboxes', {}).values())
    observed = [row for row in rows if row.get('arrived_at')]
    bad_auth = any(any(row.get(key) != 'pass' for key in ('spf', 'dkim', 'dmarc')) for row in observed)
    providers = {'google' if row.get('provider') in ('gmail', 'google_workspace') else 'microsoft'
        if row.get('provider') in ('outlook', 'microsoft365') else row.get('provider') for row in observed if row.get('folder') == 'spam'}
    from ..guardian import Signal, detect
    if bad_auth or (probe.status == 'complete' and len(providers) >= policy.value('CRM_SEED_SPAM_BLOCK_PROVIDERS')):
        detect(None, Signal('seed_spam', 'red' if bad_auth else 'amber', len(providers) if not bad_auth else 1, 1,
            len(observed), 'stop_stream' if bad_auth else 'hold_stream',
            evidence_key=scopes.evidence_key([probe.pk, bad_auth, *sorted(providers)])), stream=probe.stream, now=now)
    grade = seeds.probe_result(probe)
    observations.record(probe.stream, 'heartbeat', {'status': probe.status, 'probe_id': str(probe.pk),
        'grade': grade['grade'], 'reason': grade['reason'],
        'results': [{key: row.get(key) for key in ('provider', 'folder', 'spf', 'dkim', 'dmarc', 'arrived_at')} for row in rows]}, now=now)
