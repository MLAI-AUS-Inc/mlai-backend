"""Small tool catalogue; scopes and source access are enforced on every call."""
from django.db import transaction
from rest_framework.exceptions import PermissionDenied, ValidationError

from .authentication import validate_credential
from .models import AgentCredential
from .policy import authorize
from .runtime import audit
from . import serializers as schema
from . import sources


TOOLS = [
    {"name": "get_profile", "description": "Read the account identity and exact delegated scope for this agent."},
    {"name": "list_channels", "description": "List only selected public Slack channels still available to the owner."},
    {"name": "read_channel", "description": "Read one bounded page from an explicitly selected public Slack channel."},
    {"name": "read_thread", "description": "Read one bounded page of a Slack thread in an explicitly selected channel."},
    {"name": "search_messages", "description": "Search one channel history page; this is a bounded page search, not a complete archive."},
    {"name": "get_events_since", "description": "Read metadata-only events since a resource-specific sequence cursor."},
    {"name": "propose_action", "description": "Draft an exact public-channel message for the owner's approval. Does not send."},
]
TOOL_SCHEMAS = {
    "get_profile": schema.EmptySerializer, "list_channels": schema.EmptySerializer,
    "read_channel": schema.ReadSerializer, "read_thread": schema.ThreadSerializer,
    "search_messages": schema.SearchSerializer, "get_events_since": schema.EventsSerializer,
    "propose_action": schema.ProposalSerializer,
}


def require_scope(principal, scope, resource=None):
    if scope not in principal.credential.scopes:
        raise PermissionDenied("Credential scope does not permit this operation.")
    if resource is not None and principal.task is not None and resource != principal.task.resource:
        raise PermissionDenied("This task is limited to its approved channel.")
    authorize(principal.grant, scope, resource,
              expected_generation=principal.credential.generation,
              access_checker=sources.live_access)


@transaction.atomic
def execute(principal, tool, arguments):
    serializer = TOOL_SCHEMAS.get(tool) if isinstance(tool, str) else None
    if serializer is None:
        raise ValidationError({"tool": "Unknown tool."})
    args = schema.validated(serializer, arguments)
    # Hold the same user->grant revocation fence through source I/O. Re-reading
    # prevents a previously authenticated request using a now-revoked grant.
    from .runtime import _grant
    grant = _grant(principal.grant.pk)
    credential = AgentCredential.objects.select_for_update().select_related("task").get(pk=principal.credential.pk)
    credential.grant = grant
    principal = validate_credential(credential, audience=principal.credential.audience,
                                    allowed_kinds=("access", "task_tool"))
    resource = args.get("resource")
    if tool == "get_profile":
        result = {"owner_id": str(principal.owner.pk), "agent_id": str(grant.installation_id),
                  "agent_name": grant.installation.name, "scopes": list(credential.scopes),
                  "resources": ([principal.task.resource] if principal.task and principal.task.resource else
                                [] if principal.task else list(grant.resources)), "expires_at": grant.expires_at.isoformat()}
    elif tool == "list_channels":
        require_scope(principal, "messages:read")
        channels = []
        selected_resources = [principal.task.resource] if principal.task and principal.task.resource else [] if principal.task else grant.resources
        for selected in selected_resources:
            require_scope(principal, "messages:read", selected)
            channels.append(sources.channel_summary(principal.owner, selected))
        result = {"channels": channels}
    elif tool in {"read_channel", "read_thread", "search_messages"}:
        require_scope(principal, "messages:read", resource)
        result = sources.read_messages(principal.owner, **args)
    elif tool == "get_events_since":
        from .events import read_events
        require_scope(principal, "events:read", resource)
        result = read_events(grant, access_checker=sources.live_access, **args)
    elif tool == "propose_action":
        from .actions import propose_action
        if principal.task is None:
            raise PermissionDenied("Action drafts require a currently leased task.")
        require_scope(principal, "actions:propose", resource)
        action = propose_action(principal.task.pk, lease_token=principal.task.lease_token,
            lease_epoch=principal.task.lease_epoch, access_checker=sources.live_access, **args)
        result = {"proposal_id": str(action.pk), "status": action.status,
                  "kind": action.kind, "resource": action.resource,
                  "payload": action.payload, "payload_hash": action.payload_hash}
    else:
        raise ValidationError("Unknown tool.")
    audit("tool.called", grant=grant, task=principal.task, tool=tool, resource=resource or "")
    return result
