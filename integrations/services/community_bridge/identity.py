from typing import Optional

from django.db.models import F

from community_chat.models import CommunityChatDevice, DeviceBindingStatus
from integrations.models import CommunityBridgeIdentityLink


def verified_identity_for_slack(
    *,
    slack_workspace_id: str,
    slack_user_id: str,
) -> Optional[dict]:
    """Resolve a Slack author through their MLAI account and active chat device.

    Account-backed links deliberately re-resolve the device on every delivery so
    revoking or rotating one device does not break the user's Slack identity.
    Pre-account legacy links remain readable for one compatibility window.
    """

    link = (
        CommunityBridgeIdentityLink.objects.select_related("user")
        .filter(
            slack_workspace_id=str(slack_workspace_id or "").strip(),
            slack_user_id=str(slack_user_id or "").strip(),
            revoked_at__isnull=True,
        )
        .first()
    )
    if link is None:
        return None
    if link.user_id:
        if not link.user.is_active:
            return None
        device = _preferred_device_for_user(link.user_id, preferred_pubkey=link.buzz_pubkey)
        return _serialize(link, device=device)
    return _serialize(link, legacy_pubkey=link.buzz_pubkey)


def verified_identity_for_buzz(
    *,
    slack_workspace_id: str,
    buzz_pubkey: str,
) -> Optional[dict]:
    """Resolve any active device key to its account's Slack identity link."""

    workspace_id = str(slack_workspace_id or "").strip()
    public_key = str(buzz_pubkey or "").strip().lower()
    device = (
        CommunityChatDevice.objects.select_related("user")
        .filter(
            public_key=public_key,
            status=DeviceBindingStatus.VERIFIED,
            revoked_at__isnull=True,
            user__is_active=True,
        )
        .first()
    )
    if device is not None:
        link = (
            CommunityBridgeIdentityLink.objects.select_related("user")
            .filter(
                slack_workspace_id=workspace_id,
                user_id=device.user_id,
                revoked_at__isnull=True,
            )
            .first()
        )
        return _serialize(link, device=device)

    # Compatibility for links created before MLAI accounts became authoritative.
    link = (
        CommunityBridgeIdentityLink.objects.filter(
            slack_workspace_id=workspace_id,
            buzz_pubkey=public_key,
            user__isnull=True,
            revoked_at__isnull=True,
        )
        .first()
    )
    return _serialize(link, legacy_pubkey=public_key)


def _preferred_device_for_user(user_id: int, *, preferred_pubkey: str) -> Optional[CommunityChatDevice]:
    devices = CommunityChatDevice.objects.filter(
        user_id=user_id,
        status=DeviceBindingStatus.VERIFIED,
        revoked_at__isnull=True,
    )
    preferred = devices.filter(public_key=preferred_pubkey).first()
    if preferred is not None:
        return preferred
    return devices.order_by(
        F("last_seen_at").desc(nulls_last=True),
        F("verified_at").desc(nulls_last=True),
        "-created_at",
    ).first()


def _serialize(
    link: Optional[CommunityBridgeIdentityLink],
    *,
    device: Optional[CommunityChatDevice] = None,
    legacy_pubkey: str = "",
) -> Optional[dict]:
    if link is None:
        return None
    user = link.user if link.user_id else None
    profile_name = user.full_name if user else ""
    return {
        "id": link.id,
        "user_profile_id": str(user.community_chat_profile_id) if user else "",
        "slack_workspace_id": link.slack_workspace_id,
        "slack_user_id": link.slack_user_id,
        "buzz_pubkey": device.public_key if device is not None else legacy_pubkey,
        "display_name": profile_name or link.display_name,
        "verification_method": link.verification_method,
        "verified_at": link.verified_at.isoformat(),
        "identity_source": "mlai_account" if user else "legacy_key",
    }
