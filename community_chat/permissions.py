"""Account authority for Chat, independent of other MLAI admin products."""

from roo.models import PointsAdmin

from .models import CommunityChatDevice, DeviceBindingStatus, Moderator


def chat_role(user):
    """Return the current Chat role of an active authenticated MLAI account."""
    if not getattr(user, "is_authenticated", False) or not user.is_active:
        return "member"
    if (
        user.is_superuser
        or PointsAdmin.objects.filter(
            user=user, is_active=True, role__in=("admin", "committee")
        ).exists()
    ):
        return "admin"
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
        "can_create_channels": role in ("admin", "moderator"),
        "can_mention_channel": role in ("admin", "moderator"),
        "can_manage_channels": role == "admin",
        "can_manage_members": role == "admin",
        "can_moderate": role == "admin",
    }
