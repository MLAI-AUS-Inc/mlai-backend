"""One member-authorized public page, serialized with local privacy changes.

Reader selection/token refresh precede lower-order locks. The current User
cohort, grants, connections, devices and memberships remain locked through
INFO, history/replies and the atomic page commit. No member failure falls back
to the bot. This is a read fence; outgoing delivery fencing is Phase 4c.
"""
from contextvars import ContextVar
from dataclasses import replace
from datetime import timedelta
import hashlib
import json
import time

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from integrations.models import CommunityBridgeChannel, SlackDmMirrorConversation
from integrations.services import slack_dm_mirror as dm, slack_member_access as access
from integrations.services.community_bridge import member_audiences as audiences
from integrations.services.community_bridge.member_channels import member_channel_in_scope, queue_refresh
from integrations.services.slack_chat_catalog import _member_room_acknowledged, public_channels_enabled
from integrations.services.slack_member_membership_epoch import membership_floor
from .scheduler import BudgetDeferred, LeaseLost, finish_job, locked_job

_current = ContextVar("member_history_authority", default=None)
MAX_READER_ATTEMPTS = 3
POST_READ_ADMISSION_WAIT_SECONDS = 3


class MemberHistoryDeferred(BudgetDeferred):
    pass


class _SourceClosed(RuntimeError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__("member_history_source_closed")


def required(channel):
    """Members are never bot-readable, even during a flag-off rollback."""
    return channel.audience == "members" or getattr(settings, "SLACK_COMMUNITY_SOURCE", "bot") == "members"


def guard_page(channel):
    """Server-only assertion for repair helpers; callback payloads carry no proof."""
    if required(channel) and _current.get() != channel.pk:
        raise MemberHistoryDeferred(60)


def _identity(row):
    return (row.pk, row.slack_workspace_id, row.slack_channel_id, row.destination_platform,
            row.destination_workspace_id, row.destination_channel_id, row.audience)


def _source_ready(row):
    now = timezone.now()
    return bool(row.enabled and row.destination_platform == "buzz" and row.destination_channel_id
                and row.slack_visibility == "public" and not row.slack_archived and not row.slack_deleted
                and isinstance(row.room_state, dict) and row.mirror_decision == "mirror" and not row.hold_reason and not row.room_sync_pending
                and not row.room_sync_error and row.slack_seen_at
                and now - timedelta(seconds=audiences.SOURCE_FRESH_SECONDS) <= row.slack_seen_at <= now
                and not SlackDmMirrorConversation.objects.filter(
                    slack_workspace_id=row.slack_workspace_id, slack_conversation_id=row.slack_channel_id,
                ).exists())


def _scope(row, authority, grant, state_generation):
    floor = membership_floor(grant)
    # An observation refresh is not a new authority epoch. Consent/OAuth/floor,
    # mapping identity and applied audience changes do restart every cursor.
    value = [str(state_generation), _identity(row), authority.grant_id, authority.user_id, authority.connection_id,
             authority.workspace_id, authority.slack_user_id, authority.scopes, authority.consent_generation,
             authority.consent_version, authority.oauth_generation,
             floor.isoformat() if floor else None,
             (row.room_state or {}).get("source_reduction_observed_at"),
             str(row.audience_generation) if row.audience == "members" else None,
             row.audience_hash if row.audience == "members" else None]
    return hashlib.sha256(json.dumps(value, separators=(",", ":")).encode()).hexdigest()


class _Client:
    def __init__(self, lease, row, authority, connection):
        self.lease, self.row, self.authority, self.connection = lease, row, authority, connection
        self.started_at = None

    def _call(self, method, **kwargs):
        # Recheck time, not just row ownership: a long INFO request can consume
        # the remaining lease while its row is still locked.
        locked_job(self.lease)
        if kwargs.get("channel") != self.row.slack_channel_id:
            raise LeaseLost("member_history_source_changed")
        self.started_at = None
        def before_request():
            locked_job(self.lease)
            self.started_at = timezone.now()
        return dm._invoke_slack_with_locked_connection(
            self.authority, self.connection, method, before_request=before_request, **kwargs,
        )

    def verify_source(self):
        response = self._call("conversations_info", channel=self.row.slack_channel_id)
        channel = response.get("channel") if hasattr(response, "get") else None
        channel = channel if isinstance(channel, dict) else {}
        if channel.get("id") != self.row.slack_channel_id:
            raise MemberHistoryDeferred(60)
        if any(channel.get(key) for key in (
            "is_shared", "is_ext_shared", "is_org_shared", "is_pending_ext_shared", "pending_shared",
        )):
            raise _SourceClosed("slack_connect")
        if channel.get("is_private") is True or any(channel.get(key) is True for key in (
            "is_im", "is_mpim", "is_group",
        )):
            raise _SourceClosed("converted_to_private")
        if not hasattr(response, "get") or response.get("ok") is not True or not access._public_channel_response(response, self.row.slack_channel_id):
            # Missing/malformed evidence cannot relabel the source as private.
            raise MemberHistoryDeferred(60)
        if channel.get("is_archived"):
            raise _SourceClosed("archived")
        if channel.get("is_member") is False:
            raise dm.SlackApiError("member_history_not_joined", {"ok": False, "error": "not_in_channel"})
        if channel.get("is_member") is not True:
            raise MemberHistoryDeferred(60)

    def _history(self, method, **kwargs):
        response = self._call(method, **kwargs)
        if not hasattr(response, "get") or response.get("ok") is not True:
            raise MemberHistoryDeferred(60)
        # Only source metadata is classified; attachment/file privacy fields
        # describe the attachment and do not authorize/reclassify its channel.
        hints = [response]
        nested = response.get("channel")
        if isinstance(nested, dict):
            if nested.get("id") != self.row.slack_channel_id:
                raise MemberHistoryDeferred(60)
            hints.append(nested)
        elif nested is not None and nested != self.row.slack_channel_id:
            raise MemberHistoryDeferred(60)
        for hint in hints:
            if any(hint.get(key) for key in (
                "is_shared", "is_ext_shared", "is_org_shared", "is_pending_ext_shared", "pending_shared",
            )):
                raise _SourceClosed("slack_connect")
            if any(hint.get(key) for key in ("is_private", "is_im", "is_mpim", "is_group")):
                raise _SourceClosed("converted_to_private")
        # Slack history normally omits channel privacy flags. Re-prove the
        # source after the whole content call, before returning any body to the
        # page writer. External conversion after this proof remains possible.
        self._verify_after_read()
        return response

    def _verify_after_read(self):
        try:
            self.verify_source()
        except BudgetDeferred as error:
            # INFO shares its app/workspace gate with every reader. A fast
            # history response may finish inside the normal 1.2s interval.
            # Only a pre-request admission refusal gets one bounded wait; a
            # provider 429 has no before_request_method and is never retried.
            if (error.before_request_method != "conversations.info"
                    or error.retry_after > POST_READ_ADMISSION_WAIT_SECONDS):
                raise
            locked_job(self.lease)
            # admit_request closes its independent connection before raising.
            # Keep current local authority, but hold no budget connection here.
            time.sleep(error.retry_after)
            locked_job(self.lease)
            self.verify_source()  # a second deferral discards the page

    def conversations_history(self, **kwargs):
        return self._history("conversations_history", **kwargs)

    def conversations_replies(self, **kwargs):
        return self._history("conversations_replies", **kwargs)


def _defer(lease, row, reason, *, no_reader=False):
    # No User lock is acquired here or after these lower-order locks. Failure
    # recording/provider refresh have already completed outside this block.
    with transaction.atomic():
        locked_job(lease)
        current = CommunityBridgeChannel.objects.select_for_update().get(pk=row.pk)
        if _identity(current) != _identity(row):
            raise LeaseLost("member_history_mapping_changed")
        if no_reader:
            current.reader_grant = None
            current.sync_paused_reason = "no_reader"
            current.save(update_fields=("reader_grant", "sync_paused_reason", "updated_at"))
        finish_job(lease, checkpoint={} if no_reader else lease.checkpoint, delay_seconds=60)
    if access.enabled() and member_channel_in_scope(row.slack_workspace_id, row.slack_channel_id):
        queue_refresh(row.slack_workspace_id, row.slack_channel_id, reason=reason)


def run_page(lease, state, page):
    """Run one page under one exact reader/room/lease; bounded failover only."""
    original = state.public_channel
    if not access.enabled() or not member_channel_in_scope(original.slack_workspace_id, original.slack_channel_id):
        raise MemberHistoryDeferred(60)
    excluded = set()
    for _ in range(MAX_READER_ATTEMPTS):
        reader = access.reader_for_channel(original.slack_workspace_id, original.slack_channel_id,
                                           purpose="history", exclude_grant_ids=excluded)
        if reader is None:
            _defer(lease, original, "history_no_reader", no_reader=True)
            return
        # OAuth refresh may invalidate membership/room ACK. Do it before State,
        # then validate the resulting current authority under the full cohort.
        try:
            authority = dm._capture_slack_grant_api_authority(reader, refresh_token=True)
        except dm.SlackDmMirrorAuthorizationError:
            # No response authority was captured, so do not infer departure or
            # revoke a possibly replaced credential. Try another exact reader.
            excluded.add(reader.pk)
            try:
                access.cache.set(access._key(reader, original.slack_channel_id, "history"), "unavailable", timeout=60)
            except dm.SlackDmMirrorAuthorizationError:
                pass
            continue
        client = None
        try:
            cohort = audiences._cohort(original.slack_workspace_id, original.slack_channel_id) | {authority.user_id}
            if len(cohort) > audiences.MAX_AUTHORITY_USERS:
                raise MemberHistoryDeferred(60)
            with transaction.atomic():
                locked = audiences._lock_authority(cohort, original.slack_workspace_id, original.slack_channel_id)
                users, grants, connections, memberships = locked
                current_grant = next((g for g in grants if g.pk == authority.grant_id), None)
                current_connection = next((c for c in connections if c.pk == authority.connection_id), None)
                current_user = next((u for u in users if u.pk == authority.user_id), None)
                if current_grant is None or current_user is None or not current_user.is_active:
                    raise MemberHistoryDeferred(60)
                current_grant.user, current_grant.connection = current_user, current_connection
                if (not dm._slack_grant_api_authority_is_current(current_grant, current_connection, authority,
                        required_scopes={"channels:read", "channels:history"})
                        or dm.current_slack_oauth_generation(authority.user_id) != authority.oauth_generation
                        or not public_channels_enabled(current_grant, authority=authority)
                        or access._membership_state(current_grant, next((m for m in memberships
                            if m.grant_id == current_grant.pk), None)) != "joined"):
                    raise MemberHistoryDeferred(60)
                current_state, _ = locked_job(lease)
                row = CommunityBridgeChannel.objects.select_for_update().get(pk=original.pk)
                if (_identity(row) != _identity(original) or current_state.public_channel_id != row.pk
                        or current_state.workspace_id != row.slack_workspace_id
                        or current_state.source_channel_id != row.slack_channel_id
                        or not required(row)):
                    raise LeaseLost("member_history_mapping_changed")
                # Membership writers lock their User before Channel. Recheck
                # after Channel acquisition so no newly discovered User can
                # authorize content while absent from our sorted lock cohort.
                if not audiences._cohort(row.slack_workspace_id, row.slack_channel_id).issubset(cohort):
                    raise MemberHistoryDeferred(5)
                if not _source_ready(row):
                    raise MemberHistoryDeferred(60)
                if row.audience == "members":
                    eligible = audiences._eligible_grants(locked, row.slack_workspace_id)
                    keys = audiences._device_keys({grant.user_id for grant in eligible})
                    if keys is None or not _member_room_acknowledged(row, audiences.audience_hash(keys)):
                        raise MemberHistoryDeferred(5)
                row.reader_grant = current_grant
                row.sync_paused_reason = ""
                row.save(update_fields=("reader_grant", "sync_paused_reason", "updated_at"))
                client = _Client(lease, row, authority, current_connection)
                client.verify_source()
                scope = _scope(row, authority, current_grant, current_state.authority_generation)
                checkpoint = dict(lease.checkpoint) if lease.checkpoint.get("member_authority") == scope else {}
                checkpoint["member_authority"] = scope
                bound_lease = replace(lease, checkpoint=checkpoint)
                current_state.public_channel = row
                token = _current.set(row.pk)
                try:
                    page(bound_lease, current_state, client=client, member_authority=scope)
                finally:
                    _current.reset(token)
            return
        except _SourceClosed as error:
            from integrations.services.community_bridge.member_channels import mark_source_reduction
            if client is not None and client.started_at is not None:
                mark_source_reduction(original.slack_workspace_id, original.slack_channel_id,
                                      reason=error.reason, event_observed_at=client.started_at)
            raise MemberHistoryDeferred(60) from None
        except dm.SlackApiError as error:
            # The page transaction has ended: failure/revocation may safely
            # acquire User first and perform the existing audience reduction.
            access.record_reader_failure(reader, error, channel_id=original.slack_channel_id,
                authority=authority, purpose="history", observed_at=client.started_at if client else None)
            excluded.add(reader.pk)
            if original.audience == "members":
                # A departure disables the old room ACK. Its existing refresh
                # and full-audience lane must settle before a different reader.
                raise MemberHistoryDeferred(5) from None
            if not (dm._is_slack_auth_error(error) or str(error.response.get("error") or "")
                    in {"not_in_channel", "channel_not_found", "missing_scope", "no_permission", "access_denied"}):
                raise
        except MemberHistoryDeferred:
            _defer(lease, original, "history_authority_pending")
            return
    _defer(lease, original, "history_reader_retry")


def reject_legacy_bot_read(channel_id):
    """Legacy multi-page repairs may not read member-governed sources as bot."""
    for row in CommunityBridgeChannel.objects.filter(slack_channel_id=channel_id):
        if required(row):
            raise RuntimeError("Member-channel repair requires a durable member-authorized history job")


def reject_legacy_repair(channel):
    from django.core.management.base import CommandError
    if required(channel):
        raise CommandError("Member-channel repair requires durable member-authorized history jobs; direct replay is unavailable")


def invalidate_channel_history(workspace_id, channel_id):
    """Fence an observed leave/rejoin ABA before the pending Channel lock.

    Caller holds its membership User authority. Updating State first follows
    the same history order and supersedes in-flight leases/cursors atomically
    with the leave. Community bot/both history keeps its existing authority.
    """
    from integrations.models import BridgeSyncState

    if not transaction.get_connection().in_atomic_block:
        raise transaction.TransactionManagementError("History invalidation requires membership authority")
    rows = BridgeSyncState.objects.filter(public_channel__slack_workspace_id=workspace_id,
                                          public_channel__slack_channel_id=channel_id)
    if getattr(settings, "SLACK_COMMUNITY_SOURCE", "bot") != "members":
        rows = rows.filter(public_channel__audience="members")
    for state in rows.select_for_update(of=("self",)).order_by("pk"):
        if state.authority_generation >= 2**63 - 1:
            # A privacy reduction must commit even at the integer ceiling.
            # Revocation invalidates every lease without overflowing the field.
            state.status = "revoked"
            state.save(update_fields=("status",))
        else:
            state.authority_generation += 1
            state.save(update_fields=("authority_generation",))
        state.jobs.update(lease_token=None, lease_expires_at=None)
        state.jobs.filter(backoff_seconds=0, last_error_code="").update(due_at=timezone.now())


def request_history(user, mapping, public_key, request_key, progress):
    """Metadata-only scroll demand with the same current member authority scope.

    Members requesters must themselves hold a current joined grant and one of
    their admitted top-eight keys. Community demand retains community access.
    No token refresh/provider request occurs in this HTTP path.
    """
    from community_chat.models import CommunityChatDevice
    from integrations.models import BridgeSyncState
    from integrations.services.slack_owner_inventory_api import InventoryError
    from .history import ensure_state
    from .head_repair import head_scope
    from .scheduler import schedule_job

    if not access.enabled() or not member_channel_in_scope(mapping.slack_workspace_id, mapping.slack_channel_id):
        raise InventoryError("inventory_conversation_unavailable", 404)
    reader = access.reader_for_channel(mapping.slack_workspace_id, mapping.slack_channel_id, purpose="history")
    if reader is None:
        raise InventoryError("slack_history_unavailable", 409)
    authority = dm._capture_slack_grant_api_authority(reader, refresh_token=False)
    initial_state = ensure_state(mapping)
    cohort = audiences._cohort(mapping.slack_workspace_id, mapping.slack_channel_id) | {user.pk, authority.user_id}
    if len(cohort) > audiences.MAX_AUTHORITY_USERS:
        raise InventoryError("slack_history_unavailable", 409)
    with transaction.atomic():
        locked = audiences._lock_authority(cohort, mapping.slack_workspace_id, mapping.slack_channel_id)
        users, grants, connections, memberships = locked
        by_user, by_connection = {u.pk:u for u in users}, {c.pk:c for c in connections}
        selected = next((g for g in grants if g.pk == authority.grant_id), None)
        selected_connection = by_connection.get(authority.connection_id)
        if selected is None or authority.user_id not in by_user:
            raise InventoryError("slack_history_unavailable", 409)
        selected.user, selected.connection = by_user[authority.user_id], selected_connection
        if (not selected.user.is_active or not dm._slack_grant_api_authority_is_current(
                selected, selected_connection, authority, required_scopes={"channels:read", "channels:history"})
                or dm.current_slack_oauth_generation(authority.user_id) != authority.oauth_generation
                or not public_channels_enabled(selected, authority=authority)
                or access._membership_state(selected, next((m for m in memberships if m.grant_id==selected.pk),None)) != "joined"):
            raise InventoryError("slack_history_unavailable", 409)
        if (user.pk not in by_user or not by_user[user.pk].is_active or not CommunityChatDevice.objects.filter(
                user_id=user.pk, public_key=public_key, status="verified", revoked_at__isnull=True).exists()):
            raise InventoryError("device_unverified", 403)
        eligible = audiences._eligible_grants(locked, mapping.slack_workspace_id)
        if mapping.audience == "members" and (user.pk not in {g.user_id for g in eligible}
                or public_key not in dm._owner_device_pubkeys(user.pk, limit=8)):
            raise InventoryError("inventory_conversation_unavailable", 404)
        state = BridgeSyncState.objects.select_for_update().get(pk=initial_state.pk)
        job = state.jobs.select_for_update().get(pk=schedule_job(state,"archive").pk)
        row = CommunityBridgeChannel.objects.select_for_update().get(pk=mapping.pk)
        if (_identity(row) != _identity(mapping) or state.workspace_id != row.slack_workspace_id
                or state.source_channel_id != row.slack_channel_id or state.status in {"paused","revoked"}
                or not _source_ready(row) or not required(row)
                or not audiences._cohort(row.slack_workspace_id,row.slack_channel_id).issubset(cohort)):
            raise InventoryError("inventory_source_changed",409)
        if row.audience == "members":
            keys = audiences._device_keys({g.user_id for g in eligible})
            if keys is None or not _member_room_acknowledged(row,audiences.audience_hash(keys)):
                raise InventoryError("inventory_conversation_unavailable",404)
        state.public_channel = row
        authority_scope = _scope(row,authority,selected,state.authority_generation)
        return progress(state,request_key,scope=head_scope(state)+":"+authority_scope,
                        member_authority=authority_scope,job=job)
