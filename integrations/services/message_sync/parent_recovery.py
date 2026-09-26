"""Fresh-source repair for a rejected reply referencing a retired private room."""
import hashlib

from django.db.models import Q
from django.utils import timezone

from .reaction_recovery import LEGACY_ERRORS, _owner_has_device

CONTRACT_KEY = 'stale_parent_reference_recovery_v1'


def stale_parent_failure_query():
    """Identify failed replies for a further locked stale-parent diagnosis."""
    return Q(source_platform='slack', operation='create', status='dead',
             metadata__permanent_failure=True, metadata__thread_ts__isnull=False,
             last_error__in=LEGACY_ERRORS)


def stale_parent_failure_audit(row, conversation):
    """Retain the diagnosed old mapping before source import can replace it."""
    from integrations.services import slack_dm_mirror as dm
    metadata = row.metadata or {}
    parent_ts = str(metadata.get('thread_ts') or '')
    if (row.operation != 'create' or row.source_platform != 'slack' or row.status != 'dead'
            or not metadata.get('permanent_failure') or row.last_error not in LEGACY_ERRORS
            or not parent_ts or parent_ts == row.source_message_id
            or metadata.get('participant_hash') != conversation.participant_hash
            or dm._backfill_delivery_is_outside_history_window(row)
            or not _owner_has_device(conversation)):
        return None
    parent = conversation.deliveries.select_for_update().filter(
        source_platform='slack', source_message_id=parent_ts, operation='create', status='completed',
    ).defer('encrypted_text').first()
    old = (parent.metadata or {}) if parent is not None else {}
    if (not old.get('participant_hash') or old['participant_hash'] == conversation.participant_hash
            or not old.get('destination_message_id')):
        return None
    return {'contract': 'retired_private_parent', 'error_code': 'adapter_http_400',
            'failed_at': row.updated_at.isoformat(), 'old_parent_delivery_id': parent.pk,
            'old_parent_boundary': old['participant_hash'],
            'old_parent_destination_id': old['destination_message_id'],
            'original_backfill': bool(metadata.get('backfill'))}


def preserve_failed_children(parent):
    """Capture old-parent evidence before a fresh observation replaces the row."""
    conversation = parent.conversation
    for child in conversation.deliveries.select_for_update().filter(
        stale_parent_failure_query(), metadata__thread_ts=parent.source_message_id,
        **{f'metadata__{CONTRACT_KEY}__isnull': True},
    ):
        child.conversation = conversation
        audit = stale_parent_failure_audit(child, conversation)
        if audit is None:
            continue
        child.metadata = {**(child.metadata or {}), 'backfill': True,
                          'history_recovery_scheduled': True, CONTRACT_KEY: audit}
        child.encrypted_text = ''
        child.save(update_fields=['metadata', 'encrypted_text', 'updated_at'])


def _valid_audit(audit):
    return bool(isinstance(audit, dict) and audit.get('contract') == 'retired_private_parent'
                and audit.get('error_code') == 'adapter_http_400'
                and audit.get('old_parent_delivery_id') and audit.get('old_parent_boundary')
                and audit.get('old_parent_destination_id'))


def stage_observed_reply(row, *, author_id, text, metadata):
    """Keep the failure fenced while holding only the freshly fetched body."""
    from integrations.services import slack_dm_mirror as dm
    original = row.metadata or {}
    audit = original.get(CONTRACT_KEY)
    epoch = metadata.get('history_scan_epoch')
    if (not _valid_audit(audit) or not original.get('history_recovery_scheduled')
            or row.last_error not in LEGACY_ERRORS or row.operation != 'create'
            or not epoch or metadata.get('participant_hash') != row.conversation.participant_hash
            or not row.conversation.deliveries.filter(source_platform='slack',
                source_message_id=dm.HISTORY_MAIN_STATE_ID, metadata__scan_epoch=epoch).exists()):
        return
    row.source_author_id = author_id
    row.metadata = {**metadata, 'backfill': True, 'permanent_failure': True,
                    'history_recovery_scheduled': True, CONTRACT_KEY: {**audit,
                        'fresh_source_text_sha256': hashlib.sha256(text.encode()).hexdigest()}}
    # The prior failure was erased before scheduling. This body is exclusively
    # from a new Slack read under the currently locked consent/registration.
    row.encrypted_text = text
    row.save(update_fields=['source_author_id', 'metadata', 'encrypted_text', 'updated_at'])


def _staged_rows(conversation):
    return conversation.deliveries.select_for_update().filter(
        source_platform='slack', operation='create', status='dead',
        metadata__permanent_failure=True, metadata__history_recovery_scheduled=True,
        **{f'metadata__{CONTRACT_KEY}__isnull': False},
    )


def _erase_staging(row):
    """Invalidate source evidence as well as its body, including empty bodies."""
    metadata = dict(row.metadata or {})
    audit = dict(metadata.get(CONTRACT_KEY) or {})
    for key in ('fresh_source_text_sha256', 'qualified_epoch', 'qualified_at', 'outcome'):
        audit.pop(key, None)
    audit['outcome'] = 'awaiting_fresh_source'
    metadata[CONTRACT_KEY] = audit
    metadata.pop('history_scan_epoch', None)
    row.metadata = metadata
    row.encrypted_text = ''
    row.save(update_fields=['metadata', 'encrypted_text', 'updated_at'])


def qualify_reply_recovery(conversation, *, scan_epoch, source_limited):
    """Qualify observations only when their full selected-window scan finishes."""
    from integrations.services import slack_dm_mirror as dm
    if (conversation.status != 'live' or conversation.grant.status != 'active'
            or conversation.grant.revoked_at is not None or not _owner_has_device(conversation)):
        for row in _staged_rows(conversation):
            _erase_staging(row)
        return
    for row in _staged_rows(conversation):
        metadata = dict(row.metadata or {})
        audit = metadata.get(CONTRACT_KEY)
        if not _valid_audit(audit):
            continue
        if source_limited or not scan_epoch:
            _erase_staging(row)
            continue
        row.conversation = conversation
        if metadata.get('participant_hash') != conversation.participant_hash:
            _erase_staging(row)
            continue
        observed = metadata.get('history_scan_epoch') == scan_epoch
        if observed and audit.get('fresh_source_text_sha256') != hashlib.sha256(row.encrypted_text.encode()).hexdigest():
            # Retention may erase a fenced body between source pages. Leave it
            # fenced until a later source read supplies the body again.
            continue
        metadata[CONTRACT_KEY] = {**audit, 'qualified_at': timezone.now().isoformat(),
                                 'qualified_epoch': scan_epoch,
                                 'outcome': 'waiting_for_current_parent' if observed else 'source_absent'}
        if not observed:
            metadata.pop('permanent_failure', None)
            metadata.pop('history_recovery_scheduled', None)
            metadata['history_recovery_superseded'] = True
            row.encrypted_text = ''
        row.metadata = metadata
        row.save(update_fields=['metadata', 'encrypted_text', 'updated_at'])
    if not source_limited and scan_epoch:
        release_qualified_replies(conversation)


def release_qualified_replies(conversation):
    """Release a qualified reply into current-boundary dependency handling."""
    from integrations.services import slack_dm_mirror as dm
    rows = _staged_rows(conversation).filter(
        **{f'metadata__{CONTRACT_KEY}__outcome': 'waiting_for_current_parent'},
    )
    if not rows.exists():
        return
    registration = dm._ensure_current_registration_row_locked(conversation, conversation.grant)
    if (conversation.status != 'live' or conversation.grant.status != 'active'
            or conversation.grant.revoked_at is not None or not _owner_has_device(conversation)
            or registration is None or dm._registration_state(registration) != dm.REGISTRATION_STATE_ACTIVE):
        for row in rows:
            _erase_staging(row)
        return
    for row in rows:
        row.conversation = conversation
        metadata = dict(row.metadata or {})
        audit = metadata.get(CONTRACT_KEY)
        if (not _valid_audit(audit) or row.last_error not in LEGACY_ERRORS
                or metadata.get('participant_hash') != conversation.participant_hash
                or not audit.get('qualified_epoch')
                or audit['qualified_epoch'] != metadata.get('history_scan_epoch')
                or audit.get('fresh_source_text_sha256') != hashlib.sha256(row.encrypted_text.encode()).hexdigest()):
            continue
        if dm._backfill_delivery_is_outside_history_window(row):
            dm._complete_outside_history_window_delivery_locked(row, now=timezone.now())
            continue
        parent_ts = str(metadata.get('thread_ts') or '')
        if parent_ts and parent_ts != row.source_message_id:
            if not dm._private_destination_message_id(conversation, parent_ts):
                if not dm._mlai_target_dependency_can_progress(conversation, parent_ts):
                    metadata.update({'original_thread_ts': parent_ts, 'thread_ts': '',
                                     'thread_parent_unavailable': True})
        metadata.pop('permanent_failure', None)
        metadata.pop('history_recovery_scheduled', None)
        metadata[CONTRACT_KEY] = {**audit, 'outcome': 'fresh_source_observed'}
        metadata['dependency_reconciliation_complete'] = True
        row.metadata = metadata
        row.status = 'pending'
        row.attempts = 0
        row.last_error = ''
        row.available_at = timezone.now()
        row.save(update_fields=['metadata', 'status', 'attempts', 'last_error', 'available_at', 'updated_at'])
