"""Recover recent owner conversations before scanning the historical directory.

A revoked device correctly fences its old private room. The account's source
inventory survives that fence, so recovery must not wait for users.conversations
to rediscover thousands of old rooms. Every repair still obtains fresh Slack
membership and runs the existing registration/consent authority checks.
"""
from datetime import timedelta

from slack_sdk.errors import SlackApiError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from integrations.models import ExternalServiceConnection, SlackDmMirrorConversation, SlackDmMirrorGrant
from integrations.services.slack_chat_catalog import (
    ALL_HISTORY_CONSENT, conversation_kind, private_channels_enabled, raw_conversation_kind,
)
from integrations.services.slack_discovery_progress import conversation_progress
from .private_coverage import recent_conversations
from .scheduler import BudgetDeferred, LeaseLost

RECOVERY_RETRY_SECONDS = 120
DEVICE_AUDIENCE_HINT = "message_sync_device_audience"


def lock_enrollment_recovery_grants(user):
    """Lock account-owned recovery state before enrollment takes a device lock.

    The caller holds the user lock and the enrollment transaction. Taking
    grant/connection locks here preserves the device-revocation lock order.
    """
    from django.conf import settings
    from .device_audience import enabled as stable_private_rooms

    if not getattr(settings, "MESSAGE_SYNC_ENABLED", False) or not stable_private_rooms():
        return []
    grants = list(SlackDmMirrorGrant.objects.select_for_update().filter(
        user_id=user.pk, status="active", revoked_at__isnull=True,
    ).order_by("id"))
    connections = {row.pk: row for row in ExternalServiceConnection.objects.select_for_update().filter(
        pk__in=[grant.connection_id for grant in grants], user_id=user.pk, provider="slack",
        status__in=["connected", "syncing"],
    ).only("id", "user_id", "sync_cursor").order_by("id")}
    eligible = []
    for grant in grants:
        if grant.connection_id in connections:
            grant.connection = connections[grant.connection_id]
            eligible.append(grant)
    return eligible


def schedule_enrollment_recovery(grants, device):
    """Persist a bounded device hint with verification, without provider I/O.

    The latest confirmed device has priority when an account exceeds the relay
    audience limit. One room is repaired per existing fair discovery turn; all
    other verified owner devices still enter that room up to its normal limit.
    Existing rooms retain their status and remain usable by their old audience.
    """
    if device.status != "verified" or device.revoked_at is not None:
        return
    from .discovery import KEY as discovery_key

    for grant in grants:
        if grant.user_id != device.user_id:
            continue
        connection = grant.connection
        cursor = dict(connection.sync_cursor or {})
        hint = cursor.get(DEVICE_AUDIENCE_HINT) or {}
        if hint.get("public_key") != device.public_key:
            cursor[DEVICE_AUDIENCE_HINT] = {"public_key": device.public_key, "retry_after": {}}
        discovery = dict(cursor.get(discovery_key) or {})
        # Keep an in-flight lease and fairness position; only wake a future turn.
        discovery["due"] = min(float(discovery.get("due") or 0), timezone.now().timestamp())
        cursor[discovery_key] = discovery
        connection.sync_cursor = cursor
        connection.save(update_fields=["sync_cursor", "updated_at"])
        grant.last_discovery_at = None
        grant.save(update_fields=["last_discovery_at", "updated_at"])


def _update_device_hint(authority, public_key, *, failed_conversation=None):
    from integrations.services import slack_dm_mirror as dm

    with transaction.atomic():
        _, connection = dm._lock_slack_grant_api_authority(authority, required_scopes=dm.DIRECT_DM_SCOPES)
        cursor = dict(connection.sync_cursor or {})
        hint = dict(cursor.get(DEVICE_AUDIENCE_HINT) or {})
        if hint.get("public_key") != public_key:
            return  # A newer enrollment owns this hint now.
        if failed_conversation is None:
            cursor.pop(DEVICE_AUDIENCE_HINT, None)
        else:
            now = timezone.now().timestamp()
            retries = {key: value for key, value in (hint.get("retry_after") or {}).items()
                       if isinstance(value, (int, float)) and value > now}
            retries[str(failed_conversation)] = now + RECOVERY_RETRY_SECONDS
            hint["retry_after"] = dict(list(retries.items())[-256:])
            cursor[DEVICE_AUDIENCE_HINT] = hint
        connection.sync_cursor = cursor
        connection.save(update_fields=["sync_cursor", "updated_at"])


def recover_recent_conversation(grant, authority, *, profile_cache, cycle_started_at):
    """Attempt one known recent room under the caller's fair discovery lease.

    Return False when no recovery is due, or when a cooling-down recovery can
    yield its turn to an incomplete consented owner directory. Budget deferrals
    propagate so the worker resumes this room's metadata checkpoint next turn.
    Device recovery never resets the directory cursor.
    """
    from integrations.services import slack_dm_mirror as dm

    from .device_audience import enabled as stable_private_rooms
    hint = (grant.connection.sync_cursor or {}).get(DEVICE_AUDIENCE_HINT) or {}
    requested_key = str(hint.get("public_key") or "") if stable_private_rooms() else ""
    if requested_key and dm._active_verified_device(grant.user_id, requested_key) is None:
        _update_device_hint(authority, requested_key)
        requested_key = ""
    candidate_scope = SlackDmMirrorConversation.objects.filter(grant=grant)
    if not stable_private_rooms():
        candidate_scope = candidate_scope.filter(mlai_channel_id__isnull=True)
    recovery_scope = Q(status="provisioning") | Q(
            status="error", updated_at__lte=timezone.now() - timedelta(seconds=RECOVERY_RETRY_SECONDS),
        )
    if requested_key:
        recovery_scope |= Q(status="live", mlai_channel_id__isnull=False) | Q(status="error")
    candidates = recent_conversations(candidate_scope.filter(recovery_scope)).order_by("-coverage_activity", "id")
    waiting_for_retry = False
    for candidate in candidates:
        candidate.grant = grant
        kind = conversation_kind(candidate)
        if kind == "private_channel" and not private_channels_enabled(grant):
            continue
        if not dm._history_required_scopes(candidate.slack_conversation_id, kind=kind).issubset(
            set(grant.connection.scopes or [])
        ):
            continue
        if candidate.status == "live" and requested_key in (candidate.participant_buzz_pubkeys or []):
            continue
        if candidate.status == "error" and candidate.updated_at > timezone.now() - timedelta(seconds=RECOVERY_RETRY_SECONDS):
            waiting_for_retry = True
            continue
        retry_at = (hint.get("retry_after") or {}).get(str(candidate.pk), 0)
        if requested_key and isinstance(retry_at, (int, float)) and retry_at > timezone.now().timestamp():
            waiting_for_retry = True
            continue
        break
    else:
        if requested_key and not waiting_for_retry:
            _update_device_hint(authority, requested_key)
        if waiting_for_retry:
            from integrations.services.slack_owner_inventory import needs_private_sweep

            # Advance the initial private inventory during a recovery cooldown,
            # but keep the old idle behavior once that sweep is complete so a
            # retry hint does not cause repeated full Slack directory scans.
            return not needs_private_sweep(grant.connection, authority)
        return False
    try:
        _recover(candidate, authority, profile_cache, cycle_started_at, required_owner_public_key=requested_key or None)
    except Exception as exc:
        if (isinstance(exc, (BudgetDeferred, LeaseLost, dm.SlackDmMirrorAuthorizationError,
                             dm.SlackDmMirrorRateLimited))
                or dm._is_slack_auth_error(exc) or dm._slack_retry_after_seconds(exc)):
            raise
        # A malformed room or timed-out registration must not block later
        # rooms, nor wait for a historical directory pass to be retried. Refresh
        # the durable cooldown even if the registration ledger already marked
        # the attempt errored. Its existing ambiguous-attempt cleanup still
        # fences every retry before provisioning.
        with transaction.atomic():
            dm._lock_slack_grant_api_authority(authority, required_scopes=dm.DIRECT_DM_SCOPES)
            candidate_scope.filter(pk=candidate.pk, status__in=["provisioning", "error"]).update(
                status="error", last_error=f"Device recovery: {type(exc).__name__}", updated_at=timezone.now())
        if requested_key:
            _update_device_hint(authority, requested_key, failed_conversation=candidate.pk)
    return True


def _recover(candidate, authority, profile_cache, cycle_started_at, *, required_owner_public_key=None):
    from integrations.services import slack_dm_mirror as dm

    grant = candidate.grant
    kind = conversation_kind(candidate)
    scopes = dm._history_required_scopes(candidate.slack_conversation_id, kind=kind)
    try:
        response = dm._call_slack_with_grant_authority(
            authority, "conversations_info", required_scopes=scopes,
            channel=candidate.slack_conversation_id,
        )
    except BudgetDeferred as exc:
        # No provider request ran. Retain this owner's place in the fair queue.
        exc.discovery_admission_deferred = bool(exc.before_request_method)
        raise
    except SlackApiError as exc:
        if exc.response.get("error") not in {"channel_not_found", "not_in_channel"}:
            raise
        dm._retire_ineligible_from_slack_response(
            authority, candidate.slack_conversation_id,
            reason=dm.SLACK_CONVERSATION_UNAVAILABLE_REASON, required_scopes=scopes,
        )
        return True
    raw = response.get("channel") or {}
    if raw.get("id") != candidate.slack_conversation_id:
        raise dm.SlackDmMirrorUpstreamError("Slack returned a different recovery conversation.")
    if raw_conversation_kind(raw) != kind:
        raise dm.SlackDmMirrorUpstreamError("Slack returned a different recovery conversation type.")
    if dm._is_external_shared_conversation(raw):
        reason = dm.SLACK_CONNECT_INELIGIBLE_REASON
    elif (raw.get("is_archived") and grant.consent_version != ALL_HISTORY_CONSENT) or (
        kind == "private_channel" and raw.get("is_member") is False
    ):
        reason = dm.SLACK_CONVERSATION_UNAVAILABLE_REASON
    else:
        reason = None
    if reason:
        dm._retire_ineligible_from_slack_response(
            authority, candidate.slack_conversation_id, reason=reason,
            required_scopes=scopes,
        )
        return True
    # The stored timestamp chooses work, never grants membership. Fresh source
    # members and verified owner devices determine the replacement audience.
    with conversation_progress(authority, candidate.slack_conversation_id, kind, cycle_started_at):
        restored = dm._discover_conversation(
            grant, authority, raw, profile_cache=profile_cache,
            force_backfill=False, reset_history=False,
            activity_seconds=int(candidate.coverage_activity),
            recent_activity=True, check_recent_activity=False,
            required_owner_public_key=required_owner_public_key,
        )
    if restored is not None:
        dm._drain_staged_events_for_conversation(authority, restored.pk)
    return True
