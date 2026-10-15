"""Coalesced, consent-scoped observations with fences captured before source I/O."""
import hashlib
import logging
import re
import time
import uuid
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.core.cache import cache
from django.db import transaction

from . import inbox_exports as exports

KEY = 'message_sync_inbox_observations_v1'
REVISION_KEY = 'message_sync_inbox_source_revision_v1'
MAX_AGE = 90  # Leave transport time within the relay's 120-second window.
FORBIDDEN_COOLDOWN = 1800  # Refused authority needs time to settle.
INVALID_COOLDOWN = 900  # Invalid rooms must not poison later batches.
MAX_BACKOFF = 300  # Bound repeated transient request failures.
REPROBE_INTERVAL = 300  # Unknown accounts get at most one source probe per room.
logger = logging.getLogger(__name__)


def _normalize_result(raw) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    accepted = raw.get('accepted') is True
    code = raw.get('code') if isinstance(raw.get('code'), str) else 'ok' if accepted else 'unknown'
    return {'accepted': accepted, 'message': str(raw.get('message') or '')[:200], 'code': code,
            'status': raw.get('status') if type(raw.get('status')) is int else None,
            'retryable': raw.get('retryable') if type(raw.get('retryable')) is bool else not accepted and code not in {'invalid', 'forbidden'}}


def _clear_pending(row):
    for key in ('pending', 'pending_raw_ts', 'due'):
        row.pop(key, None)


def _backoff(row, now):
    attempts = min(int(row.get('attempts') or 0) + 1, 32767)
    row['attempts'] = attempts
    row['due'] = now + min(30 * 2 ** min(attempts - 1, 10), MAX_BACKOFF)


def _reprobe(connection, row, now):
    if now >= row.get('reprobe_after', 0):
        schedule_locked(connection, row['source_id'])
        row['reprobe_after'] = now + REPROBE_INTERVAL


def enabled():
    return getattr(settings, 'MESSAGE_SYNC_INBOX_CURSOR_PUSH', False)


def timestamp(value):
    """Canonical exact Slack ordering, including Slack's literal zero cursor."""
    if not isinstance(value, str) or not re.fullmatch(r'[0-9]{1,12}(?:\.[0-9]{1,6})?', value):
        return None
    try:
        stamp = Decimal(value)
        return format(stamp, '.6f') if 0 <= stamp * 1_000_000 <= exports.I64_MAX else None
    except InvalidOperation:
        return None


def community_id():
    """Cache capability metadata by adapter identity, never cache credentials."""
    from community_chat import adapter
    scope = hashlib.sha256((str(settings.COMMUNITY_CHAT_ADAPTER_URL) + ':' + str(settings.COMMUNITY_CHAT_ADAPTER_TOKEN)).encode()).hexdigest()
    key = 'inbox-community-v1:' + scope
    value = cache.get(key)
    if value is None:
        capabilities, _ = adapter._request('GET', '/v1/capabilities')
        value = str(uuid.UUID(str(capabilities.get('community_id'))))
        cache.set(key, value, timeout=30)
    return value


def capture(authority, target):
    """Capture settled exports before conversations.info; never after its reply."""
    if not enabled() or not target.has_relay_room:
        return None
    from community_chat.inbox_accounts import account_key
    from integrations.services import slack_chat_read_state as reads
    from integrations.services.community_bridge.buzz import BuzzBridgeClient
    community = community_id()
    account = account_key(community, authority.user_id)
    key = exports.room_key(community, target.channel_id)
    with transaction.atomic():
        grant, connection = reads._lock_slack_grant_api_authority(authority, required_scopes={target.read_scope})
        row = ((connection.sync_cursor or {}).get(KEY) or {}).get(key) or {}
        recover = row.get('consent') != authority.consent_generation
        consented_at = grant.consented_at
    recovery = BuzzBridgeClient.inbox_export_frontier(community_id=community, account_key=account,
        channel_id=target.channel_id, consented_at=consented_at) if recover else None
    with transaction.atomic():
        _, connection = reads._lock_slack_grant_api_authority(authority, required_scopes={target.read_scope})
        rows = dict((connection.sync_cursor or {}).get(KEY) or {})
        row = dict(rows.get(key) or {})
        if row.get('consent') != authority.consent_generation:
            row = {'consent': authority.consent_generation}
        if recovery is not None:
            state = exports.settle((connection.sync_cursor or {}).get(exports.KEY), key, recovery)
            connection.sync_cursor = {**(connection.sync_cursor or {}), exports.KEY: state}
        context = {'community_id': community, 'account_key': account, 'channel_id': str(target.channel_id),
                   'source_id': target.slack_id, 'authority': reads._cache_key(authority, target),
                   'consent': authority.consent_generation}
        rows[key] = {**row, **context}
        connection.sync_cursor = {**(connection.sync_cursor or {}), KEY: rows}
        connection.save(update_fields=['sync_cursor', 'updated_at'])
        applied = (((connection.sync_cursor or {}).get(exports.KEY) or {}).get(key) or {}).get('applied', 0)
    return {**context, 'applied_revision': int(applied)}


def coalesce(rows, context, last_read, *, observed_at, now, revision_floor=0,
             source_frontier=None, own_echo=False):
    """A newer observation replaces transport work, retaining exact source order."""
    stamp = timestamp(last_read)
    if stamp is None or not 0 <= now - observed_at < MAX_AGE:
        return rows, False
    key = exports.room_key(context['community_id'], context['channel_id'])
    result = dict(rows or {})
    previous = dict(result.get(key) or {})
    if previous.get('consent') != context['consent'] or previous.get('authority') not in (None, context['authority']):
        previous = {}
    if observed_at < previous.get('observed_at', 0):
        return rows, False
    comparisons = [timestamp(previous.get(name)) for name in ('last_read', 'delivered_ts')]
    regress = any(value is not None and Decimal(stamp) < Decimal(value) for value in comparisons)
    # Suppress only the first echo of this scoped device confirmation. After
    # a later source read, returning to that timestamp is a real regression.
    first_own_echo = own_echo and previous.get('native_echo_revision') != context['applied_revision']
    revision = max(int(previous.get('revision') or 0), revision_floor, time.time_ns() // 1000) + 1
    if revision > exports.I64_MAX:
        raise ValueError('inbox_revision_exhausted')
    pending = {'account_key': context['account_key'], 'channel_id': context['channel_id'],
               'slack_last_read': source_frontier or stamp, 'op': 'regress' if regress and not first_own_echo else 'observe',
               'applied_revision': context['applied_revision'], 'revision': revision,
               'created_at': int(observed_at)}
    if own_echo:
        previous['native_echo_revision'] = context['applied_revision']
    result[key] = {**previous, **context, 'last_read': stamp, 'observed_at': observed_at,
                   'revision': revision, 'pending': pending, 'pending_raw_ts': stamp, 'due': now}
    return result, True


def observe_locked(connection, context, last_read, *, observed_at):
    """Persist under the caller's owner authority lock, before history count I/O."""
    if context is None or not enabled():
        return
    key = exports.room_key(context['community_id'], context['channel_id'])
    existing = ((connection.sync_cursor or {}).get(KEY) or {}).get(key) or {}
    if existing.get('cooldown_until', 0) > time.time():
        return
    frontier, own_echo = exports.native_source_frontier((connection.sync_cursor or {}).get(exports.KEY), context, last_read)
    rows, changed = coalesce((connection.sync_cursor or {}).get(KEY), context, last_read,
                            observed_at=observed_at, now=time.time(),
                            source_frontier=frontier, own_echo=own_echo,
                            revision_floor=int((connection.sync_cursor or {}).get(REVISION_KEY) or 0))
    if changed:
        revision = rows[exports.room_key(context['community_id'], context['channel_id'])]['revision']
        connection.sync_cursor = {**(connection.sync_cursor or {}), KEY: rows, REVISION_KEY: revision}
        connection.save(update_fields=['sync_cursor', 'updated_at'])


def confirmed_locked(connection, authority, target, last_read, *, observed_at):
    """A confirmed write uses the fence already settled by complete_read."""
    if not enabled() or not target.has_relay_room:
        return
    from integrations.services import slack_chat_read_state as reads
    rows = (connection.sync_cursor or {}).get(KEY) or {}
    for key, row in rows.items():
        if (row.get('channel_id') == str(target.channel_id)
                and row.get('consent') == authority.consent_generation
                and row.get('authority') == reads._cache_key(authority, target)):
            context = {name: row[name] for name in ('community_id', 'account_key', 'channel_id', 'source_id', 'authority', 'consent')}
            context['applied_revision'] = int((((connection.sync_cursor or {}).get(exports.KEY) or {}).get(key) or {}).get('applied', 0))
            observe_locked(connection, context, last_read, observed_at=observed_at)
            return
    # A write before the first metadata probe retains its receipt and lets the
    # scheduled probe establish the scoped consent checkpoint.
    schedule_locked(connection, target.slack_id)


def schedule_locked(connection, source_id):
    """Wake a fresh owner probe after publication or an expired observation."""
    if not enabled():
        return
    from .read_priority import KEY as HINT_KEY, merged_hints
    from .read_state import KEY as WORKER_KEY
    cursor = dict(connection.sync_cursor or {})
    cursor[HINT_KEY] = merged_hints(cursor.get(HINT_KEY) or {}, [source_id], now=time.time(), reason='visible')
    cursor[WORKER_KEY] = {**(cursor.get(WORKER_KEY) or {}), 'due': 0}
    connection.sync_cursor = cursor
    connection.save(update_fields=['sync_cursor', 'updated_at'])


def publication(conversation_id):
    """After commit, wake the first source probe under current owner authority."""
    if not enabled():
        return
    from integrations.models import SlackDmMirrorConversation
    from integrations.services import slack_chat_read_state as reads
    conversation = SlackDmMirrorConversation.objects.select_related('grant__connection').get(pk=conversation_id)
    authority = reads._capture_slack_grant_api_authority(conversation.grant, refresh_token=False)
    with transaction.atomic():
        _, connection = reads._lock_slack_grant_api_authority(authority, required_scopes={'im:read'})
        schedule_locked(connection, conversation.slack_conversation_id)


def flush(authority, grant, keys):
    """Retry unchanged signatures; expired probes require new Slack metadata."""
    if not enabled():
        return
    from integrations.services import slack_chat_read_state as reads
    from integrations.services.community_bridge.buzz import BuzzBridgeClient, BuzzBridgeError, BuzzBridgePermanentError
    now = time.time()
    with transaction.atomic():
        _, connection = reads._lock_slack_grant_api_authority(authority, required_scopes={'im:read'})
        targets = {str(target.channel_id): target for target in reads._targets_for_keys(grant, keys)}
        rows = dict((connection.sync_cursor or {}).get(KEY) or {})
        eligible = []
        for key, row in list(rows.items()):
            pending = row.get('pending')
            if not pending or row.get('due', 0) > now:
                continue
            target = targets.get(row['channel_id'])
            if (target is None or target.slack_id != row['source_id'] or target.read_scope not in authority.scopes
                    or row['authority'] != reads._cache_key(authority, target)):
                rows.pop(key, None)
                continue
            if row.get('cooldown_until', 0) > now:
                _clear_pending(row)
                rows[key] = row
                continue
            if not 0 <= now - pending['created_at'] < MAX_AGE:
                rows[key] = {k: v for k, v in row.items() if k not in {'pending', 'pending_raw_ts', 'due'}}
                schedule_locked(connection, target.slack_id)
                continue
            eligible.append((row.get('due', 0), key, pending))
        batch = [(key, pending) for _, key, pending in sorted(eligible)[:200]]
        connection.sync_cursor = {**(connection.sync_cursor or {}), KEY: rows}
        connection.save(update_fields=['sync_cursor', 'updated_at'])
        # Hold the authority lock through submission so consent revocation or
        # device loss cannot overtake delivery of a private-room observation.
        responses = None
        if batch:
            try:
                responses = BuzzBridgeClient.push_inbox_cursors([pending for _, pending in batch])
            except BuzzBridgePermanentError as exc:
                logger.error('inbox cursor push rejected', extra={'count': len(batch), 'error': exc.__class__.__name__, 'adapter_error': getattr(exc, 'adapter_error', '')})
            except BuzzBridgeError as exc:
                logger.warning('inbox cursor push unavailable', extra={'count': len(batch), 'error': str(exc)[:200]})
            except Exception:
                logger.exception('inbox cursor push failed')
        for index, (key, pending) in enumerate(batch):
            row = rows[key]
            r = _normalize_result(responses[index]) if isinstance(responses, list) and index < len(responses) else None
            if r is None:
                _backoff(row, now)
            elif r['accepted'] and r['message'].startswith('ignored:causal_fence:'):
                schedule_locked(connection, row['source_id'])
                _clear_pending(row)
            elif r['accepted'] and r['message'].startswith('ignored:unknown_account:'):
                _clear_pending(row)
                _reprobe(connection, row, now)
            elif r['accepted']:
                row['delivered_ts'] = row.get('pending_raw_ts', pending['slack_last_read'])
                _clear_pending(row)
                for field in ('attempts', 'rejected', 'cooldown_until', 'reprobe_after'):
                    row.pop(field, None)
            elif r['message'].startswith('invalid: inbox source timestamp'):
                _clear_pending(row)
                _reprobe(connection, row, now)
            elif r['message'].startswith('restricted: inbox disabled') or r['retryable']:
                _backoff(row, now)
            else:
                _clear_pending(row)
                row['rejected'] = {'code': r['code'], 'status': r['status'], 'at': now}
                row['cooldown_until'] = now + (FORBIDDEN_COOLDOWN if r['code'] == 'forbidden' else INVALID_COOLDOWN)
                logger.warning('inbox cursor rejected', extra={'channel_id': row.get('channel_id'), 'code': r['code'], 'status': r['status']})
        connection.sync_cursor = {**(connection.sync_cursor or {}), KEY: rows}
        connection.save(update_fields=['sync_cursor', 'updated_at'])
