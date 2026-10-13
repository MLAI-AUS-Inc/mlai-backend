"""Account-owned installation metadata and deliberate remote revocation."""

from datetime import timedelta

from django.db.models import Max
from django.utils import timezone
from rest_framework.exceptions import NotFound, PermissionDenied

from .adapter import revoke_member_invite, revoke_relay_membership
from .device_revocation import revoke_device_authority
from .models import CommunityChatAccountSession, CommunityChatDevice, DeviceBindingStatus
from .privacy import locked_privacy_session


def device_agent_connections(user_id):
    """Return the owner's agent anchors without granting agent authority."""
    from agent_runtime.models import AgentInstallation

    connections = {}
    for installation in AgentInstallation.objects.filter(
        owner_id=user_id,
        state__in=("awaiting_owner_proof", "provisioning", "active", "paused"),
        owner_device_id__isnull=False,
    ).only("owner_device_id", "name"):
        connections.setdefault(installation.owner_device_id, []).append(installation.name)
    return connections


def account_devices(user, session):
    """List owned active registrations with actual authenticated-session use."""
    activity = {
        (row["public_key"], row["installation_id"]): row["last_authenticated_at"]
        for row in CommunityChatAccountSession.objects.filter(user=user)
        .values("public_key", "installation_id")
        .annotate(last_authenticated_at=Max("last_used_at"))
    }
    agents = device_agent_connections(user.pk)
    rows = []
    for device in CommunityChatDevice.objects.filter(
        user=user, revoked_at__isnull=True,
        status__in=(DeviceBindingStatus.PENDING, DeviceBindingStatus.VERIFIED),
    ).order_by("-created_at", "id"):
        rows.append({
            "id": str(device.pk), "public_key": device.public_key,
            "installation_id": str(device.installation_id),
            "client_id": device.client_id, "name": device.name,
            "platform": device.platform, "status": device.status,
            "created_at": device.created_at,
            "last_authenticated_at": activity.get((device.public_key, device.installation_id)),
            "is_current": device.public_key == session.public_key
                and device.installation_id == session.installation_id,
            "agent_connections": agents.get(device.pk, []),
        })
    return rows


def update_account_device(*, authenticated_session, device_id, name):
    """Rename only the current owner's live registration."""
    with locked_privacy_session(authenticated_session) as (user, _):
        device = CommunityChatDevice.objects.select_for_update().filter(
            pk=device_id, user=user, revoked_at__isnull=True,
            status__in=(DeviceBindingStatus.PENDING, DeviceBindingStatus.VERIFIED),
        ).first()
        if device is None:
            raise NotFound("Device not found.")
        device.name = name
        device.save(update_fields=("name", "updated_at"))


def remove_account_device(*, authenticated_session, device_id, expected_public_key):
    """Revoke another owned device after recent proof, preserving agent anchors."""
    with locked_privacy_session(authenticated_session) as (user, session):
        if session.created_at < timezone.now() - timedelta(minutes=10):
            raise PermissionDenied({"code": "reauthentication_required",
                "detail": "Sign out and sign back in before removing another device."})
        device = CommunityChatDevice.objects.filter(
            pk=device_id, user=user, public_key=expected_public_key,
            revoked_at__isnull=True,
            status__in=(DeviceBindingStatus.PENDING, DeviceBindingStatus.VERIFIED),
        ).first()
        if device is None:
            raise NotFound("Device not found. Refresh your devices.")
        if device.public_key == session.public_key or device.installation_id == session.installation_id:
            raise PermissionDenied({"code": "current_device",
                "detail": "Use Remove this device to disconnect the app you are using."})
        if device.pk in device_agent_connections(user.pk):
            raise PermissionDenied({"code": "agent_device_in_use",
                "detail": "Reconnect or disconnect its agent before removing this device."})
        revoked = revoke_device_authority(
            user, device_id=device.pk, public_key=device.public_key,
            reason="removed_from_account_devices",
            revoke_member_invite_callback=revoke_member_invite,
            revoke_relay_membership_callback=revoke_relay_membership,
        )
        if revoked is None:
            raise NotFound("Device changed. Refresh your devices.")
        return revoked.relay_revocation_status
