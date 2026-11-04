"""Resolve missing public bridge dependencies using positive, scoped evidence."""

from dataclasses import dataclass
from datetime import timedelta
import logging
import re

from django.conf import settings

from integrations.models import CommunityBridgeDelivery, CommunityBridgeReceipt
from integrations.services.community_bridge import buzz

logger = logging.getLogger(__name__)
IN_FLIGHT = ('pending', 'processing', 'failed', 'waiting_parent')
MUTATIONS = frozenset({'edit', 'delete', 'reaction_add', 'reaction_remove'})
PARK_RECHECK_SECONDS = 7 * 86400
IGNORED_TERMINAL = frozenset({'subtype:channel_join', 'subtype:channel_leave', 'subtype:huddle_thread', 'userless_bot'})


@dataclass(frozen=True)
class DependencyVerdict:
    """An outcome and its content-free evidence classification."""

    action: str
    code: str
    kind: str
    evidence: str = ''


def dependency_kind(dependency_id):
    """Classify opaque parent identities without inspecting message content."""
    if dependency_id.startswith('reaction:'):
        return 'reaction_object'
    if buzz.EVENT_ID_RE.fullmatch(dependency_id):
        return 'buzz_event'
    return 'slack_ts' if re.fullmatch(r'\d+\.\d+', dependency_id) else 'other'


def classify_missing_dependency(delivery, dependency_id, *, first_seen, now) -> DependencyVerdict:
    """Wait unless scoped outbox or receipt evidence proves a dependency absent."""
    from integrations.services.message_sync.history import PUBLIC_HISTORY_DAYS
    kind = dependency_kind(dependency_id)
    if {delivery.source_platform, delivery.target_platform} != {'slack', 'buzz'}:
        return DependencyVerdict('wait', 'platform_not_covered', kind)
    candidates = CommunityBridgeDelivery.objects.filter(
        channel_id=delivery.channel_id, source_platform=delivery.source_platform,
        source_channel_id=delivery.source_channel_id, target_platform=delivery.target_platform,
        source_message_id=dependency_id,
        delivery_type='reaction_add' if kind == 'reaction_object' else 'create',
    ).exclude(pk=delivery.pk)
    opposite = CommunityBridgeDelivery.objects.filter(
        channel_id=delivery.channel_id, source_platform=delivery.target_platform,
        target_platform=delivery.source_platform, delivery_type='create', status__in=IN_FLIGHT,
    ).exclude(pk=delivery.pk)
    if candidates.filter(status__in=IN_FLIGHT).exists() or opposite.exists():
        return DependencyVerdict('wait', 'dependency_in_flight', kind)
    age = (now - first_seen).total_seconds()
    if age < getattr(settings, 'COMMUNITY_BRIDGE_DEPENDENCY_VERDICT_MIN_AGE_SECONDS', 3600):
        return DependencyVerdict('wait', 'too_recent', kind)
    evidence = ''
    if candidates.filter(status='dead').exists():
        code = 'dependency_dead'
    elif candidates.filter(status='completed').exists():
        code = 'dependency_completed_without_link'
    else:
        code = ''
        if kind == 'slack_ts' and delivery.source_platform == 'slack':
            receipts = CommunityBridgeReceipt.objects.filter(
                platform='slack', source_channel_id=delivery.source_channel_id, status='ignored')
            for error in receipts.filter(source_message_id=dependency_id).values_list('error_text', flat=True):
                detail = str(error or '').partition(':')[2]
                if detail in IGNORED_TERMINAL:
                    code = 'dependency_ignored_' + detail.removeprefix('subtype:')
                    evidence = 'column'
                    break
            if not code:
                rows = receipts.filter(source_message_id='',
                    created_at__gte=now - timedelta(days=getattr(settings, 'COMMUNITY_BRIDGE_RAW_PAYLOAD_RETENTION_DAYS', 30)),
                    payload__event__ts=dependency_id).values_list('payload__event__subtype', 'payload__event__user', 'payload__event__bot_id')[:5]
                bridge_bot = str(getattr(settings, 'SLACK_BRIDGE_BOT_USER_ID', '') or '')
                for subtype, user, bot_id in rows:
                    if bridge_bot and user == bridge_bot:
                        continue
                    if subtype in {'channel_join', 'channel_leave', 'huddle_thread'}:
                        code = 'dependency_ignored_' + subtype
                    elif subtype == 'bot_message' and not user:
                        code = 'dependency_ignored_userless_bot'
                    if code:
                        evidence = 'payload'
                        break
        if not code and kind == 'slack_ts' and float(dependency_id) < now.timestamp() - (PUBLIC_HISTORY_DAYS + 1) * 86400:
            code = 'dependency_outside_import_window'
        if not code and kind == 'reaction_object':
            code = 'reaction_target_absent'
        if not code and kind == 'buzz_event':
            code = 'buzz_parent_not_mirrored'
        if not code:
            return DependencyVerdict('wait', 'unknown', kind)
    if delivery.delivery_type in MUTATIONS:
        action = 'noop'
    elif code == 'dependency_outside_import_window' and age < PUBLIC_HISTORY_DAYS * 86400:
        action = 'park'
    else:
        action = 'dead_letter'
    return DependencyVerdict(action, code, kind, evidence)


def apply_dependency_verdict(row, verdict, *, dependency_id, first_seen, now, claimed: bool):
    """Apply a reviewed verdict under the caller's delivery lock and lease guard."""
    from integrations.services.community_bridge import store
    if verdict.action not in {'noop', 'dead_letter', 'park'}:
        raise ValueError('Only terminal or parked dependency verdicts can be applied')
    fields = ['status', 'available_at', 'last_error', 'dependency_first_seen_at', 'updated_at']
    row.dependency_first_seen_at = first_seen
    if claimed:
        row.attempts = max(0, int(row.attempts or 0) - 1)
        row.dependency_attempts = min(int(row.dependency_attempts or 0) + 1, 32767)
        row.locked_at = row.lease_token = row.lease_expires_at = None
        fields += ['attempts', 'dependency_attempts', 'locked_at', 'lease_token', 'lease_expires_at']
    row.available_at = now
    if verdict.action == 'noop':
        row.status = 'completed'
        row.completed_at = now
        fields.append('completed_at')
        prefix = 'dependency_noop'
    elif verdict.action == 'dead_letter':
        row.status = 'dead'
        prefix = 'parent_unresolvable'
    else:
        row.status = 'waiting_parent'
        row.available_at = now + timedelta(seconds=PARK_RECHECK_SECONDS)
        prefix = 'parent_mapping_parked'
    row.last_error = f'{prefix}:{verdict.code}:{dependency_id}'
    row.save(update_fields=fields)
    if verdict.action in {'noop', 'dead_letter'}:
        store._wake_waiting_child_deliveries(row)
    logger.info('community_bridge_dependency_outcome delivery_id=%s action=%s code=%s delivery_type=%s source_platform=%s target_platform=%s evidence=%s',
                row.pk, verdict.action, verdict.code, row.delivery_type, row.source_platform, row.target_platform, verdict.evidence)
