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

from integrations.models import SlackDmMirrorConversation
from integrations.services.slack_chat_catalog import (
    ALL_HISTORY_CONSENT, conversation_kind, private_channels_enabled, raw_conversation_kind,
)
from integrations.services.slack_discovery_progress import conversation_progress
from .private_coverage import recent_conversations
from .scheduler import BudgetDeferred, LeaseLost

RECOVERY_RETRY_SECONDS = 120


def recover_recent_conversation(grant, authority, *, profile_cache, cycle_started_at):
    """Attempt one known recent room under the caller's fair discovery lease.

    Return False only when no recovery is due. Budget deferrals propagate so
    the directory worker resumes this room's metadata checkpoint next turn.
    The historical directory cursor is never reset by device recovery.
    """
    from integrations.services import slack_dm_mirror as dm

    from .device_audience import enabled as stable_private_rooms
    candidate_scope = SlackDmMirrorConversation.objects.filter(grant=grant)
    if not stable_private_rooms():
        candidate_scope = candidate_scope.filter(mlai_channel_id__isnull=True)
    candidates = recent_conversations(candidate_scope.filter(
        Q(status="provisioning") | Q(
            status="error", updated_at__lte=timezone.now() - timedelta(seconds=RECOVERY_RETRY_SECONDS),
        ),
    )).order_by("-coverage_activity", "id")
    for candidate in candidates:
        candidate.grant = grant
        kind = conversation_kind(candidate)
        if kind == "private_channel" and not private_channels_enabled(grant):
            continue
        if not dm._history_required_scopes(candidate.slack_conversation_id, kind=kind).issubset(
            set(grant.connection.scopes or [])
        ):
            continue
        break
    else:
        return False
    try:
        _recover(candidate, authority, profile_cache, cycle_started_at)
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
    return True


def _recover(candidate, authority, profile_cache, cycle_started_at):
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
        )
    if restored is not None:
        dm._drain_staged_events_for_conversation(authority, restored.pk)
    return True
