"""Recheck the existing unified MCP consent at durable native boundaries."""

from django.conf import settings
from django.utils import timezone

from .models import AgentCredential, AgentOAuthBinding
from .policy import AgentPermissionDenied

PREFIX = "unified-mcp:"
DOT_SCOPES = frozenset({"agent:mentions:listen", "agent:thread:read", "agent:thread:reply"})


def assert_unified_authority(grant, *, credential_id=None, allow_pending=False):
    """Both durable generation and live cached session consent must still agree.

    Original standalone grants remain usable only in standalone mode. In unified
    mode, missing or deleted bindings never fall back to the enrollment grant.
    """
    if getattr(grant.installation, "community_id", None) is None:
        return None
    bindings = AgentOAuthBinding.objects.filter(credential__grant_id=grant.pk,
        client_id__startswith=PREFIX).select_related("credential")
    if credential_id is not None:
        bindings = bindings.filter(credential_id=credential_id)
    rows = list(bindings)
    if not rows:
        if credential_id is not None or getattr(settings, "MLAI_MCP_NATIVE_AGENTS_ENABLED", False):
            raise AgentPermissionDenied("unified_binding_unavailable")
        return None
    if not getattr(settings, "MLAI_MCP_NATIVE_AGENTS_ENABLED", False):
        raise AgentPermissionDenied("unified_agent_disabled")
    from startup_updates.mcp import oauth
    from startup_updates.mcp.config import mlai_url
    now = timezone.now()
    for binding in rows:
        credential = binding.credential
        try:
            principal = oauth.valid_grant(binding.client_id[len(PREFIX):])
        except Exception:
            # Cache, account/session, and device authority are all required.
            # An unavailable cache cannot restore standalone authorization.
            continue
        cached = principal.grant
        if (principal.user.pk == grant.installation.owner_id
                and cached.get("resource") == mlai_url()
                and DOT_SCOPES <= set(cached.get("scopes", []))
                and cached.get("native_agent_grant_id") == str(grant.pk)
                and cached.get("native_agent_credential_id") == str(credential.pk)
                and credential.kind == "access" and credential.generation == grant.generation
                and credential.revoked_at is None and (allow_pending or credential.expires_at > now)
                and credential.audience == mlai_url()
                and set(credential.scopes) == {"events:read", "messages:read", "messages:reply"}
                and set(grant.scopes) == set(credential.scopes) and len(grant.resources) == 1
                and grant.revoked_at is None and grant.expires_at > now
                and grant.installation.is_active and grant.installation.state == "active"
                and grant.installation.owner_device_id is not None):
            return principal
    raise AgentPermissionDenied("unified_authority_changed")


def assert_record_authority(grant, metadata):
    """Queued callback/reply work remains tied to its exact consenting connection."""
    marker = metadata.get("unified_credential_id") if isinstance(metadata, dict) else None
    if marker:
        return assert_unified_authority(grant, credential_id=marker)
    if getattr(grant.installation, "community_id", None) is None:
        return None
    if getattr(settings, "MLAI_MCP_NATIVE_AGENTS_ENABLED", False):
        raise AgentPermissionDenied("unified_binding_unavailable")
    linked = AgentOAuthBinding.objects.filter(credential__grant_id=grant.pk, client_id__startswith=PREFIX).exists()
    if linked:
        raise AgentPermissionDenied("unified_binding_unavailable")
    return None


def assert_credential_authority(grant, credential):
    """A private adapted principal cannot outlive the particular unified grant."""
    if getattr(grant.installation, "community_id", None) is None:
        return None
    if not isinstance(credential, AgentCredential):
        raise AgentPermissionDenied("unified_credential_unavailable")
    marker = AgentOAuthBinding.objects.filter(credential_id=credential.pk, client_id__startswith=PREFIX).exists()
    return assert_unified_authority(grant, credential_id=credential.pk if marker else None)


def pending_activation(grant, credential_id, *, now=None):
    """Recognize valid consent waiting for the provider's first bearer use."""
    if not credential_id:
        return False
    try:
        assert_unified_authority(grant, credential_id=credential_id, allow_pending=True)
    except AgentPermissionDenied:
        return False
    return AgentCredential.objects.filter(pk=credential_id, grant=grant, kind="access",
        generation=grant.generation, revoked_at__isnull=True, expires_at__lte=now or timezone.now()).exists()
