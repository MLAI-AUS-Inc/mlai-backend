"""Canonical recipients and operation keys for automatic link-love rewards."""

import re
import hashlib
from uuid import UUID

from core.models import User
from django.conf import settings
from django.db import transaction
from django.db.models import Q
from community_chat.models import CommunityChatDevice, DeviceBindingStatus
from community_chat.privacy import has_ai_consent
from .models import Ledger


def native_boost_key(user, root_id):
    from community_chat.volunteer.access import community_id
    if not re.fullmatch(r"[0-9a-f]{64}", root_id):
        root_id = hashlib.sha256(root_id.encode()).hexdigest()
    return f"native_boost:{community_id()}:{root_id}:profile:{user.community_chat_profile_id}"


def _bridge_root_ids(prefix):
    """Find canonical source aliases, without treating them as award authority."""
    from integrations.models import CommunityBridgeMessageLink
    _, channel, timestamp = prefix.split(":")
    links = CommunityBridgeMessageLink.objects.filter(
        channel__slack_workspace_id=settings.COMMUNITY_CHAT_ROO_SLACK_WORKSPACE_ID,
        channel__slack_channel_id=channel, channel__audience="community",
        channel__slack_visibility="public",
    ).filter(
        Q(source_platform="slack", source_channel_id=channel, source_message_id=timestamp,
          destination_platform="buzz", source_parent_message_id="")
        | Q(source_platform="buzz", destination_channel_id=channel, destination_message_id=timestamp,
            destination_platform="slack", source_parent_message_id="")
    )
    roots = {link.destination_message_id if link.source_platform == "slack" else link.source_message_id
             for link in links}
    return sorted(root for root in roots if re.fullmatch(r"[0-9a-f]{64}", root))


def _prior_boost(user, prefixes, root_ids):
    from community_chat.volunteer.access import community_id
    from community_chat.volunteer.models import VolunteerRecognition
    aliases = Q(idempotency_key__in=[native_boost_key(user, root) for root in root_ids])
    for prefix in prefixes:
        aliases |= Q(idempotency_key__startswith=f"{prefix}:")
    prior = Ledger.objects.filter(aliases, user=user, kind="EARN", source="EVENT", delta_microroo=2_000_000).order_by("id").first()
    if prior is None and root_ids:
        recognition = VolunteerRecognition.objects.filter(
            user=user, community=community_id(),
            action_key="boost_startup", source__source_id__in=root_ids, ledger__isnull=False,
        ).select_related("ledger").order_by("created_at").first()
        if recognition and recognition.ledger.user_id == user.pk and recognition.ledger.delta_microroo == 2_000_000:
            prior = recognition.ledger
    return prior


@transaction.atomic
def award_native_boost(user, source):
    """Share native reaction/reply credits and the existing Slack link-love keys."""
    from integrations.models import CommunityBridgeMessageLink
    from .services import PointsService
    User.objects.select_for_update().get(pk=user.pk)
    root = source["source_id"]
    if not isinstance(root, str) or not 1 <= len(root) <= 255:
        raise ValueError("Canonical startup source required")
    prefixes = set()
    for link in CommunityBridgeMessageLink.objects.filter(
        channel__slack_workspace_id=settings.COMMUNITY_CHAT_ROO_SLACK_WORKSPACE_ID,
        channel__destination_channel_id=source["channel_id"],
        channel__audience="community", channel__slack_visibility="public",
    ).filter(Q(
        source_platform="slack", destination_platform="buzz", destination_message_id=root,
        source_parent_message_id="") | Q(
        source_platform="buzz", destination_platform="slack", source_message_id=root,
        source_parent_message_id="")):
        timestamp = link.source_message_id if link.source_platform == "slack" else link.destination_message_id
        prefixes.add(f"link_love:{link.channel.slack_channel_id}:{timestamp}")
    prior = _prior_boost(user, prefixes, [root])
    if prior:
        return prior, False
    return PointsService.award(user=user, delta=2, source="EVENT", description="link-love",
        created_by_slack_id="volunteer:verified_source", idempotency_key=native_boost_key(user, root))


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
    roots = _bridge_root_ids(prefix)
    if len(roots) > 1:
        raise ValueError("Ambiguous startup source")
    if roots:
        prior = _prior_boost(user, [prefix], roots)
        return prior.idempotency_key if prior else native_boost_key(user, roots[0])
    canonical = f"{prefix}:profile:{user.community_chat_profile_id}"
    aliases = [key, canonical]
    if user.slack_id:
        aliases.append(f"{prefix}:{user.slack_id}")
    previous = Ledger.objects.filter(user=user, idempotency_key__in=aliases).order_by("id").first()
    return previous.idempotency_key if previous else canonical
