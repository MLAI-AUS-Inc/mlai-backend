"""Durable account cursor exports, with confirmed/permanent causal settlement."""
import hmac
import re
import time
import uuid
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from integrations.services.slack_dm_mirror import SlackDmMirrorAuthorizationError

KEY = 'message_sync_inbox_exports_v1'
I64_MAX = 2**63 - 1
HEX = re.compile(r'^[0-9a-f]{64}$')


def parse_export(payload):
    """Validate the authenticated callback without trusting IDs as authority."""
    if set(payload) != {'type', 'community_id', 'account_key', 'channel_id', 'op',
                        'read_through_event', 'read_through_us', 'revision'}:
        raise ValueError('invalid_read_cursor')
    if payload['type'] != 'read_cursor' or payload['op'] not in {'read', 'unread'}:
        raise ValueError('invalid_read_cursor')
    result = dict(payload)
    for field in ('community_id', 'channel_id'):
        result[field] = str(uuid.UUID(str(payload[field])))
    if not isinstance(result['account_key'], str) or not HEX.fullmatch(result['account_key']):
        raise ValueError('invalid_read_cursor')
    if result['read_through_event'] is not None and (
        not isinstance(result['read_through_event'], str) or not HEX.fullmatch(result['read_through_event'])
    ):
        raise ValueError('invalid_read_cursor')
    for field in ('revision', 'read_through_us'):
        value = payload[field]
        if not isinstance(value, str) or not re.fullmatch(r'-?[0-9]+', value):
            raise ValueError('invalid_read_cursor')
        result[field] = int(value)
    if not 1 <= result['revision'] <= I64_MAX or not -I64_MAX - 1 <= result['read_through_us'] <= I64_MAX:
        raise ValueError('invalid_read_cursor')
    return result


def room_key(community_id, channel_id):
    return f'{community_id}/{channel_id}'


def coalesce(state, export, *, now):
    """Newest relay revision wins, including explicit unread over an older read."""
    result = dict(state or {})
    key = room_key(export['community_id'], export['channel_id'])
    previous = result.get(key) or {}
    if export['revision'] <= int(previous.get('received') or 0):
        return result, False
    result[key] = {'received': export['revision'], 'applied': int(previous.get('applied') or 0),
                   'pending': {**export, 'requested_at': now, 'due': now}}
    return result, True


def settle(state, key, revision):
    """Release the fence only after confirmation or a permanent give-up."""
    result = dict(state or {})
    previous = dict(result.get(key) or {})
    previous['applied'] = max(int(previous.get('applied') or 0), revision)
    pending = previous.get('pending') or {}
    if int(pending.get('revision') or 0) <= revision:
        previous.pop('pending', None)
    previous['received'] = max(int(previous.get('received') or 0), revision)
    result[key] = previous
    return result


def save(connection, state):
    connection.sync_cursor = {**(connection.sync_cursor or {}), KEY: state}
    connection.save(update_fields=['sync_cursor', 'updated_at'])


def give_up_on_disconnect(cursor):
    """Preserve revision fences only; erase source IDs, times and pending work."""
    state = dict((cursor or {}).get(KEY) or {})
    for key, value in list(state.items()):
        state = settle(state, key, int(value.get('received') or 0))
    return {KEY: state} if state else {}


def confirmed_read(connection, target, source_ts):
    """Settle pending exported reads covered by the confirmed Slack frontier."""
    from integrations.services import slack_chat_read_state as reads
    confirmed = reads._timestamp(source_ts)
    if confirmed is None:
        return
    state = dict((connection.sync_cursor or {}).get(KEY) or {})
    changed = False
    for key, value in list(state.items()):
        pending = value.get('pending') or {}
        if (pending.get('op') == 'read' and pending.get('channel_id') == target.channel_id
                and reads._timestamp(pending.get('source_ts')) is not None
                and reads._timestamp(pending['source_ts']) <= confirmed):
            state = settle(state, key, pending['revision'])
            changed = True
    if changed:
        save(connection, state)


def resolve_link_timestamp(links, event_id, frontier_us, *, unread=False):
    """Resolve inbound/native links first, then latest inbound at/before F."""
    candidates = []
    for link in links:
        if getattr(link, 'source_deleted_at', None) or getattr(link, 'destination_deleted_at', None):
            continue
        inbound = link.source_platform == 'slack' and link.destination_platform == 'buzz'
        outgoing = link.source_platform == 'buzz' and link.destination_platform == 'slack'
        if not (inbound or outgoing):
            continue
        stamp = link.source_message_id if inbound else link.destination_message_id
        try:
            value = Decimal(str(stamp))
            if not value.is_finite() or value < 0:
                continue
        except (InvalidOperation, ValueError, TypeError):
            continue
        anchor = link.destination_message_id if inbound else link.source_message_id
        if event_id and anchor == event_id:
            return format(max(Decimal(0), value - (Decimal('0.000001') if unread else 0)), '.6f')
        if inbound and int(value * 1_000_000) <= frontier_us:
            candidates.append(value)
    return format(max(candidates), '.6f') if candidates else '0.000000'


def _candidates(export):
    from integrations.models import CommunityBridgeChannel, SlackDmMirrorConversation, SlackDmMirrorGrant
    private = SlackDmMirrorConversation.objects.select_related('grant__connection').filter(
        mlai_channel_id=export['channel_id']).first()
    if private is not None:
        return [(private.grant, private, None)]
    mapped = CommunityBridgeChannel.objects.filter(
        destination_platform='buzz', destination_channel_id=export['channel_id'], enabled=True).first()
    if mapped is None:
        return []
    return [(grant, None, mapped) for grant in SlackDmMirrorGrant.objects.select_related('connection').filter(
        slack_workspace_id=mapped.slack_workspace_id).order_by('user_id', 'id')]


def _locked_owner_connection(grant):
    """User->all grants->connection lock order also permits permanent settlement."""
    from integrations.models import SlackDmMirrorGrant, ExternalServiceConnection
    get_user_model().objects.select_for_update().get(pk=grant.user_id)
    list(SlackDmMirrorGrant.objects.select_for_update().filter(user_id=grant.user_id).order_by('id'))
    return ExternalServiceConnection.objects.select_for_update().get(pk=grant.connection_id, user_id=grant.user_id)


def accept(payload):
    """Enqueue only an HMAC-matched owner and currently authorized mapped room."""
    if not getattr(settings, 'MESSAGE_SYNC_INBOX_READ_EXPORT', False):
        return {'status': 'disabled'}
    from community_chat.inbox_accounts import account_key
    from community_chat.models import CommunityChatDevice
    from community_chat import adapter
    from integrations.services import slack_chat_read_state as reads
    from integrations.models import CommunityBridgeMessageLink
    export = parse_export(payload)
    capabilities, _ = adapter._request('GET', '/v1/capabilities')
    if str(capabilities.get('community_id')) != export['community_id']:
        raise ValueError('wrong_inbox_community')
    match = next(((g, private, mapped) for g, private, mapped in _candidates(export)
                  if hmac.compare_digest(account_key(export['community_id'], g.user_id), export['account_key'])), None)
    if match is None:
        return {'status': 'unmapped_account'}
    grant, private, mapped = match
    with transaction.atomic():
        connection = _locked_owner_connection(grant)
        state, changed = coalesce((connection.sync_cursor or {}).get(KEY), export, now=time.time())
        if not changed:
            return {'status': 'duplicate'}
        key = room_key(export['community_id'], export['channel_id'])
        try:
            grant.refresh_from_db()
            authority = reads._capture_slack_grant_api_authority(grant)
            keys = list(CommunityChatDevice.objects.filter(user_id=grant.user_id, status='verified', revoked_at__isnull=True).order_by('pk').values_list('public_key', flat=True))
            chosen = next(((pk, target) for pk in keys for target in reads._targets_for_keys(grant, {pk})
                           if target.channel_id == export['channel_id']), None)
            if chosen is None:
                raise SlackDmMirrorAuthorizationError('room_unavailable')
            public_key, target = chosen
            scope = {'im': 'im:write', 'mpim': 'mpim:write', 'private_channel': 'groups:write'}.get(target.kind, 'channels:write')
            if not {scope, target.read_scope}.issubset(authority.scopes):
                raise SlackDmMirrorAuthorizationError('write_scope_unavailable')
        except SlackDmMirrorAuthorizationError:
            save(connection, settle(state, key, export['revision']))
            return {'status': 'permanent_give_up'}
        if private is not None:
            from integrations.services.slack_dm_mirror import _slack_destination_message_id, _current_private_delivery_rows
            ts = _slack_destination_message_id(private, export['read_through_event'])
            if ts:
                ts = format(max(Decimal(0), Decimal(ts) - (Decimal('0.000001') if export['op'] == 'unread' else 0)), '.6f')
            else:
                rows = _current_private_delivery_rows(private)
                if rows.filter(source_platform='buzz', source_message_id=export['read_through_event'], operation='create').exclude(status__in=['completed', 'failed']).exists():
                    raise RuntimeError('private_inbox_anchor_pending')
                inbound = latest_inbound_before(rows.filter(source_platform='slack', operation='create', status='completed'), export['read_through_us'])
                ts = format(Decimal(inbound.source_message_id), '.6f') if inbound else '0.000000'
        else:
            links = CommunityBridgeMessageLink.objects.filter(channel=mapped, source_deleted_at__isnull=True, destination_deleted_at__isnull=True)
            anchor = export['read_through_event']
            from django.db.models import Q
            direct = list(links.filter(Q(source_platform='buzz', source_message_id=anchor) | Q(destination_platform='buzz', destination_message_id=anchor))) if anchor else []
            if not direct and anchor:
                from integrations.models import CommunityBridgeDelivery
                if CommunityBridgeDelivery.objects.filter(channel=mapped, source_platform='buzz', source_message_id=anchor, delivery_type='create').exclude(status__in=['completed', 'failed']).exists():
                    raise RuntimeError('inbox_delivery_link_pending')
            inbound = latest_inbound_before(links.filter(source_platform='slack', destination_platform='buzz'), export['read_through_us']) if not direct else None
            fallback = [inbound] if inbound else []
            ts = resolve_link_timestamp(direct + fallback, anchor, export['read_through_us'], unread=export['op'] == 'unread')
        pending = state[key]['pending']
        pending.update(source_ts=ts, public_key=public_key, source_id=target.slack_id)
        from .receipts import KEY as RECEIPTS_KEY, enqueue_read
        if export['op'] == 'read':
            pending['device'] = enqueue_read(authority, target, public_key=public_key, source_ts=ts)
        else:
            queue = {k: v for k, v in ((connection.sync_cursor or {}).get(RECEIPTS_KEY) or {}).items() if v.get('source_id') != target.slack_id}
            connection.sync_cursor = {**(connection.sync_cursor or {}), RECEIPTS_KEY: queue}
            connection.save(update_fields=['sync_cursor', 'updated_at'])
        connection = _locked_owner_connection(grant)
        save(connection, state)
        from .read_state import KEY as WORKER_KEY
        connection.sync_cursor[WORKER_KEY] = {**(connection.sync_cursor.get(WORKER_KEY) or {}), 'due': 0}
        connection.save(update_fields=['sync_cursor', 'updated_at'])
    return {'status': 'queued'}


def flush_once(grant, authority, keys):
    """Retry the newest durable export through existing provider admission."""
    from integrations.services import slack_chat_read_state as reads
    with transaction.atomic():
        _, connection = reads._lock_slack_grant_api_authority(authority, required_scopes={'im:read'})
        state = dict((connection.sync_cursor or {}).get(KEY) or {})
        now = time.time()
        candidates = [(k, v['pending']) for k, v in state.items() if v.get('pending') and v['pending'].get('due', 0) <= now]
        if not candidates:
            return None
        key, pending = min(candidates, key=lambda item: item[1]['requested_at'])
        target = next((t for t in reads._targets_for_keys(grant, {pending['public_key']} & keys) if t.channel_id == pending['channel_id']), None)
        scope = {'im': 'im:write', 'mpim': 'mpim:write', 'private_channel': 'groups:write'}.get(getattr(target, 'kind', ''), 'channels:write')
        if target is None or now - pending['requested_at'] >= 7 * 86400 or not {scope, target.read_scope}.issubset(authority.scopes):
            save(connection, settle(state, key, pending['revision']))
            return 0
        result = None
        try:
            if pending['op'] == 'read':
                result = reads.apply_read(authority, target, source_ts=pending['source_ts'], required={scope, target.read_scope}, public_key=pending['public_key'], device_binding=pending['device'])
            else:
                result = apply_unread(authority, target, pending, required={scope, target.read_scope})
        except SlackDmMirrorAuthorizationError:
            save(connection, settle(state, key, pending['revision']))
            return 0
        except Exception as error:
            permanent = (getattr(error, 'response', {}) or {}).get('error') in {
                'missing_scope', 'token_revoked', 'invalid_auth', 'account_inactive',
                'not_in_channel', 'channel_not_found',
            }
            if permanent:
                _, connection = reads._lock_slack_grant_api_authority(authority, required_scopes={target.read_scope})
                save(connection, settle((connection.sync_cursor or {}).get(KEY), key, pending['revision']))
                return 0
            _, connection = reads._lock_slack_grant_api_authority(authority, required_scopes={target.read_scope})
            current = dict((connection.sync_cursor or {}).get(KEY) or {})
            if (current.get(key) or {}).get('pending') == pending:
                current[key] = {**current[key], 'pending': {**pending, 'due': now + max(1, getattr(error, 'retry_after', 30))}}
                save(connection, current)
            return None
        if result and (result.get('synced') or result.get('cancelled')):
            if pending['op'] == 'read' and result.get('synced'):
                from .receipts import complete_read
                complete_read(authority, target, source_ts=result['last_read'])
            _, connection = reads._lock_slack_grant_api_authority(authority, required_scopes={target.read_scope})
            state = dict((connection.sync_cursor or {}).get(KEY) or {})
            save(connection, settle(state, key, pending['revision']))
            return 1
        return None


def apply_unread(authority, target, pending, *, required):
    """Explicit unread uses the exact mapped anchor; ordinary reads remain monotone."""
    from integrations.services import slack_chat_read_state as reads
    from integrations.services.slack_dm_mirror import _locked_active_verified_device
    if _locked_active_verified_device(authority.user_id, pending['public_key']) is None:
        raise SlackDmMirrorAuthorizationError('device_revoked')
    response = reads._call_slack_with_grant_authority(authority, 'conversations_info', required_scopes=required, channel=target.slack_id)
    details = response.get('channel') or {}
    if (details.get('id') != target.slack_id or reads._is_external_shared_conversation(details)
            or details.get('is_member') is False or (target.kind != 'im' and details.get('is_member') is not True)):
        raise SlackDmMirrorAuthorizationError('room_unavailable')
    reads._call_slack_with_grant_authority(authority, 'conversations_mark', required_scopes=required, channel=target.slack_id, ts=pending['source_ts'])
    _, connection = reads._lock_slack_grant_api_authority(authority, required_scopes=required)
    key = reads._cache_key(authority, target)
    snapshot = {**(reads.cache.get(key) or {}), 'available': False, 'last_read': pending['source_ts'], 'fetched_at': time.time(), 'confirmed_at': time.time(), 'refresh_required': True}
    from .read_snapshots import publish_snapshot
    publish_snapshot(connection, key, snapshot)
    reads.cache.delete(reads._pending_key(authority, target))
    reads.cache.set(key + ':receipt', uuid.uuid4().hex, timeout=86400)
    return {'synced': True, 'last_read': pending['source_ts']}


def latest_inbound_before(rows, frontier_us):
    """Exact bounded-result SQL lookup, including anchors older than recent pages."""
    from django.db.models import DecimalField
    from django.db.models.functions import Cast
    return rows.filter(source_message_id__regex=r'^[0-9]{1,12}\.[0-9]{6}$').annotate(
        inbox_source_time=Cast('source_message_id', DecimalField(max_digits=18, decimal_places=6))
    ).filter(inbox_source_time__lte=Decimal(frontier_us) / 1_000_000).order_by('-inbox_source_time', '-id').first()
