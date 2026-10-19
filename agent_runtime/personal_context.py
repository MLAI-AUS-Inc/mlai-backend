"""Per-message owner consent for personal Dot context, without wider grants.

The signed relay trigger is the authority. The notification channel remains the
existing grant resource; the immutable invocation resource marks this distinct
read/reply path. Neither the client nor the Dot can supply message contents.
"""

import re
from datetime import datetime, timedelta, timezone as datetime_timezone

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError

from . import native_sources, runtime
from .models import AgentGrant, AgentInstallation, AgentInvocation
from .policy import AgentPermissionDenied, authorize, validate_grant_base

MODE = "owner-mentions"
CONTEXT_POLICY = "owner_requested_conversation"
HEX_ID = re.compile(r"^[0-9a-f]{64}$")
MARKER = ":personal-channel:"


def enabled():
    return bool(getattr(settings, "AGENT_RUNTIME_PERSONAL_CONTEXT_ENABLED", False))


def is_resource(resource):
    return isinstance(resource, str) and MARKER in resource


def actual_resource(resource):
    """Resolve only a canonical personal marker; ordinary resources are unchanged."""
    if not is_resource(resource):
        return resource
    value = resource.replace(MARKER, ":channel:", 1)
    community, channel = native_sources.parse_resource(value)
    if resource != f"community:{community}{MARKER}{channel}":
        raise ValidationError("Invalid personal conversation resource.")
    return value


def resource_for(community, channel):
    value = f"community:{community}:channel:{channel}"
    native_sources.parse_resource(value)
    return value.replace(":channel:", MARKER, 1)


def validate_mode(grant, resource):
    if not enabled() or grant.installation.app_id != "openai-dot" or not is_resource(resource):
        raise AgentPermissionDenied("personal_context_unavailable")
    community, _ = native_sources.parse_resource(actual_resource(resource))
    if (community != str(grant.installation.community_id) or len(grant.resources) != 1
            or native_sources.parse_resource(grant.resources[0])[0] != community):
        raise AgentPermissionDenied("resource_denied")


def notification_resource(invocation):
    if not is_resource(invocation.resource):
        return invocation.resource
    validate_mode(invocation.grant, invocation.resource)
    return invocation.grant.resources[0]


def relay_query(grant, resource, operation, *, owner_public_key, **context):
    """Use the restricted signed gateway, with owner authority checked per request."""
    native_sources.require_native(reply=operation == "reply")
    validate_mode(grant, resource)
    community, channel = native_sources.parse_resource(actual_resource(resource))
    _, config = native_sources.community_config(community)
    native_sources.current_device(grant.installation.owner, public_key=owner_public_key)
    directive = {**context, "operation": operation, "execution_mode": "personal_context",
        "community_id": community, "channel_id": channel, "owner_pubkey": owner_public_key,
        "bot_pubkey": grant.installation.public_key, "installation_id": str(grant.installation_id),
        "grant_generation": grant.generation}
    return native_sources._request(config, "/query", [{"native_agent": directive}])


def validate_metadata(metadata, *, grant, resource, event_id, owner_public_key, root=None):
    community, channel = native_sources.parse_resource(actual_resource(resource))
    if (not isinstance(metadata, dict) or metadata.get("context_policy") != CONTEXT_POLICY
            or metadata.get("community_id") != community or metadata.get("channel_id") != channel
            or metadata.get("event_id") != event_id or metadata.get("author_pubkey") != owner_public_key
            or not HEX_ID.fullmatch(str(metadata.get("root_event_id", "")))
            or type(metadata.get("created_at")) is not int
            or (root is not None and metadata.get("root_event_id") != root)):
        raise PermissionDenied("Personal conversation trigger does not match this request.")
    validate_mode(grant, resource)
    return metadata


def authorize_invocation(grant, invocation, scope, *, expected_generation=None, now=None):
    """Verify the signed per-request consent and current source ACL, not a wildcard."""
    now = now or timezone.now()
    validate_mode(grant, invocation.resource)
    authorize(grant, scope, expected_generation=expected_generation, now=now)
    try:
        result = relay_query(grant, invocation.resource, "trigger", owner_public_key=invocation.owner_public_key,
            trigger_event_id=invocation.source_event_id, root_event_id=invocation.thread_id,
            expires_at=min(int(grant.expires_at.timestamp()), int(now.timestamp()) + 840))
    except native_sources.SourceUnavailable as exc:
        raise AgentPermissionDenied("resource_unavailable") from exc
    return validate_metadata(result.get("trigger"), grant=grant, resource=invocation.resource,
        event_id=invocation.source_event_id, owner_public_key=invocation.owner_public_key, root=invocation.thread_id)


@transaction.atomic
def submit(owner, installation_id, data, *, now=None):
    """Admit one canonical signed owner mention; retries never create another request."""
    now = now or timezone.now()
    native_sources.require_native()
    if not enabled():
        raise AgentPermissionDenied("personal_context_unavailable")
    installation = AgentInstallation.objects.get(pk=installation_id, owner=owner,
        app_id="openai-dot", community_id=data["community_id"], is_active=True, state="active")
    grant_id = AgentGrant.objects.filter(installation=installation, revoked_at__isnull=True,
        expires_at__gt=now).order_by("-created_at").values_list("pk", flat=True).first()
    if grant_id is None:
        raise AgentPermissionDenied("grant_inactive")
    grant = runtime._grant(grant_id)
    installation = grant.installation
    if installation.owner.pk != owner.pk:
        raise AgentPermissionDenied("owner_changed")
    validate_grant_base(grant, now=now)
    from .unified_authority import assert_unified_authority
    assert_unified_authority(grant)
    if installation.owner_device_id is None:
        raise AgentPermissionDenied("owner_device_changed")
    anchor = native_sources.current_device(owner, public_key=installation.owner_device.public_key)
    if anchor.pk != installation.owner_device_id:
        raise AgentPermissionDenied("owner_device_changed")
    caller = native_sources.current_device(owner, public_key=data["owner_public_key"])
    resource = resource_for(str(data["community_id"]), str(data["channel_id"]))
    validate_mode(grant, resource)
    for scope in ("events:read", "messages:read", "messages:reply"):
        authorize(grant, scope, now=now)
    result = relay_query(grant, resource, "trigger", owner_public_key=caller.public_key,
        trigger_event_id=data["event_id"], expires_at=min(int(grant.expires_at.timestamp()), int(now.timestamp()) + 840))
    metadata = validate_metadata(result.get("trigger"), grant=grant, resource=resource,
        event_id=data["event_id"], owner_public_key=caller.public_key)
    existing = AgentInvocation.objects.filter(installation=installation, source_event_id=data["event_id"]).first()
    if existing:
        if (existing.grant_id != grant.pk or existing.generation != grant.generation
                or existing.resource != resource or existing.owner_public_key != caller.public_key
                or existing.thread_id != metadata["root_event_id"]):
            raise ValidationError("This mention already has a different invocation.", code="invocation_conflict")
        return existing
    try:
        occurred = datetime.fromtimestamp(metadata["created_at"], tz=datetime_timezone.utc)
    except (ValueError, OverflowError, OSError) as exc:
        raise ValidationError("Invalid mention timestamp.") from exc
    from .native_invocations import always_admit, native_daily_cap, native_day_start
    ttl = settings.AGENT_RUNTIME_NATIVE_QUEUE_TTL_MINUTES if always_admit() else 15
    expires = min(grant.expires_at, occurred + timedelta(minutes=ttl))
    if occurred > now + timedelta(seconds=60) or occurred < grant.created_at - timedelta(seconds=300) or expires <= now:
        raise ValidationError("Mention is outside its permitted request window.", code="invocation_expired")
    if AgentInvocation.objects.filter(installation=installation, created_at__gte=native_day_start(now)).count() >= native_daily_cap(grant):
        raise AgentPermissionDenied("daily_limit_reached")
    invocation = AgentInvocation.objects.create(installation=installation, grant=grant, generation=grant.generation,
        resource=resource, source_event_id=data["event_id"], thread_id=metadata["root_event_id"],
        owner_public_key=caller.public_key, expires_at=expires)
    from .events import ingest_event
    ingest_event(source="native", source_event_id=str(invocation.pk), resource=grant.resources[0],
        event_type="agent.mentioned", occurred_at=min(occurred, now), actor_kind="human",
        payload={"installation_id": str(installation.pk), "invocation_id": str(invocation.pk),
                 "source_event_id": invocation.source_event_id})
    runtime.audit("native.personal_context.admitted", grant=grant, invocation_id=str(invocation.pk),
        source_event_id=invocation.source_event_id)
    return invocation
