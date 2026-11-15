"""Chat admin controls for every Dot in the community, like Slack's Manage apps.

Chat owners and admins (community_chat.ChatRole) can list Dots, suspend and
restore them, and remove them. Suspension stops every action at once, because
grant checks require an active installation, and the owner can't undo it.
Removal is the owner's disconnect, done by an admin. The activity log shows
event types, counts and ids from the audit trail, never message text.
"""

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError

from . import runtime
from .models import AgentAuditEvent, AgentInstallation

ACTIVITY_LIMIT = 50
# Audit metadata keys an admin may see; everything else stays private.
VISIBLE_KEYS = ("channel_id", "event_id", "invocation_id", "state", "participants")


def is_chat_admin(user):
    from community_chat.models import ChatRole
    return bool(user and user.is_active and ChatRole.objects.filter(user=user, role__in=("owner", "admin")).exists())


def require_admin(user):
    if not is_chat_admin(user):
        raise PermissionDenied("Only MLAI Chat owners and admins can manage agents.")


def _owner_name(owner):
    name = " ".join(f"{getattr(owner, 'first_name', '')} {getattr(owner, 'last_name', '')}".split())
    return name or "A member"


def row(installation):
    grant = installation.grants.order_by("-created_at").first()
    return {"id": str(installation.pk), "name": installation.name, "handle": installation.handle or "",
            "owner_name": _owner_name(installation.owner), "app_id": installation.app_id,
            "state": installation.state, "public_key": installation.public_key,
            "created_at": installation.created_at.isoformat(),
            "suspended_at": installation.suspended_at.isoformat() if installation.suspended_at else None,
            "expires_at": grant.expires_at.isoformat() if grant else None}


def list_installations(user, community):
    require_admin(user)
    rows = (AgentInstallation.objects.filter(community_id=community, app_id__in=("openai-dot", "mlai-community"))
            .exclude(state="revoked").select_related("owner").order_by("created_at")[:500])
    return [row(installation) for installation in rows]


def _locked(installation_id):
    return runtime.locked(AgentInstallation.objects.select_related("owner")).get(pk=installation_id)


@transaction.atomic
def suspend(user, installation_id):
    require_admin(user)
    installation = _locked(installation_id)
    if installation.state == "revoked" or not installation.is_active:
        raise ValidationError("This agent was already removed.")
    if installation.state != "suspended":
        installation.native_cursor = {**installation.native_cursor, "state_before_suspension": installation.state}
        installation.state = "suspended"
        installation.suspended_at = timezone.now()
        installation.save(update_fields=("state", "suspended_at", "native_cursor", "updated_at"))
        runtime.audit("native.admin.suspended", installation_id=str(installation.pk), admin_id=str(user.pk))
    return row(installation)


@transaction.atomic
def restore(user, installation_id):
    require_admin(user)
    installation = _locked(installation_id)
    if installation.state != "suspended":
        raise ValidationError("This agent is not suspended.")
    cursor = dict(installation.native_cursor)
    previous = cursor.pop("state_before_suspension", "active")
    installation.state = previous if previous in {"active", "paused", "provisioning"} else "paused"
    installation.suspended_at = None
    installation.native_cursor = cursor
    installation.save(update_fields=("state", "suspended_at", "native_cursor", "updated_at"))
    runtime.audit("native.admin.restored", installation_id=str(installation.pk), admin_id=str(user.pk))
    return row(installation)


@transaction.atomic
def remove(user, installation_id):
    require_admin(user)
    installation = _locked(installation_id)
    if installation.state == "revoked":
        return row(installation)
    runtime.revoke_installation(installation.pk, owner=installation.owner)
    runtime.audit("native.admin.removed", installation_id=str(installation.pk), admin_id=str(user.pk))
    return row(_locked(installation_id))


def activity(user, installation_id):
    require_admin(user)
    installation = AgentInstallation.objects.get(pk=installation_id)
    events = (AgentAuditEvent.objects.filter(grant__installation=installation)
              | AgentAuditEvent.objects.filter(metadata__installation_id=str(installation.pk)))
    result = []
    for event in events.order_by("-created_at")[:ACTIVITY_LIMIT]:
        details = {key: event.metadata[key] for key in VISIBLE_KEYS if key in event.metadata}
        result.append({"type": event.event_type, "at": event.created_at.isoformat(), **details})
    return result
