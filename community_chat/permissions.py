"""Account authority for Chat, independent of other MLAI admin products."""

from .models import ChatRole, CommunityChatDevice, DeviceBindingStatus, Moderator


def is_chat_admin(role):
    """Both administration tiers can manage ordinary Chat members."""
    return role in ("owner", "admin")


def appointed_chat_role(user):
    """Read appointments even for inactive targets, whose access stays protected."""
    return ChatRole.objects.filter(user=user).values_list("role", flat=True).first()


def chat_role(user):
    """Return the current Chat role of an active authenticated MLAI account."""
    if not getattr(user, "is_authenticated", False) or not user.is_active:
        return "member"
    appointment = appointed_chat_role(user)
    if appointment:
        return appointment
    if Moderator.objects.filter(user=user, is_active=True).exists():
        return "moderator"
    return "member"


def device_chat_role(public_key):
    """Resolve authority only through a live, verified device/account binding."""
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
    return chat_role(device.user) if device else "member"


def device_protection_role(public_key):
    """Keep administrator targets protected after device/account revocation.

    This role only denies peer changes; it must never authorize an actor. A
    current binding takes precedence over historical ownership of a reused key.
    """
    devices = CommunityChatDevice.objects.filter(public_key=public_key)
    device = (
        devices.filter(
            status__in=(DeviceBindingStatus.PENDING, DeviceBindingStatus.VERIFIED),
            revoked_at__isnull=True,
        ).first()
        or devices.order_by("-created_at").first()
    )
    if device is None:
        return "member"
    return appointed_chat_role(device.user) or "member"


def account_chat_role(user, public_key, installation_id):
    """A session may use only its own account's verified device installation."""
    if (
        not installation_id
        or not CommunityChatDevice.objects.filter(
            user=user,
            public_key=public_key,
            installation_id=installation_id,
            status=DeviceBindingStatus.VERIFIED,
            revoked_at__isnull=True,
        ).exists()
    ):
        return "member"
    return chat_role(user)


def role_capabilities(role):
    """Expose the same small permission matrix to mobile and desktop clients."""
    return {
        "role": role,
        "can_create_channels": role in ("owner", "admin", "moderator"),
        "can_mention_channel": True,  # Slack default; relay enforces channel-specific restrictions.
        "can_manage_channels": is_chat_admin(role),
        "can_manage_members": is_chat_admin(role),
        "can_moderate": is_chat_admin(role),
        "can_manage_admins": role == "owner",
    }
