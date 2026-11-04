"""Classify private Slack authors without broadening a mirror's consent boundary."""

from dataclasses import dataclass
import logging

from django.utils import timezone
from django.utils.dateparse import parse_datetime

from integrations.models import SlackDmMirrorDelivery

logger = logging.getLogger(__name__)
AUTHOR_REFRESH_RETRY_SECONDS = 600
IDENTITY_STALE_RETRY_SECONDS = 900
TERMINAL_REASONS = frozenset({'consent_excludes_author', 'system_author'})
UNDELIVERABLE_REASONS = TERMINAL_REASONS | {'non_participant_author'}


@dataclass(frozen=True)
class AuthorResolution:
    """An existing recipient key or the reason resolution must wait or stop."""

    pubkey: str
    reason: str


def resolve_author(delivery) -> AuthorResolution:
    """Resolve only identities already admitted to this owner mirror."""
    from integrations.services import slack_dm_mirror as dm
    from integrations.services.slack_chat_catalog import conversation_kind
    conv = delivery.conversation
    metadata = delivery.metadata or {}
    pubkey = dm._history_delivery_author_pubkey(delivery)
    if pubkey:
        return AuthorResolution(pubkey, '')
    if (delivery.source_author_id in (conv.participant_slack_ids or [])
            or (dm._all_history_group_import(conv) and metadata.get('backfill'))):
        reason = 'identity_stale'
    elif conversation_kind(conv) in {'mpim', 'private_channel'} and metadata.get('backfill'):
        reason = 'consent_excludes_author'
    elif conversation_kind(conv) == 'im' and (
            delivery.source_author_id == 'USLACKBOT' or delivery.source_author_id.startswith('B')):
        reason = 'system_author'
    else:
        reason = 'non_participant_author'
    return AuthorResolution('', reason)


def author_refresh_observed(delivery) -> bool:
    """Require this conversation's membership confirmation after the request."""
    from integrations.services.slack_chat_catalog import conversation_metadata
    try:
        requested = parse_datetime(str((delivery.metadata or {}).get('author_refresh_requested_at') or ''))
        confirmed = parse_datetime(str(conversation_metadata(delivery.conversation).get('membership_confirmed_at') or ''))
        return bool(requested and confirmed and confirmed > requested)
    except (TypeError, ValueError):
        return False


def mark_undeliverable_locked(delivery, *, reason: str, source: str) -> None:
    """Erase an unrepresentable body while holding the complete authority lock chain."""
    from integrations.services import slack_dm_mirror as dm
    from integrations.services.slack_chat_catalog import conversation_kind
    if reason not in UNDELIVERABLE_REASONS:
        raise ValueError('Invalid undeliverable reason')
    conv = delivery.conversation
    if delivery.status == 'processing':
        dm._assert_private_delivery_authorized_locked(delivery)
    elif delivery.status in {'pending', 'failed'}:
        if not (conv.grant.status == 'active' and conv.grant.revoked_at is None
                and conv.status == 'live' and str((delivery.metadata or {}).get('participant_hash') or '')
                in {'', str(conv.participant_hash or '')}):
            raise dm.SlackDmMirrorAuthorizationError('The private delivery is no longer authorized.')
    else:
        raise ValueError('Delivery is not actionable')
    now = timezone.now()
    metadata = dict(delivery.metadata or {})
    metadata.update(dependency_superseded=True,
                    dependency_superseded_reason='The Slack author is not represented in this owner mirror.',
                    undeliverable=True, undeliverable_reason=reason,
                    undeliverable_at=now.isoformat(), undeliverable_source=source)
    metadata.pop('author_refresh_requested_at', None)
    delivery.metadata = metadata
    delivery.status = 'completed'
    delivery.encrypted_text = ''
    delivery.completed_at = now
    delivery.last_error = ''
    delivery.save(update_fields=('status', 'encrypted_text', 'metadata', 'completed_at', 'last_error', 'updated_at'))
    logger.info('slack_dm_mirror_delivery_undeliverable delivery_id=%s conversation_id=%s kind=%s operation=%s reason=%s backfill=%s source=%s',
                delivery.pk, conv.pk, conversation_kind(conv), delivery.operation, reason, bool(metadata.get('backfill')), source)


def author_class(delivery) -> str:
    """Return a content-free author category for operator review."""
    author = delivery.source_author_id
    if author in (delivery.conversation.participant_slack_ids or []):
        return 'participant'
    if author == 'USLACKBOT':
        return 'slackbot'
    if author.startswith('B'):
        return 'bot_id'
    return 'non_participant_human' if author.startswith(('U', 'W')) else 'other'


def plan_row(delivery, *, now) -> dict:
    """Describe the next safe action, omitting author identifiers and body text."""
    from integrations.services import slack_dm_mirror as dm
    from integrations.services.slack_chat_catalog import conversation_kind
    conv = delivery.conversation
    metadata = delivery.metadata or {}
    resolution = resolve_author(delivery)
    stale = str(metadata.get('participant_hash') or '') not in {'', str(conv.participant_hash or '')}
    if conv.grant.status != 'active' or conv.grant.revoked_at is not None or conv.status != 'live':
        action = 'skip_authority'
    elif stale:
        action = 'skip_participants_changed'
    elif resolution.pubkey:
        action = 'none'
    elif resolution.reason in TERMINAL_REASONS:
        action = 'mark_undeliverable'
    elif resolution.reason == 'non_participant_author':
        action = 'mark_undeliverable' if author_refresh_observed(delivery) else 'request_identity_refresh'
    else:
        action = 'keep_retrying'
    return dict(delivery_id=delivery.pk, conversation_id=conv.pk, grant_id=conv.grant_id,
                conversation_status=conv.status, kind=conversation_kind(conv), operation=delivery.operation,
                backfill=bool(metadata.get('backfill')), thread_reply=bool(metadata.get('thread_ts') and metadata['thread_ts'] != delivery.source_message_id),
                author_class=author_class(delivery), author_in_participants=delivery.source_author_id in (conv.participant_slack_ids or []),
                author_in_identity_map=bool((conv.participant_identity_map or {}).get(delivery.source_author_id)),
                group_import_consent=dm._all_history_group_import(conv),
                import_key_present=dm._shadow_pubkey(conv, 'private-channel-import') in (conv.participant_buzz_pubkeys or []),
                attempts=delivery.attempts, age_seconds=max(0, int((now - delivery.created_at).total_seconds())),
                refresh_requested=bool(metadata.get('author_refresh_requested_at')), refresh_observed=author_refresh_observed(delivery),
                verdict=resolution.reason or 'resolved', action=action)


def retire_consent_excluded_rows_locked(grant, conversations) -> int:
    """Retire queued excluded authors under the caller's grant and conversation locks."""
    by_id = {conv.pk: conv for conv in conversations}
    retired = 0
    if not by_id:
        return 0
    rows = SlackDmMirrorDelivery.objects.select_for_update().filter(
        source_platform='slack', metadata__backfill=True, status__in=('pending', 'failed'),
        conversation_id__in=by_id,
    ).exclude(source_message_id__startswith='history-state:').exclude(source_message_id__startswith='registration-state:').order_by('id')
    for row in rows:
        row.conversation = by_id[row.conversation_id]
        row.conversation.grant = grant
        if str((row.metadata or {}).get('participant_hash') or '') != str(row.conversation.participant_hash or ''):
            continue
        if resolve_author(row).reason == 'consent_excludes_author':
            mark_undeliverable_locked(row, reason='consent_excludes_author', source='consent_change')
            retired += 1
    logger.info('slack_dm_mirror_consent_sweep grant_id=%s retired=%s', grant.pk, retired)
    return retired


def author_resolves_for_observation(conversation, *, author_id, metadata) -> bool:
    """Check a fresh source observation against the current admitted identities."""
    from integrations.services import slack_dm_mirror as dm
    probe = SlackDmMirrorDelivery(conversation=conversation, source_platform='slack', source_author_id=author_id, metadata=metadata)
    return bool(dm._history_delivery_author_pubkey(probe))
