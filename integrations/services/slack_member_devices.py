"""Complete, bounded Members-only device audiences.

Callers establish current consent and joined membership and, for I/O, hold
sorted User/grant/connection/device authority locks. These helpers are snapshots,
not leases. Private Slack rooms retain their separate eight-device policy.
"""
from django.db.models.functions import Lower

from community_chat.models import CommunityChatDevice, DeviceBindingStatus

from .community_bridge.buzz import EVENT_ID_RE

MAX_MEMBER_KEYS = 1000


def device_key_result(user_ids):
    """Return every distinct current key, or an explicit untruncated failure.

    The extra row proves overflow. LIMIT never selects a usable partial audience;
    callers must close/hold the room when the complete set cannot be represented.
    """
    users = set(user_ids)
    if not users:
        return set(), ""
    values = list(
        CommunityChatDevice.objects.filter(
            user_id__in=users,
            user__is_active=True,
            status=DeviceBindingStatus.VERIFIED,
            revoked_at__isnull=True,
        )
        .annotate(member_key=Lower("public_key"))
        .order_by("member_key")
        .values_list("member_key", flat=True)
        .distinct()[: MAX_MEMBER_KEYS + 1]
    )
    if any(not isinstance(value, str) or not EVENT_ID_RE.fullmatch(value) for value in values):
        return None, "audience_authority_unavailable"
    if len(values) > MAX_MEMBER_KEYS:
        return None, "audience_too_large"
    return set(values), ""


def member_device_key_allowed(user_id, public_key):
    """Exact current device admission, with no per-account rank/count limit.

    Full room capacity and acknowledgement remain separate mandatory checks.
    """
    if not isinstance(public_key, str) or not EVENT_ID_RE.fullmatch(public_key):
        return False
    return CommunityChatDevice.objects.filter(
        user_id=user_id,
        user__is_active=True,
        public_key__iexact=public_key,
        status=DeviceBindingStatus.VERIFIED,
        revoked_at__isnull=True,
    ).exists()
