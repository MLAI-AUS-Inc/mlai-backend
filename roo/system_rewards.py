"""Canonical recipients and operation keys for automatic link-love rewards."""

import re
from uuid import UUID

from core.models import User
from community_chat.models import CommunityChatDevice, DeviceBindingStatus
from community_chat.privacy import has_ai_consent
from .models import Ledger


def native_reward_user(profile_id, *, points, reason, idempotency_key):
    try:
        valid = isinstance(profile_id, str) and str(UUID(profile_id)) == profile_id
    except (ValueError, TypeError, AttributeError):
        valid = False
    if (not valid or points != 2 or reason != "link-love"
            or not re.fullmatch(r"link_love:C[A-Z0-9]+:[0-9]{10,}\.[0-9]+:profile:" + re.escape(profile_id), idempotency_key)):
        raise ValueError("A canonical link-love recipient and stable operation key are required")
    user = User.objects.filter(community_chat_profile_id=profile_id, is_active=True).first()
    if (user is None or not CommunityChatDevice.objects.filter(
            user=user, status=DeviceBindingStatus.VERIFIED, revoked_at__isnull=True).exists()
            or not has_ai_consent(user.pk)):
        raise ValueError("The MLAI Chat reward recipient is no longer verified or consenting")
    return user


def canonical_link_love_key(user, key, *, recipient, points, reason):
    """Use one ledger key across Slack/Chat aliases, retaining historical keys."""
    if reason != "link-love" or points != 2:
        return key
    match = re.fullmatch(r"(link_love:C[A-Z0-9]+:[0-9]{10,}\.[0-9]+):" + re.escape(recipient), key)
    if not match:
        return key
    prefix = match[1]
    canonical = f"{prefix}:profile:{user.community_chat_profile_id}"
    aliases = [key, canonical]
    if user.slack_id:
        aliases.append(f"{prefix}:{user.slack_id}")
    previous = Ledger.objects.filter(user=user, idempotency_key__in=aliases).order_by("id").first()
    return previous.idempotency_key if previous else canonical
