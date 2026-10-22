"""Existing owner consent, provider scopes and membership fence public Slack I/O.

Readers do not require a public attribution link. Posting additionally resolves
the verified device/account and its current Slack attribution link. Credentials
remain inside the existing grant-authority call path and never enter errors.
"""

from __future__ import annotations

from datetime import datetime, timedelta
import hashlib
import re

from django.conf import settings
from django.core.cache import cache
from django.core.exceptions import ObjectDoesNotExist
from django.db import transaction
from django.utils import timezone

from community_chat.models import CommunityChatDevice
from integrations.models import (
    CommunityBridgeChannel, CommunityBridgeIdentityLink, SlackChannelMembership,
    SlackDmMirrorGrant,
)
from integrations.services import slack_dm_mirror as dm
from integrations.services.message_sync.scheduler import BudgetDeferred
from integrations.services.slack_chat_catalog import public_channels_enabled

PROBE_SECONDS = 600
WORKSPACE_ID = re.compile(r"^T[A-Z0-9]{1,99}$")
CHANNEL_ID = re.compile(r"^C[A-Z0-9]{1,99}$")
PUBLIC_KEY = re.compile(r"^[0-9a-f]{64}$")
METHOD_SCOPES = {
    "conversations_info": frozenset({"channels:read"}),
    "conversations_history": frozenset({"channels:history"}),
    "conversations_replies": frozenset({"channels:history"}),
    "chat_postMessage": frozenset({"chat:write"}),
    "chat_update": frozenset({"chat:write"}),
    "chat_delete": frozenset({"chat:write"}),
    "reactions_add": frozenset({"reactions:write"}),
    "reactions_remove": frozenset({"reactions:write"}),
}
INFO_METHODS = frozenset({"conversations_info"})
HISTORY_METHODS = frozenset({"conversations_history", "conversations_replies"})
SAFE_PROVIDER_ERRORS = dm.SLACK_AUTH_ERROR_CODES | {
    "channel_not_found", "not_in_channel", "is_archived", "ratelimited",
    "restricted_action", "method_not_supported_for_channel_type", "request_timeout",
}


class MemberAccessDenied(dm.SlackDmMirrorAuthorizationError):
    """The current member authority cannot perform this operation."""


def enabled() -> bool:
    """Keep all new member-provider activity behind the deployment switch."""
    return bool(getattr(settings, "SLACK_MEMBER_CHANNELS_ENABLED", False)
                and getattr(settings, "SLACK_OWNER_INVENTORY_ENABLED", False)
                and getattr(settings, "MESSAGE_SYNC_ENABLED", False))


def _key(grant, channel_id, purpose):
    """Bind local observations to consent/OAuth identity without token material."""
    authority = dm._capture_slack_grant_api_authority(grant, refresh_token=False)
    identity = (authority.grant_id, authority.user_id, authority.connection_id,
                authority.workspace_id, authority.slack_user_id,
                authority.consent_generation, authority.consent_version,
                authority.oauth_generation, authority.scopes, channel_id, purpose)
    return "slack-member:" + hashlib.sha256(repr(identity).encode()).hexdigest()


def _membership(grant, channel_id, *, locked=False):
    rows = SlackChannelMembership.objects.filter(
        grant_id=grant.pk, slack_workspace_id=grant.slack_workspace_id,
        slack_channel_id=channel_id,
    )
    return (rows.select_for_update() if locked else rows).first()


def _membership_state(grant, row):
    from .slack_member_membership_epoch import membership_floor

    floor = membership_floor(grant)
    if row is None or row.observed_at is None or floor is None or row.observed_at < floor:
        return None
    if row.source == "probe":
        if row.observed_at < timezone.now() - timedelta(seconds=PROBE_SECONDS):
            return None
        # A probe under an old OAuth/consent epoch cannot authorize a new one.
        if cache.get(_key(grant, row.slack_channel_id, "membership")) != row.state:
            return None
    return row.state if row.state in {"joined", "left"} else None


class MemberSlackClient:
    """Scope-checking facade over the existing fenced, user-app budgeted SDK."""

    def __init__(self, grant):
        if (not enabled() or not WORKSPACE_ID.fullmatch(str(grant.slack_workspace_id or ""))
                or not public_channels_enabled(grant)):
            raise MemberAccessDenied("Member Slack access is unavailable.")
        self._authority = dm._capture_slack_grant_api_authority(grant)
        self._posting_public_key = getattr(grant, "_member_posting_public_key", None)

    def __repr__(self):
        return f"MemberSlackClient(grant_id={self._authority.grant_id})"

    def __getattr__(self, method):
        if method not in METHOD_SCOPES:
            raise AttributeError(method)
        return lambda **kwargs: self.call(method, **kwargs)

    def call(self, method, **kwargs):
        """Recheck live consent and joined authority immediately before I/O."""
        method = method.replace(".", "_")
        scopes = METHOD_SCOPES.get(method)
        if not enabled() or scopes is None or not scopes.issubset(self._authority.scopes):
            raise MemberAccessDenied("The Slack operation lacks current permission.")
        channel_id = kwargs.get("channel")
        from .community_bridge.member_channels import member_channel_in_scope
        if (not WORKSPACE_ID.fullmatch(self._authority.workspace_id)
                or not CHANNEL_ID.fullmatch(str(channel_id or ""))
                or not member_channel_in_scope(self._authority.workspace_id, channel_id)):
            raise MemberAccessDenied("A valid Slack channel is required.")
        grant = None
        started_at = None
        try:
            with transaction.atomic():
                grant, connection = dm._lock_slack_grant_api_authority(
                    self._authority, required_scopes=scopes,
                )
                grant.connection = connection
                if not public_channels_enabled(grant, authority=self._authority):
                    raise MemberAccessDenied("Member Slack consent is unavailable.")
                if method not in INFO_METHODS | HISTORY_METHODS and (
                    not self._posting_public_key
                    or not _poster_identity_current(grant, self._posting_public_key)
                ):
                    raise MemberAccessDenied("A current verified posting identity is required.")
                if method not in INFO_METHODS and _membership_state(
                    grant, _membership(grant, channel_id, locked=True),
                ) != "joined":
                    raise MemberAccessDenied("Current channel membership is required.")
                started_at = timezone.now()
                response = dm._call_slack_with_grant_authority(
                    self._authority, method, required_scopes=scopes, **kwargs,
                )
                if method == "conversations_info" and not _public_channel_response(response, channel_id):
                    raise MemberAccessDenied("The response is not an eligible public source.")
                return response
        except dm.SlackApiError as error:
            if grant is not None:
                purpose = "history" if method in HISTORY_METHODS else "info" if method in INFO_METHODS else "write"
                record_reader_failure(
                    grant, error, channel_id=channel_id, purpose=purpose, authority=self._authority,
                    observed_at=started_at,
                )
            raise


def member_client(grant) -> MemberSlackClient:
    """Return a client whose every supported method checks its own scopes."""
    return MemberSlackClient(grant)


def reader_for_channel(workspace_id, channel_id, *, purpose="history", exclude_grant_ids=()):
    """Select a reader; only public info may fall back to an unjoined grant.

    member_client records provider failures using its pre-request authority.
    Callers add the failed grant ID to exclude_grant_ids before retrying.
    BudgetDeferred is scheduling, not a grant failure. Selection makes no
    Slack requests and grants no room membership; the client fences history.
    """
    if purpose not in {"history", "info"}:
        raise ValueError("Reader purpose must be history or info")
    from .community_bridge.member_channels import member_channel_in_scope
    if not enabled() or not member_channel_in_scope(workspace_id, channel_id):
        return None
    excluded = set(exclude_grant_ids)
    grants = SlackDmMirrorGrant.objects.filter(
        slack_workspace_id=workspace_id, status="active", revoked_at__isnull=True,
        user__is_active=True,
    ).select_related("user", "connection")
    by_id = {grant.pk: grant for grant in grants}
    memberships = list(SlackChannelMembership.objects.filter(
        slack_workspace_id=workspace_id, slack_channel_id=channel_id,
        state="joined", grant_id__in=by_id,
    ).order_by("-observed_at", "grant_id"))
    joined = {row.grant_id: row for row in memberships}
    pointer = CommunityBridgeChannel.objects.filter(
        slack_workspace_id=workspace_id, slack_channel_id=channel_id,
    ).values_list("reader_grant_id", flat=True).first()
    preferred = ([pointer] if pointer in joined else []) + [row.grant_id for row in memberships]
    if purpose == "info":
        preferred += sorted(by_id)
    seen = set()
    for grant_id in preferred:
        if grant_id in seen or grant_id in excluded:
            continue
        seen.add(grant_id)
        grant = by_id[grant_id]
        if not public_channels_enabled(grant) or cache.get(_key(grant, channel_id, purpose)):
            continue
        if purpose == "history" and _membership_state(grant, joined.get(grant_id)) != "joined":
            continue
        return grant
    return None


def record_reader_failure(grant, error, *, channel_id, authority, purpose="history", observed_at=None):
    """Record content-free failure/exclusion; revoke rejected auth durably.

    A transport error must not cause an unacknowledged write to switch actors.
    Only readers get temporary failover exclusions; write retry/actor binding
    remains the delivery owner's responsibility.
    """
    if isinstance(error, (BudgetDeferred, dm.SlackDmMirrorRateLimited)):
        return
    # Require the pre-request authority explicitly. An old response must never
    # capture and revoke a newly linked credential during failure handling.
    with transaction.atomic():
        try:
            current, connection = dm._lock_slack_grant_api_authority(authority, required_scopes=set())
        except (dm.SlackDmMirrorAuthorizationError, ObjectDoesNotExist):
            return
        current.connection = connection
        if dm._is_slack_auth_error(error):
            # Retain the exact-authority user fence through the full existing
            # privacy revoke, including its nested local transaction.
            dm.revoke_user_grant(current.user)
            return
        raw = str(error.response.get("error") or "") if isinstance(error, dm.SlackApiError) else ""
        code = raw if raw in SAFE_PROVIDER_ERRORS else "request_failed"
        if (code == "not_in_channel" and isinstance(observed_at, datetime)
                and timezone.is_aware(observed_at)):
            row = _membership(current, channel_id, locked=True)
            if row is not None:
                # This request predates a newer observation. Its negative
                # result cannot overwrite that join or its reader diagnostics.
                if row.observed_at > observed_at:
                    return
                from .slack_channel_membership import mark_left

                if mark_left(current, channel_id, observed_at=observed_at,
                             source="probe", authority=authority):
                    # Bind the negative probe to exact OAuth/consent authority
                    # before its after-commit audience reduction runs.
                    cache.set(_key(current, channel_id, "membership"), "left",
                              timeout=PROBE_SECONDS)
        if purpose in {"history", "info"}:
            cache.set(_key(current, channel_id, purpose), True, timeout=60)
        SlackDmMirrorGrant.objects.filter(pk=current.pk, status="active").update(
            last_error=f"member_access:{code}", updated_at=timezone.now(),
        )
        CommunityBridgeChannel.objects.filter(
            slack_workspace_id=current.slack_workspace_id, slack_channel_id=channel_id,
            reader_grant_id=current.pk,
        ).update(reader_grant=None)


def _poster_identity_current(grant, public_key):
    return CommunityChatDevice.objects.select_for_update().filter(
        user_id=grant.user_id, public_key=public_key, status="verified",
        revoked_at__isnull=True, user__is_active=True,
    ).only("pk").first() is not None and CommunityBridgeIdentityLink.objects.select_for_update().filter(
        user_id=grant.user_id, slack_workspace_id=grant.slack_workspace_id,
        slack_user_id=grant.slack_user_id, revoked_at__isnull=True,
    ).only("pk").first() is not None


def _public_channel_response(response, channel_id):
    try:
        raw = response.get("channel")
    except (AttributeError, TypeError):
        return False
    return bool(
        isinstance(raw, dict) and raw.get("id") == channel_id
        and raw.get("is_private") is False and raw.get("is_channel") is True
        and not any(raw.get(key) for key in ("is_im", "is_mpim", "is_group"))
        and not any(raw.get(key) for key in (
            "is_shared", "is_ext_shared", "is_org_shared", "is_pending_ext_shared", "pending_shared",
        ))
    )


def _probe_membership(grant, channel_id, public_key):
    authority = dm._capture_slack_grant_api_authority(grant)
    started = timezone.now()
    response = member_client(grant).conversations_info(channel=channel_id)
    raw = response.get("channel")
    if not _public_channel_response(response, channel_id):
        return None
    if not isinstance(raw.get("is_member"), bool):
        return None
    state = "joined" if raw["is_member"] else "left"
    with transaction.atomic():
        current, connection = dm._lock_slack_grant_api_authority(authority, required_scopes={"channels:read"})
        current.connection = connection
        if not public_channels_enabled(current, authority=authority) or not _poster_identity_current(current, public_key):
            return None
        row = _membership(current, channel_id, locked=True)
        if row is not None and row.observed_at >= started:
            return _membership_state(current, row)
        from .slack_channel_membership import mark_joined, mark_left

        # The request's start, rather than response time, cannot defeat a
        # newer signed leave. The service persists the matching audience wake.
        changed = (mark_joined if state == "joined" else mark_left)(
            current, channel_id, observed_at=started, source="probe",
            authority=authority, public_verified=True,
        )
        if not changed:
            return _membership_state(current, _membership(current, channel_id, locked=True))
        cache.set(_key(current, channel_id, "membership"), state, timeout=PROBE_SECONDS)
    return state


def _poster_for_author(pubkey, workspace_id, channel_id):
    """Resolve a verified author's own grant; unknown membership gets one probe."""
    key = str(pubkey or "").strip().lower()
    from .community_bridge.member_channels import member_channel_in_scope
    if not enabled() or not PUBLIC_KEY.fullmatch(key) or not member_channel_in_scope(workspace_id, channel_id):
        return None
    device = CommunityChatDevice.objects.filter(
        public_key=key, status="verified", revoked_at__isnull=True, user__is_active=True,
    ).first()
    if device is None:
        return None
    link = CommunityBridgeIdentityLink.objects.filter(
        user_id=device.user_id, slack_workspace_id=workspace_id, revoked_at__isnull=True,
    ).first()
    if link is None:
        return None
    grant = SlackDmMirrorGrant.objects.filter(
        user_id=device.user_id, slack_workspace_id=workspace_id,
        slack_user_id=link.slack_user_id, status="active", revoked_at__isnull=True,
    ).select_related("user", "connection").first()
    if grant is None or not public_channels_enabled(grant):
        return None
    authority = dm._capture_slack_grant_api_authority(grant)
    with transaction.atomic():
        current, connection = dm._lock_slack_grant_api_authority(authority, required_scopes={"chat:write"})
        current.connection = connection
        if not public_channels_enabled(current, authority=authority) or not _poster_identity_current(current, key):
            return None
        state = _membership_state(current, _membership(current, channel_id, locked=True))
    if state is None:
        state = _probe_membership(current, channel_id, key)
    if state != "joined":
        return None
    # Probes and cached positives remain subject to a final current-device,
    # consent and membership fence. The eventual write checks membership again.
    with transaction.atomic():
        current, connection = dm._lock_slack_grant_api_authority(authority, required_scopes={"chat:write"})
        current.connection = connection
        if (not public_channels_enabled(current, authority=authority)
                or not _poster_identity_current(current, key)
                or _membership_state(current, _membership(current, channel_id, locked=True)) != "joined"):
            return None
        current._member_posting_public_key = key
        return current


def poster_for_author(pubkey, workspace_id, channel_id):
    """Return current posting authority, or None after a consent/device race.

    Provider failures and budget deferrals propagate: callers must not silently
    switch the actor of a write after an uncertain upstream result.
    """
    try:
        return _poster_for_author(pubkey, workspace_id, channel_id)
    except dm.SlackDmMirrorAuthorizationError:
        return None
