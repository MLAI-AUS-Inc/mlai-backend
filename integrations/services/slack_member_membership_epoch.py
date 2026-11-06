"""A public-membership proof barrier independent of the owner's consent.

Pauses, reconnects and administrative authority changes can outlast a Slack
membership change. Existing consent remains accepted; old observations cannot
authorize a new epoch until an authoritative fresh join/inventory proves it.
"""

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.db import connection, transaction
from django.db.models import Max
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from integrations.models import ExternalServiceConnection, SlackChannelMembership, SlackDmMirrorGrant


KEY = "slack_member_membership_epoch_v1"


def _timestamp(raw):
    if not isinstance(raw, str):
        return None
    try:
        value = parse_datetime(raw)
    except ValueError:
        return None
    return value if value is not None and timezone.is_aware(value) else None


def membership_floor(grant):
    """Return the oldest eligible proof time, or None for corrupt authority."""
    consent = grant.consented_at
    if consent is None or timezone.is_naive(consent):
        return None
    cursor = getattr(grant.connection, "sync_cursor", {}) or {}
    if not isinstance(cursor, dict):
        return None
    if KEY not in cursor:
        return consent
    epoch = cursor[KEY]
    floor = _timestamp(epoch.get("not_before")) if isinstance(epoch, dict) else None
    return max(consent, floor) if floor is not None else None


def _private_only_scopes(scopes):
    from .slack_chat_catalog import PUBLIC_CHANNEL_SCOPES

    return bool(isinstance(scopes, (list, tuple, set, frozenset))
                and all(isinstance(value, str) for value in scopes)
                and not PUBLIC_CHANNEL_SCOPES.issubset({value.strip() for value in scopes}))


def invalidate_memberships_locked(user_id):
    """Advance all Slack membership floors in User→grant→connection order.

    Call inside the caller's authority transaction before Channel locks. The
    user lock also fences membership observations. Include already stored
    observations when advancing so even a clock-skewed old proof is invalid.
    No joined/left rows or consent choices are rewritten or deleted.
    """
    from .message_sync.member_callback_authority import assert_callback_user_locked

    if not connection.in_atomic_block:
        raise transaction.TransactionManagementError("Membership invalidation requires an authority transaction")
    assert_callback_user_locked(user_id)
    get_user_model().objects.select_for_update().get(pk=user_id)
    list(SlackDmMirrorGrant.objects.select_for_update().filter(user_id=user_id).order_by("pk"))
    connections = list(ExternalServiceConnection.objects.select_for_update().filter(
        user_id=user_id, provider="slack",
    ).order_by("pk"))
    observations = {row["grant__connection_id"]: row["latest"] for row in SlackChannelMembership.objects.filter(
        grant__user_id=user_id,
    ).values("grant__connection_id").annotate(latest=Max("observed_at"))}
    now = timezone.now()
    updated = 0
    for current in connections:
        cursor = dict(current.sync_cursor) if isinstance(current.sync_cursor, dict) else {}
        if current.pk not in observations and KEY not in cursor and _private_only_scopes(current.scopes):
            # Private-only zero-membership erasure remains inert. Flags alone
            # cannot justify this skip: old public callbacks may remain parked
            # while the feature is off and replay after pause/resume.
            continue
        epoch = cursor.get(KEY)
        previous = _timestamp(epoch.get("not_before")) if isinstance(epoch, dict) else None
        candidates = [value for value in (now, previous, observations.get(current.pk)) if value is not None]
        barrier = max(candidates) + timedelta(microseconds=1)
        cursor[KEY] = {"not_before": barrier.isoformat()}
        current.sync_cursor = cursor
        current.save(update_fields=("sync_cursor", "updated_at"))
        updated += 1
    return updated
