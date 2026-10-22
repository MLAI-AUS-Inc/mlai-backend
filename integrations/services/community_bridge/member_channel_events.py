"""Route authenticated user-app callbacks without guessing public privacy from IDs.

The callback view owns signature verification. This module performs no provider
I/O: uncertain source state wakes the bounded refresh lane; private ownership
continues through the existing encrypted private intake path.
"""
from datetime import datetime, timezone as dt_timezone
from decimal import Decimal, InvalidOperation
import re
import uuid

from django.conf import settings
from django.db import transaction

from integrations.models import CommunityBridgeChannel, SlackDmMirrorConversation, SlackDmMirrorGrant
from integrations.services import slack_member_access as access
from integrations.services import slack_channel_membership as membership
from . import member_channels as channels

_CONTENT = frozenset({'message', 'reaction_added', 'reaction_removed'})
_LIFECYCLE = frozenset({'channel_created', 'channel_rename', 'channel_archive', 'channel_unarchive',
                        'channel_deleted', 'channel_id_changed', 'channel_shared', 'channel_unshared'})
_MEMBERSHIP = frozenset({'member_joined_channel', 'member_left_channel'})
_USER_ID = re.compile(r'^[UW][A-Z0-9]{1,99}$')


def _user_app(payload):
    configured = getattr(settings, 'MESSAGE_SYNC_SLACK_USER_APP_ID', '')
    return bool(isinstance(configured, str) and configured and payload.get('api_app_id') == configured)


def _channel_id(event):
    if event.get('type') == 'channel_id_changed':
        return event.get('old_channel_id')
    raw = event.get('item') if event.get('type') in {'reaction_added', 'reaction_removed'} else event
    raw = raw if isinstance(raw, dict) else {}
    channel = raw.get('channel')
    return channel.get('id') if isinstance(channel, dict) else channel


def _objects(payload, event):
    values = [payload, event]
    values.extend(event.get(key) for key in ('item', 'channel', 'message', 'previous_message'))
    return [value for value in values if isinstance(value, dict)]


def _external(payload, event):
    # Missing fields are not public proof. Any positive/ambiguous sharing signal
    # denies new public intake, matching the existing conservative access layer.
    return event.get('type') == 'channel_shared' or any(
        raw.get(key) for raw in _objects(payload, event)
        for key in ('is_ext_shared_channel', 'is_ext_shared', 'is_ext_ws_shared',
                    'is_external_shared', 'is_pending_ext_shared', 'pending_shared', 'is_shared', 'is_org_shared')
    )


def _private_hint(event):
    item = event.get('item') if isinstance(event.get('item'), dict) else {}
    return any(raw.get('is_private') or raw.get('is_mpim') or raw.get('is_im') or raw.get('is_group')
               for raw in _objects({}, event)) or any(
        isinstance(raw.get('channel_type'), str) and raw['channel_type'] in {'group', 'mpim', 'im'}
        for raw in (event, item)
    )


def _ordinary_create(event):
    return (event.get('type') == 'message' and isinstance(event.get('subtype', ''), str)
            and event.get('subtype', '') in {'', 'thread_broadcast'}
            and not event.get('bot_id') and not event.get('bot_profile'))


def _public_mapping(row):
    return bool(row is not None and row.destination_platform == 'buzz' and row.destination_channel_id
                and row.slack_visibility == 'public' and not row.slack_archived and not row.slack_deleted
                and row.mirror_decision == 'mirror' and not row.hold_reason)


def _public_verified(row, event):
    if not _public_mapping(row):
        return False
    if row.slack_seen_at is not None:
        try:
            if 0 <= (datetime.now(dt_timezone.utc) - row.slack_seen_at).total_seconds() <= 300:
                return True
        except (AttributeError, TypeError, ValueError):
            pass
    # Legacy operator mappings can predate info observations. Only the signed
    # ordinary public-create contract supplies their missing positive type proof.
    return row.audience == 'community' and _ordinary_create(event) and event.get('channel_type') == 'channel'


def _member_intake_ready(row):
    """Validate the current local audience acknowledgement, not a send lease.

    The caller holds Channel through intake. Current provider-send authority
    still needs its own account/audience fence before any member activation.
    """
    if (row is None or not row.enabled or row.audience_sync_pending or row.room_sync_pending
            or row.room_sync_error or not _public_mapping(row)):
        return False
    state = row.room_state if isinstance(row.room_state, dict) else {}
    applied = state.get('applied')
    try:
        generation = str(uuid.UUID(str(row.audience_generation)))
        destination = uuid.UUID(str(row.destination_channel_id))
    except (TypeError, ValueError, AttributeError):
        return False
    return bool(destination.int and generation != str(uuid.UUID(int=0))
                and isinstance(row.audience_hash, str) and re.fullmatch(r'[0-9a-f]{64}', row.audience_hash)
                and isinstance(applied, dict) and applied.get('audience') == 'members'
                and state.get('audience_applied') == {
                    'generation': generation, 'hash': row.audience_hash,
                    'workspace_id': row.slack_workspace_id, 'channel_id': row.slack_channel_id,
                    'destination_channel_id': row.destination_channel_id,
                })


def _observed_at(payload, event):
    value = event.get('event_ts')
    if value is None:
        value = payload.get('event_time')
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    try:
        stamp = Decimal(value)
        if not stamp.is_finite() or stamp <= 0:
            return None
        return datetime.fromtimestamp(float(stamp), tz=dt_timezone.utc)
    except (InvalidOperation, OverflowError, ValueError, OSError):
        return None


def _subject_grants(workspace, subject):
    if not isinstance(subject, str) or not _USER_ID.fullmatch(subject):
        return []
    return SlackDmMirrorGrant.objects.filter(
        slack_workspace_id=workspace, slack_user_id=subject,
    ).select_related('user', 'connection').order_by('user_id', 'id')


def route_member_public_event(payload):
    """Return a handled result only for owned user-app public/control work."""
    if not isinstance(payload, dict) or not _user_app(payload):
        return None
    workspace = payload.get('team_id')
    if not isinstance(workspace, str) or workspace != getattr(settings, 'MESSAGE_SYNC_SLACK_BOT_WORKSPACE_ID', ''):
        return None
    event = payload.get('event')
    if payload.get('type') != 'event_callback' or not isinstance(event, dict):
        return None
    event_type = event.get('type')
    if not isinstance(event_type, str) or event_type not in _CONTENT | _LIFECYCLE | _MEMBERSHIP:
        return None
    source = _channel_id(event)
    if not channels.valid_slack_channel_identity(workspace, source):
        return None
    row = CommunityBridgeChannel.objects.filter(slack_workspace_id=workspace, slack_channel_id=source).first()
    private_owned = SlackDmMirrorConversation.objects.filter(
        slack_workspace_id=workspace, slack_conversation_id=source,
    ).exists()
    observed = _observed_at(payload, event)
    if private_owned or _private_hint(event):
        if row is not None:
            channels.mark_source_reduction(workspace, source, reason='converted_to_private', event_observed_at=observed)
        return None
    if _external(payload, event):
        if row is not None:
            channels.mark_source_reduction(workspace, source, reason='slack_connect', event_observed_at=observed)
            return {'status': 'member_source_reduction_queued'}
        return None
    reduction = {'channel_deleted': 'deleted', 'channel_archive': 'archived',
                 'channel_id_changed': 'rekey_requires_room_migration'}.get(event_type)
    if reduction:
        if row is None:
            return None
        channels.mark_source_reduction(workspace, source, reason=reduction, event_observed_at=observed,
                                       **({'new_channel_id': event.get('new_channel_id')} if event_type == 'channel_id_changed' else {}))
        return {'status': 'member_source_reduction_queued'}
    if event_type == 'member_left_channel':
        if row is None:
            if access.enabled() and channels.member_channel_in_scope(workspace, source):
                channels.queue_refresh(workspace, source, reason='public_source_unverified')
            return None
        if observed is not None:
            for grant in _subject_grants(workspace, event.get('user')):
                membership.mark_left(grant, source, observed_at=observed, source='event',
                                     public_verified=_public_verified(row, event))
        channels.mark_source_reduction(workspace, source, reason='member_left', event_observed_at=observed)
        return {'status': 'member_leave_observed'}
    if not access.enabled() or not channels.member_channel_in_scope(workspace, source):
        return {'status': 'ignored', 'reason': 'member_channels_disabled_or_out_of_scope'} if event_type in _CONTENT else None
    verified = _public_verified(row, event)
    if event_type in _LIFECYCLE:
        channels.queue_refresh(workspace, source, reason=event_type)
        return {'status': 'member_refresh_queued'} if verified else None
    if not verified:
        # The signed actor identifies a useful INFO reader, not a member. Only
        # that reader's fresh public is_member response may expand authority.
        reason = 'public_source_unverified'
        if observed is not None and (event_type == 'member_joined_channel' or _ordinary_create(event)):
            for grant in _subject_grants(workspace, event.get('user')):
                reason = channels.info_reader_hint_reason(grant.pk, observed) or reason
                break
        channels.queue_refresh(workspace, source, reason=reason)
        if row is not None and row.audience == 'members' and _public_mapping(row):
            return {'status': 'member_audience_pending'}
        return None
    if event_type == 'member_joined_channel':
        if observed is not None:
            for grant in _subject_grants(workspace, event.get('user')):
                membership.mark_joined(grant, source, observed_at=observed, source='event', public_verified=True)
        channels.queue_refresh(workspace, source, reason='member_joined')
        return {'status': 'member_join_observed'}
    if _ordinary_create(event) and observed is not None:
        for grant in _subject_grants(workspace, event.get('user')):
            membership.mark_joined(grant, source, observed_at=observed, source='message', public_verified=True)
    if row.audience == 'community' and getattr(settings, 'SLACK_COMMUNITY_SOURCE', 'bot') not in {'both', 'members'}:
        return {'status': 'ignored', 'reason': 'community_source_bot'}
    if row.audience != 'members' and not row.enabled:
        channels.queue_refresh(workspace, source, reason='member_room_not_ready')
        return {'status': 'member_refresh_queued'}
    from .store import ingest_slack_event
    # This copy is server-derived only after the exact public mapping proof;
    # caller-supplied internal flags never select an authorization path.
    normalized = {**payload, 'event': {**event, 'channel_type': 'channel'}}
    if row.audience == 'members':
        with transaction.atomic():
            current = CommunityBridgeChannel.objects.select_for_update().filter(
                pk=row.pk, slack_workspace_id=workspace, slack_channel_id=source,
                destination_platform='buzz', destination_channel_id=row.destination_channel_id,
                audience='members',
            ).first()
            if not _member_intake_ready(current) or not _public_verified(current, event):
                if current is not None:
                    # A durable wake is metadata only. Keep this callback's
                    # ciphertext until a real audience acknowledgement exists.
                    if not current.audience_sync_pending:
                        current.audience_sync_pending = True
                        current.save(update_fields=('audience_sync_pending', 'updated_at'))
                    if current.room_sync_pending or not _public_verified(current, event):
                        channels.queue_refresh(workspace, source, reason='member_room_not_ready')
                return {'status': 'member_audience_pending'}
            return ingest_slack_event(normalized, public_source_verified=True)
    return ingest_slack_event(normalized, public_source_verified=True)


def dispatch_slack_callback(payload, *, private_ingest=None, public_ingest=None):
    """Share inline/durable order while preserving private ownership and rollback."""
    from integrations.services.slack_dm_mirror import ingest_slack_dm_event
    from .store import ingest_slack_event

    result = channels.route_member_public_event(payload)
    if result is not None:
        return result
    result = (private_ingest or ingest_slack_dm_event)(payload)
    if result is not None:
        return result
    if _user_app(payload):
        return {'status': 'ignored', 'reason': 'unowned_member_event'}
    return (public_ingest or ingest_slack_event)(payload)
