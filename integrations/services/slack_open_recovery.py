"""Review and queue one existing unfinished mirror through the owner-open worker."""

import hashlib
import json

from django.db import transaction

from community_chat.models import CommunityChatDevice
from integrations.models import SlackDmMirrorConversation, SlackDmMirrorGrant
from integrations.services import slack_dm_mirror as dm
from integrations.services.slack_chat_catalog import private_channels_enabled
from integrations.services.slack_open_requests import enqueue_open_locked
from integrations.services.slack_owner_inventory import device_epoch, has_metadata_consent, state_for
from integrations.services.slack_owner_inventory_api import InventoryError, _authorized


def recover_existing_conversation(*, grant_id, device_id, source_id, apply=False, expected_plan=None):
    """Default to a content-free dry run; an exact reviewed plan permits enqueue.

    This never calls Slack or the relay, resets read markers, or provisions a
    second channel. The existing worker owns source validation, bounded retries,
    registration reconciliation, history consent and eventual publication.
    """
    grant = SlackDmMirrorGrant.objects.select_related('user', 'connection').filter(
        pk=grant_id, status='active', revoked_at__isnull=True,
    ).first()
    if grant is None:
        raise InventoryError('slack_grant_unavailable', 404)
    device = CommunityChatDevice.objects.filter(
        pk=device_id, user_id=grant.user_id, status='verified', revoked_at__isnull=True,
    ).first()
    if device is None:
        raise InventoryError('device_unverified', 403)
    current, authority, authorized_device, _ = _authorized(grant.user, device.public_key)
    if current.pk != grant_id or authorized_device.pk != device_id:
        raise InventoryError('slack_authority_changed', 409)

    with transaction.atomic():
        grant, connection = dm._lock_slack_grant_api_authority(authority, required_scopes={'im:read'})
        if grant.pk != grant_id or not has_metadata_consent(connection, authority):
            raise InventoryError('slack_authority_changed', 409)
        conversation = SlackDmMirrorConversation.objects.select_for_update().filter(
            grant_id=grant_id, slack_conversation_id=source_id,
        ).first()
        if (conversation is None or conversation.mlai_channel_id is not None
                or conversation.status not in {'provisioning', 'error'}):
            raise InventoryError('conversation_not_unprovisioned', 409)
        device = CommunityChatDevice.objects.select_for_update().filter(
            pk=device_id, user_id=grant.user_id, public_key=device.public_key,
            verified_at=device.verified_at, status='verified', revoked_at__isnull=True,
        ).first()
        if device is None:
            raise InventoryError('device_unverified', 403)
        row = grant.owner_conversation_inventory.filter(slack_conversation_id=source_id).first()
        if row is None or row.eligibility != 'eligible' or row.kind not in {'im', 'mpim', 'private_channel'}:
            raise InventoryError('inventory_conversation_unavailable', 409)
        if row.kind == 'private_channel' and not private_channels_enabled(grant):
            raise InventoryError('inventory_consent_required', 403)
        days = dm._grant_history_days(grant)
        if (days and row.source_archived is True) or row.source_is_open is False:
            raise InventoryError('inventory_conversation_unavailable', 409)
        if not dm._history_required_scopes(source_id, kind=row.kind).issubset(set(connection.scopes or [])):
            raise InventoryError('slack_authority_changed', 403)
        # Changes to authority or the reviewed mirror require another dry run.
        # Names, message bodies, token material and public device keys are absent.
        scope = {
            'grant_id': grant_id, 'device_id': device_id, 'source_id': source_id,
            'conversation_id': conversation.pk, 'status': conversation.status,
            'history_days': days, 'kind': row.kind,
            'participant_hash': conversation.participant_hash,
            'epoch': device_epoch(grant, authority, device, state=state_for(connection, authority)),
        }
        fingerprint = hashlib.sha256(json.dumps(scope, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        report = {
            key: value for key, value in scope.items()
            if key not in {'epoch', 'participant_hash'}
        }
        report.update(apply=bool(apply), plan=fingerprint, action='queue_existing_owner_open', enqueued=False)
        if apply:
            if not expected_plan or expected_plan != fingerprint:
                raise InventoryError('recovery_plan_changed', 409)
            result = enqueue_open_locked(grant, connection, authority, device, row)
            report.update(enqueued=True, state=result['state'], retry_after_seconds=result['retry_after_seconds'])
        return report
