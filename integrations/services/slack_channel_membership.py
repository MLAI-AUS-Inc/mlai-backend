"""Ordered public Slack membership observations, never provider or relay I/O.

``public_verified`` is an internal caller assertion after authoritative privacy
classification, never a field copied from a callback. Channel IDs alone prove
neither privacy nor membership. Audience mutation remains the room service's
responsibility; every accepted observation leaves its durable pending wake in
the same authority transaction.
"""

from datetime import datetime, timedelta

from django.contrib.auth import get_user_model
from django.core.exceptions import ObjectDoesNotExist
from django.db import transaction
from django.utils import timezone

from integrations.models import (
    CommunityBridgeChannel, ExternalServiceConnection, SlackChannelMembership,
    SlackChannelMembershipSource, SlackDmMirrorGrant,
)
from integrations.services.slack_chat_catalog import PUBLIC_CHANNEL_SCOPES, public_channels_enabled


def _valid_observation(observed_at, source):
    return bool(
        source in SlackChannelMembershipSource.values
        and isinstance(observed_at, datetime) and timezone.is_aware(observed_at)
        and observed_at <= timezone.now() + timedelta(minutes=5)
    )


def _identity_valid(grant, channel_id):
    from .community_bridge.member_channels import valid_slack_channel_identity

    return valid_slack_channel_identity(grant.slack_workspace_id, channel_id)


def _locked(grant, authority, *, joined):
    """Use exact captured authority for additions and all provider responses.

    A signed leave may also reduce a paused/revoked grant. Its subject is bound
    to the original user/connection/Slack identity while taking the same lock
    order, without requiring a still-usable token or new provider permission.
    """
    from . import slack_dm_mirror as dm
    from .message_sync.member_callback_authority import assert_callback_user_locked

    assert_callback_user_locked(grant.user_id)

    if joined or authority is not None:
        authority = authority or dm._capture_slack_grant_api_authority(grant, refresh_token=False)
        if (authority.grant_id, authority.user_id, authority.connection_id,
                authority.workspace_id, authority.slack_user_id) != (
                grant.pk, grant.user_id, grant.connection_id,
                grant.slack_workspace_id, grant.slack_user_id):
            return None, None
        current, connection = dm._lock_slack_grant_api_authority(
            authority, required_scopes=PUBLIC_CHANNEL_SCOPES if joined else set(),
        )
        current.connection = connection
        return current, authority
    get_user_model().objects.select_for_update().get(pk=grant.user_id)
    rows = list(SlackDmMirrorGrant.objects.select_for_update().filter(
        user_id=grant.user_id,
    ).order_by("id"))
    current = next((row for row in rows if row.pk == grant.pk), None)
    if current is None or any(getattr(current, name) != getattr(grant, name) for name in (
        "connection_id", "slack_workspace_id", "slack_user_id",
    )):
        return None, None
    connection = ExternalServiceConnection.objects.select_for_update().filter(
        pk=current.connection_id, user_id=current.user_id, provider="slack",
    ).first()
    if connection is None:
        return None, None
    current.connection = connection
    return current, None


def _pending(grant, channel_id, *, reduction):
    from .community_bridge.member_channels import mark_member_channels_pending

    if reduction:
        from .message_sync.member_history import invalidate_channel_history
        invalidate_channel_history(grant.slack_workspace_id, channel_id)
    mark_member_channels_pending(
        grant.user_id, reason="membership_left" if reduction else "membership_joined",
        channel_ids=[channel_id], reduction=reduction,
    )


def _apply(grant, channel_id, *, observed_at, source, state, authority=None, public_verified=False):
    from . import slack_dm_mirror as dm
    from . import slack_member_access as access
    from .community_bridge.member_channels import member_channel_in_scope

    joined = state == "joined"
    if not _identity_valid(grant, channel_id) or not _valid_observation(observed_at, source):
        return False
    if joined and (public_verified is not True or not access.enabled()
                   or not member_channel_in_scope(grant.slack_workspace_id, channel_id)):
        return False
    try:
        with transaction.atomic():
            current, captured = _locked(grant, authority, joined=joined)
            if current is None or observed_at < current.consented_at:
                return False
            if joined:
                from .slack_member_membership_epoch import membership_floor

                floor = membership_floor(current)
                if floor is None or observed_at < floor:
                    return False
            if joined and not public_channels_enabled(current, authority=captured):
                return False
            row = SlackChannelMembership.objects.select_for_update().filter(
                grant=current, slack_channel_id=channel_id,
            ).first()
            if row is not None and row.slack_workspace_id != current.slack_workspace_id:
                return False
            if row is not None and (observed_at < row.observed_at or (
                observed_at == row.observed_at and (row.state == "left" or joined)
            )):
                return False
            if not joined and row is None and not CommunityBridgeChannel.objects.filter(
                slack_workspace_id=current.slack_workspace_id, slack_channel_id=channel_id,
                audience="members", destination_platform="buzz",
            ).exclude(destination_channel_id="").exists():
                # Keep a proven pre-room leave so a delayed older join cannot
                # later add its subject. Rollback reductions never discover
                # arbitrary unknown sources while expansion is disabled.
                if (public_verified is not True or not access.enabled()
                        or not member_channel_in_scope(current.slack_workspace_id, channel_id)
                        or not public_channels_enabled(current, authority=captured)):
                    return False
            # A newer message orders future leave/join observations, but does
            # not change an already-current audience. An expired probe or an
            # observation before the current epoch still needs a full wake.
            unchanged_message_join = bool(joined and source == "message" and row is not None
                                          and access._membership_state(current, row) == "joined")
            values = dict(state=state, source=source, observed_at=observed_at,
                          left_at=None if joined else observed_at)
            if row is None:
                SlackChannelMembership.objects.create(
                    grant=current, slack_workspace_id=current.slack_workspace_id,
                    slack_channel_id=channel_id, **values,
                )
            else:
                for name, value in values.items():
                    setattr(row, name, value)
                row.save(update_fields=tuple(values))
            if not unchanged_message_join:
                _pending(current, channel_id, reduction=not joined)
            return True
    except (dm.SlackDmMirrorError, ObjectDoesNotExist):
        return False


def mark_joined(grant, channel_id, *, observed_at, source="event", authority=None, public_verified=False):
    """Record one current proven public join; additions obey flags and scope."""
    return _apply(grant, channel_id, observed_at=observed_at, source=source,
                  state="joined", authority=authority, public_verified=public_verified)


def mark_left(grant, channel_id, *, observed_at, source="event", authority=None, public_verified=False):
    """Record a known-source reduction even when expansion is disabled."""
    return _apply(grant, channel_id, observed_at=observed_at, source=source,
                  state="left", authority=authority, public_verified=public_verified)


def reconcile_from_inventory(grant, seen_ids, complete, *, started_at, authority):
    """Apply only a complete exact-authority sweep, at its original start.

    The collector supplies only exact public objects with ``is_member=True``.
    Missing IDs prove departure only within the current expansion cohort. An
    old sweep cannot defeat a later event, including an equal-time leave.
    """
    from . import slack_dm_mirror as dm
    from . import slack_member_access as access
    from .community_bridge.member_channels import member_channel_in_scope

    counts = {"joined": 0, "left": 0}
    if complete is not True or not access.enabled() or not _valid_observation(started_at, "inventory"):
        return counts
    if isinstance(seen_ids, (str, bytes)):
        return counts
    try:
        seen = set(seen_ids)
    except TypeError:
        return counts
    if any(not isinstance(value, str) or not _identity_valid(grant, value) for value in seen):
        return counts
    try:
        with transaction.atomic():
            current, captured = _locked(grant, authority, joined=True)
            if current is None or started_at < current.consented_at or not public_channels_enabled(current, authority=captured):
                return counts
            from .slack_member_membership_epoch import membership_floor

            floor = membership_floor(current)
            if floor is None or started_at < floor:
                return counts
            previous = set(SlackChannelMembership.objects.filter(
                grant=current, slack_workspace_id=current.slack_workspace_id, state="joined",
            ).values_list("slack_channel_id", flat=True))
            for channel_id in sorted(seen | previous):
                if not member_channel_in_scope(current.slack_workspace_id, channel_id):
                    continue
                joined = channel_id in seen
                changed = (mark_joined if joined else mark_left)(
                    current, channel_id, observed_at=started_at, source="inventory", authority=captured,
                    **({"public_verified": True} if joined else {}),
                )
                counts["joined" if joined else "left"] += int(changed)
            return counts
    except (dm.SlackDmMirrorError, ObjectDoesNotExist):
        return {"joined": 0, "left": 0}


def consenting_members(workspace_id, channel_id):
    """Return current joined grant snapshots, never an audience lock lease.

    This predicate remains available for existing-room reductions with flags
    off. Read entitlement uses the existing Slack consent, independently of
    Roo AI consent, public attribution links and the canary expansion cohort.
    """
    from . import slack_member_access as access
    from .community_bridge.member_channels import valid_slack_channel_identity

    if not valid_slack_channel_identity(workspace_id, channel_id):
        return []
    rows = SlackChannelMembership.objects.filter(
        slack_workspace_id=workspace_id, slack_channel_id=channel_id, state="joined",
        grant__slack_workspace_id=workspace_id, grant__status="active",
        grant__revoked_at__isnull=True, grant__user__is_active=True,
    ).select_related("grant__user", "grant__connection").order_by("grant_id")
    return [row.grant for row in rows if access._membership_state(row.grant, row) == "joined"
            and public_channels_enabled(row.grant)]
