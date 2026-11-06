"""Content-bound seed probes with at-most-once test submission and read-only polling."""

import hashlib
import copy
import json
import logging
import re
from datetime import timedelta

from django.core.cache import cache
from django.db import transaction
from django.utils import timezone

from crm import blast_email, listmonk, sending_domains
from crm.models import Blast, CrmEvent, DeliverabilityIncident, SeedMailbox, SeedTest, SenderMailbox, SendingStream

from .. import policy
from . import render, seed_inbox

logger = logging.getLogger(__name__)


def sync_mailboxes():
    """Apply an explicit operator configuration without ever reading its credentials."""
    raw = policy.value('CRM_SEED_MAILBOXES')
    specs = json.loads(raw) if isinstance(raw, str) and raw else raw or []
    if not isinstance(specs, list):
        raise ValueError('Invalid seed mailbox configuration')
    validated, addresses = [], set()
    for spec in specs:
        if not isinstance(spec, dict) or set(spec) - {'provider', 'address', 'imap_host', 'imap_port', 'auth', 'secret_env', 'enabled'}:
            raise ValueError('Invalid seed mailbox configuration')
        row = SeedMailbox(**spec)
        row.address = row.address.strip().lower()
        if (not re.fullmatch(r'CRM_SEED_[A-Z0-9_]+', row.secret_env) or row.address in addresses
                or not 0 < row.imap_port <= 65535 or type(row.enabled) is not bool):
            raise ValueError('Invalid seed mailbox configuration')
        row.full_clean(validate_unique=False)
        addresses.add(row.address)
        validated.append(row)
    with transaction.atomic():
        for row in validated:
            SeedMailbox.objects.update_or_create(address=row.address, defaults={key: getattr(row, key)
                for key in ('provider', 'imap_host', 'imap_port', 'auth', 'secret_env', 'enabled')})
    return len(validated)


def get_or_start(blast, digest, *, now, force=False, template=None):
    """Reuse the exact message and enrolled inbox set for at most 24 hours."""
    if not policy.value('CRM_PREFLIGHT_ENABLED'):
        from crm.blasts import BlastError
        raise BlastError('preflight_disabled', 404)
    with transaction.atomic():
        stream = SendingStream.objects.select_for_update().get(pk=blast.stream_id)
        current = Blast.objects.select_for_update(of=('self',)).select_related('event', 'mailbox').get(pk=blast.pk)
        current.stream = stream
        if render.content_hash(current, template if template is not None else render.template_body()) != digest:
            from crm.blasts import BlastError
            raise BlastError('draft_changed', 409)
        enabled = list(SeedMailbox.objects.filter(enabled=True).order_by('pk'))
        identities = {str(row.pk): {'provider': row.provider, 'folder': 'missing'} for row in enabled}
        recent = SeedTest.objects.filter(stream=stream, blast=blast, content_hash=digest,
            created_at__gte=now - timedelta(hours=policy.value('CRM_PREFLIGHT_MAX_AGE_HOURS'))).order_by('-created_at').first()
        if not force and recent and set((recent.results or {}).get('mailboxes') or {}) == set(identities):
            probe = recent
        else:
            message = {key: getattr(current, key) for key in ('subject', 'body_markdown', 'preview_text', 'sign_off_name')}
            message['event'] = render.event_context(current)
            message['identity'] = {'email': current.mailbox.email, 'reply_to': current.mailbox.reply_to, 'domain': stream.domain}
            probe = SeedTest.objects.create(stream=stream, blast=blast, content_hash=digest,
                status='sending' if enabled else 'timeout', results={'mailboxes': identities, 'message': message})
    if probe.status == 'sending':
        submit(probe.pk, now=now)
        probe.refresh_from_db()
    return probe


def _seed_subscriber(mailbox):
    existing = blast_email._find_one(mailbox.address)
    if existing is None:
        try:
            listmonk.create_subscriber(mailbox.address, 'MLAI inbox check', lists=[], attribs={
                'consent_reason': 'This is an inbox check for MLAI, requested for this test mailbox.'})
        except listmonk.ListmonkError as exc:
            if not exc.conflict:
                raise
        return
    # Preserve existing consent wording, status, name and memberships. A probe
    # never enables a blocklisted subscriber or changes the canonical contact.
    attributes = existing.get('attribs') if isinstance(existing.get('attribs'), dict) else {}
    if not attributes.get('consent_reason'):
        listmonk.update_subscriber(int(existing['id']), {'email': existing['email'], 'name': existing.get('name') or 'MLAI inbox check',
            'status': existing.get('status') or 'enabled', 'attribs': {**attributes,
                'consent_reason': 'This is an inbox check for MLAI, requested for this test mailbox.'},
            'lists': [row['id'] for row in existing.get('lists') or () if isinstance(row, dict) and isinstance(row.get('id'), int)]})


def submit(probe_id, *, now):
    """Commit the attempt before the non-idempotent test API; uncertain attempts aren't repeated."""
    if not (policy.value('CRM_PREFLIGHT_ENABLED') or policy.value('CRM_SEED_HEARTBEAT_ENABLED')):
        return
    with transaction.atomic():
        probe = SeedTest.objects.select_for_update(of=('self',)).select_related('blast__event', 'blast__mailbox', 'stream__mailbox').get(pk=probe_id)
        submission = (probe.results or {}).get('submission') or {}
        if probe.status != 'sending' or submission.get('attempted_at'):
            return
        heartbeat = not probe.blast_id and (probe.results or {}).get('heartbeat') is True
        if not policy.value('CRM_SEED_HEARTBEAT_ENABLED' if heartbeat else 'CRM_PREFLIGHT_ENABLED'):
            return
        if (not probe.blast_id and not heartbeat) or probe.stream.transactional or not sending_domains.is_verified(probe.stream.domain):
            return
        snapshot = probe.results.get('message') or {}
        identity = snapshot.get('identity') or {}
        source = probe.blast if probe.blast_id else Blast(pk=probe.token, stream=probe.stream,
            mailbox=probe.stream.mailbox, purpose='marketing', channels=['email'])
        if (not source.mailbox_id or identity.get('email') != source.mailbox.email
                or identity.get('domain') != probe.stream.domain):
            probe.status = 'error'; probe.save(update_fields=['status', 'updated_at'])
            return
        blast = copy.copy(source)
        for key in ('subject', 'body_markdown', 'preview_text', 'sign_off_name'):
            setattr(blast, key, snapshot.get(key, ''))
        event = snapshot.get('event') or {}
        blast.event = CrmEvent(pk=event['pk'], name=event['name'], url=event['url'],
            start_at=blast_email.parse_iso(event['start_at']), timezone=event['timezone']) if event.get('pk') else None
        blast.stream = probe.stream
        blast.mailbox = SenderMailbox(email=identity['email'], reply_to=identity['reply_to'], domain=identity['domain'], display_name='MLAI')
        mailboxes = list(SeedMailbox.objects.filter(pk__in=probe.results['mailboxes'], enabled=True).order_by('pk'))
        if not mailboxes:
            probe.status = 'timeout'; probe.save(update_fields=['status', 'updated_at'])
            return
        name = f'crm-seed-{probe.token}'
        list_id = submission.get('list_id') or listmonk.find_list(name) or listmonk.create_list(name, tags=['crm', 'seed'])
        payload = render.preview_payload(blast, list_id, name)
        payload['headers'].append({'X-MLAI-Seed': str(probe.token)})
        campaign = listmonk.get_campaign(submission['campaign_id']) if submission.get('campaign_id') else listmonk.find_campaign(name)
        if campaign is None:
            campaign = listmonk.create_campaign(payload)
        if campaign.get('status') != 'draft':
            # The seed list is intentionally empty; never restart a resource
            # whose provider state no longer matches a test-only draft.
            probe.status = 'error'; probe.save(update_fields=['status', 'updated_at'])
            return
        campaign_id = int(campaign['id'])
        for mailbox in mailboxes:
            _seed_subscriber(mailbox)
        probe.results = {**probe.results, 'submission': {'list_id': list_id, 'campaign_id': campaign_id,
            'attempted_at': now.isoformat(), 'state': 'request_started'}}
        probe.status, probe.sent_at = 'waiting', now
        probe.save(update_fields=['results', 'status', 'sent_at', 'updated_at'])
        emails = [row.address for row in mailboxes]
    # A partial test send can return 500 after some recipients received it.
    # Once an attempt commits, every retry polls inboxes instead of resending.
    state, reason = 'submitted', ''
    try:
        listmonk.test_campaign(campaign_id, payload, emails)
    except listmonk.ListmonkError as exc:
        state, reason = 'uncertain', exc.code
    with transaction.atomic():
        current = SeedTest.objects.select_for_update().get(pk=probe_id)
        result = dict(current.results or {})
        result['submission'] = {**result.get('submission', {}), 'state': state, 'reason': reason}
        current.results = result
        current.save(update_fields=['results', 'updated_at'])


def probe_result(probe):
    results = list(((probe.results or {}).get('mailboxes') or {}).values())
    status, reason = seed_inbox.grade(results, timed_out=probe.status in ('timeout', 'error'), marketing=not probe.stream.transactional)
    return {'id': str(probe.pk), 'status': probe.status, 'results': results,
        'grade': status, 'reason': reason, 'timed_out': probe.status in ('timeout', 'error')}


def timeout_incident(probe, now):
    """Record the canary-only reduction without slowing or stopping the whole stream."""
    if probe.status not in ('timeout', 'error') or not probe.blast_id:
        return
    fingerprint = hashlib.sha256(f'seed_timeout:{probe.blast_id}:{probe.content_hash}'.encode()).hexdigest()
    with transaction.atomic():
        SendingStream.objects.select_for_update().get(pk=probe.stream_id)
        if DeliverabilityIncident.objects.filter(fingerprint=fingerprint).exists():
            return
        DeliverabilityIncident.objects.create(stream_id=probe.stream_id, blast_id=probe.blast_id,
            signal='seed_timeout', severity='amber', value=policy.value('CRM_SEED_TIMEOUT_MINUTES'),
            threshold=policy.value('CRM_SEED_TIMEOUT_MINUTES'), sample=len((probe.results or {}).get('mailboxes') or {}),
            action='reduce_canary', fingerprint=fingerprint, opened_at=now, last_detected_at=now, action_applied_at=now)


def poll(probe_id, *, now):
    probe = SeedTest.objects.select_related('stream__mailbox').get(pk=probe_id)
    if probe.status not in ('sending', 'waiting'):
        return probe
    started = probe.sent_at or probe.created_at
    expired = now >= started + timedelta(minutes=policy.value('CRM_SEED_TIMEOUT_MINUTES'))
    if probe.status == 'sending' and not expired:
        submit(probe.pk, now=now)
        probe.refresh_from_db()
    observed = dict((probe.results or {}).get('mailboxes') or {})
    for mailbox in SeedMailbox.objects.filter(pk__in=observed, enabled=True).order_by('pk'):
        if not observed[str(mailbox.pk)].get('arrived_at'):
            observed[str(mailbox.pk)] = seed_inbox.read_mailbox(mailbox, probe, now=now)
    complete = bool(observed) and all(row.get('arrived_at') for row in observed.values())
    with transaction.atomic():
        current = SeedTest.objects.select_for_update().get(pk=probe.pk)
        if current.status not in ('sending', 'waiting'):
            return current
        merged = dict((current.results or {}).get('mailboxes') or {})
        for identity, result in observed.items():
            if not merged.get(identity, {}).get('arrived_at'):
                merged[identity] = result
        current.results = {**current.results, 'mailboxes': merged}
        current.status = 'complete' if complete else 'timeout' if expired else current.status
        current.save(update_fields=['results', 'status', 'updated_at'])
    timeout_incident(current, now)
    return current


def run_seed_checks(*, now=None):
    """Never raises into the minute scheduler and performs no mailbox mutations."""
    if not (policy.value('CRM_PREFLIGHT_ENABLED') or policy.value('CRM_SEED_HEARTBEAT_ENABLED')):
        return {'status': 'disabled'}
    now = now or timezone.now()
    try:
        if not cache.add('crm:preflight:seeds', 1, timeout=policy.value('CRM_SEED_POLL_SECONDS')):
            return {'status': 'throttled'}
        checked = 0
        for probe in SeedTest.objects.filter(status__in=('sending', 'waiting')).order_by('created_at'):
            current = poll(probe.pk, now=now)
            if current.blast_id:
                from .service import refresh_seed_result
                refresh_seed_result(current, now=now)
            if (current.results or {}).get('heartbeat'):
                from ..monitors.heartbeat import assess
                assess(current, now=now)
            checked += 1
        return {'status': 'completed', 'checked': checked}
    except Exception as exc:
        logger.error('CRM seed checks deferred: %s', type(exc).__name__)
        return {'status': 'deferred'}
