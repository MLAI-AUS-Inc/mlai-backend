"""Invocation-bound personal agents behind the existing unified MCP audience.

No provider gets a second bearer credential. The private credential is an
internal adapter and remains fenced by the original unified consent/session.
"""

from copy import deepcopy
from contextlib import contextmanager
from datetime import datetime, timezone as datetime_timezone
from functools import wraps
import secrets
from uuid import UUID

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ObjectDoesNotExist
from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import APIException, AuthenticationFailed, PermissionDenied, ValidationError

from agent_runtime import broker, event_webhooks, native_crypto, native_installations, native_sources, runtime
from agent_runtime.authentication import token_hash, validate_credential
from agent_runtime.mcp import input_schema
from agent_runtime.models import AgentCredential, AgentGrant, AgentInstallation, AgentInvocationReply, AgentOAuthBinding, AgentSubscription
from agent_runtime.policy import AgentPermissionDenied, validate_grant_base
from agent_runtime.unified_authority import DOT_SCOPES, PREFIX, assert_unified_authority
from . import oauth
from .config import mlai_url

NAMES = {"get_agent_account": "get_profile", "list_agent_channels": "list_channels",
         "get_invocation": "get_invocation", "read_invocation_thread": "read_invocation_thread",
         "reply_to_invocation": "reply_to_invocation", "get_invocation_status": "get_invocation_status"}
TOOL_SCOPES = {name: sorted(DOT_SCOPES) for name in NAMES}
TOOLS = []
for public_name, native_name in NAMES.items():
    template = deepcopy(next(item for item in broker.TOOLS if item["name"] == native_name))
    template.update(name=public_name, inputSchema=input_schema(native_name))
    template["annotations"] = {"readOnlyHint": native_name != "reply_to_invocation",
        "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}
    if public_name == "get_agent_account":
        template["description"] = "Identify this explicitly connected MLAI account and personal agent installation."
        template["_meta"] = {"openai/profile": True}
        template["outputSchema"] = {"type": "object", "properties": {
            field: {"type": "string"} for field in ("id", "name", "email", "nickname")},
            "required": ["id"], "additionalProperties": False}
        template["outputSchema"]["properties"]["id"].update(minLength=1, pattern=r"\S")
    elif public_name == "list_agent_channels":
        template["description"] = "List only this personal agent's approved public MLAI Chat channel."
    TOOLS.append(template)


def enabled():
    return bool(getattr(settings, "MLAI_MCP_NATIVE_AGENTS_ENABLED", False)
                and getattr(settings, "AGENT_RUNTIME_ENABLED", False)
                and getattr(settings, "AGENT_RUNTIME_NATIVE_ENABLED", False)
                and getattr(settings, "AGENT_RUNTIME_NATIVE_REPLIES_ENABLED", False)
                and getattr(settings, "AGENT_RUNTIME_MCP_EVENTS_ENABLED", False))


def _uuid(value, message):
    try:
        return UUID(str(value))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValidationError(message) from exc


class NativeAgentError(APIException):
    """An actionable application failure, without an OAuth reconnect challenge."""
    status_code = 409

    def __init__(self, code):
        self.default_code = code
        super().__init__("The personal agent request could not be completed.", code=code)


def _api_errors(operation):
    """Keep reconnect challenges specific to unified authority and scope loss."""
    @wraps(operation)
    def wrapped(*args, **kwargs):
        try:
            return operation(*args, **kwargs)
        except AgentPermissionDenied as exc:
            if exc.code.startswith("unified_") or exc.code == "scope_denied":
                raise PermissionDenied("Personal agent authorisation is no longer available.", code=exc.code) from exc
            code = "owner_action_required" if exc.code in {"grant_inactive", "installation_inactive", "owner_device_changed"} else exc.code
            raise NativeAgentError(code) from exc
        except ObjectDoesNotExist as exc:
            raise NativeAgentError("owner_action_required") from exc
        except runtime.AgentRuntimeError as exc:
            raise NativeAgentError(exc.code) from exc
        except ValidationError as exc:
            codes = exc.get_codes()
            code = codes[0] if isinstance(codes, list) and len(codes) == 1 else None
            if code in {"invocation_ambiguous", "reply_conflict"}:
                raise NativeAgentError(code) from exc
            raise
        except PermissionDenied as exc:
            code = exc.get_codes()
            if isinstance(code, str) and (code.startswith("unified_") or code == "scope_denied"):
                raise
            raise NativeAgentError("owner_action_required") from exc
    return wrapped


def consent_installations(user, session):
    """List only completed owner proofs that can support fresh bounded consent."""
    if not enabled():
        return []
    rows = []
    for installation in AgentInstallation.objects.filter(owner=user, app_id="openai-dot", state="active", is_active=True).select_related("owner_device"):
        grant = installation.grants.filter(expires_at__gt=timezone.now()).order_by("-created_at").first()
        if not grant or len(grant.resources) != 1 or not installation.owner_attestation or not installation.owner_device_id:
            continue
        try:
            device = native_sources.current_device(user, public_key=installation.owner_device.public_key)
            channels = native_sources.eligible_channels(user, str(installation.community_id), owner_public_key=device.public_key)
            channel = next((item for item in channels if item["resource"] == grant.resources[0]), None)
        except APIException:
            continue
        if channel:
            rows.append({"id": str(installation.pk), "name": installation.name, "channelName": channel["name"],
                "communityId": str(installation.community_id), "resource": grant.resources[0]})
    return rows


@contextmanager
def consent_fence(user):
    """Serialize only fresh native consent with the existing owner revocation fence."""
    with transaction.atomic():
        runtime.locked(get_user_model().objects.all()).get(pk=user.pk)
        yield


@_api_errors
@transaction.atomic
def bind_native_grant(user, session, unified_grant, installation_id):
    """Fresh consent supersedes native authority without upgrading any older token."""
    if (not enabled() or unified_grant.get("resource") != mlai_url()
            or not DOT_SCOPES <= set(unified_grant.get("scopes", []))):
        raise PermissionDenied("Reconnect MLAI and explicitly approve the personal agent permissions.", code="scope_denied")
    if not installation_id:
        raise ValidationError("Choose an already verified personal agent installation.")
    identifier = _uuid(installation_id, "Choose a valid personal agent installation.")
    user = runtime.locked(get_user_model().objects.all()).get(pk=user.pk)
    installation = runtime.locked(AgentInstallation.objects.filter(owner=user, pk=identifier,
        app_id="openai-dot", is_active=True, state="active")).select_related("owner_device").first()
    if installation is None or not installation.owner_attestation or not installation.owner_device_id:
        raise PermissionDenied("Complete the signed personal agent enrollment first.")
    original = runtime.locked(AgentGrant.objects.filter(installation=installation,
        expires_at__gt=timezone.now())).order_by("-created_at").first()
    if original is None or set(original.scopes) != set(native_installations.DOT_SCOPES) or len(original.resources) != 1:
        raise PermissionDenied("The installation needs renewed signed enrollment.")
    device = native_sources.current_device(user, public_key=installation.owner_device.public_key)
    if device.pk != installation.owner_device_id or not native_sources.live_access(user, "messages:read",
            original.resources[0], owner_public_key=device.public_key):
        raise PermissionDenied("The enrolled channel is no longer available.")
    if (unified_grant.get("user_id") != user.pk or unified_grant.get("auth_version") != user.auth_version
            or unified_grant.get("session_id") != str(session.pk) or not unified_grant.get("client_id")):
        raise PermissionDenied("The agent consent belongs to another account session.", code="unified_session_changed")
    now = timezone.now()
    generation = original.generation + 1
    expiry = min(original.expires_at, datetime.fromtimestamp(unified_grant["expiresAt"], tz=datetime_timezone.utc))
    resources = list(original.resources)
    for old_id in installation.grants.filter(revoked_at__isnull=True).values_list("pk", flat=True):
        runtime.revoke_grant(old_id, owner=user)
    grant = AgentGrant.objects.create(installation=installation, generation=generation,
        scopes=list(native_installations.DOT_SCOPES), resources=resources, expires_at=expiry,
        max_concurrent_tasks=original.max_concurrent_tasks, daily_task_limit=original.daily_task_limit,
        daily_cost_limit_microunits=original.daily_cost_limit_microunits,
        max_task_seconds=original.max_task_seconds)
    # An expired private adapter records the durable bridge at consent time but
    # is not connection evidence. Successful unified bearer authentication
    # activates it; neither raw secret nor an alternate token is returned.
    credential = AgentCredential.objects.create(grant=grant, kind="access", generation=grant.generation,
        token_hash=token_hash(secrets.token_urlsafe(48)), audience=mlai_url(), scopes=list(grant.scopes), expires_at=now)
    AgentOAuthBinding.objects.create(credential=credential, client_id=PREFIX + unified_grant["id"])
    unified_grant.update(native_agent_grant_id=str(grant.pk), native_agent_credential_id=str(credential.pk))
    runtime.audit("unified.native.consented", grant=grant, client_id=unified_grant["client_id"], unified_grant_id=unified_grant["id"])
    return grant


@transaction.atomic
def revoke_native_grant(cached_grant):
    """Fence only the exact linked native authority; leave its installation reusable."""
    if not cached_grant.get("native_agent_grant_id"):
        return
    user = runtime.locked(get_user_model().objects.all()).get(pk=cached_grant["user_id"])
    identifier = _uuid(cached_grant["native_agent_grant_id"], "Invalid native binding.")
    grant = AgentGrant.objects.filter(pk=identifier, installation__owner=user).first()
    if grant is not None and AgentOAuthBinding.objects.filter(credential__grant=grant,
            client_id=PREFIX + cached_grant["id"]).exists():
        runtime.revoke_grant(grant.pk, owner=user)


def _private(principal, *, activate=False):
    current = oauth.valid_grant(principal.grant["id"])
    cached = current.grant
    if not enabled() or cached.get("resource") != mlai_url() or not DOT_SCOPES <= set(cached.get("scopes", [])):
        raise PermissionDenied("Explicit personal agent consent is required.", code="unified_consent_required")
    grant_id = _uuid(cached.get("native_agent_grant_id"), "Choose a signed personal agent installation.")
    credential_id = _uuid(cached.get("native_agent_credential_id"), "The personal agent connection is unavailable.")
    owner = runtime.locked(get_user_model().objects.all()).get(pk=current.user.pk)
    installation_id = AgentGrant.objects.filter(pk=grant_id, installation__owner=owner).values_list("installation_id", flat=True).first()
    installation = runtime.locked(AgentInstallation.objects.filter(pk=installation_id, owner=owner)).first()
    grant = runtime.locked(AgentGrant.objects.filter(pk=grant_id, installation=installation)).first()
    if grant is None:
        raise PermissionDenied("This agent belongs to another account.", code="unified_owner_changed")
    grant.installation = installation
    installation.owner = owner
    credential = runtime.locked(AgentCredential.objects.filter(pk=credential_id, grant=grant)).select_related("task").first()
    if credential is None or not AgentOAuthBinding.objects.filter(credential=credential, client_id=PREFIX + cached["id"]).exists():
        raise PermissionDenied("The private agent binding is unavailable.", code="unified_binding_unavailable")
    validate_grant_base(grant)
    if activate and credential.revoked_at is None and credential.expires_at <= timezone.now():
        assert_unified_authority(grant, credential_id=credential.pk, allow_pending=True)
        credential.expires_at = min(grant.expires_at, datetime.fromtimestamp(cached["expiresAt"], tz=datetime_timezone.utc))
        credential.save(update_fields=("expires_at",))
        runtime.audit("unified.native.authenticated", grant=grant, client_id=cached["client_id"])
    assert_unified_authority(grant, credential_id=credential.pk)
    credential.grant = grant
    return validate_credential(credential, audience=mlai_url(), allowed_kinds=("access",))


@_api_errors
@transaction.atomic
def on_authenticated(principal):
    """Called only after the unified endpoint accepted an actual bearer token."""
    return _private(principal, activate=True)


@_api_errors
@transaction.atomic
def principal_for(principal):
    return _private(principal)


def available_tools(principal):
    if not principal.grant.get("native_agent_grant_id") or not principal.grant.get("native_agent_credential_id") or not enabled():
        return []
    try:
        principal_for(principal)
    except (APIException, AgentPermissionDenied, runtime.AgentRuntimeError):
        return []
    result = deepcopy(TOOLS)
    for tool in result:
        security = [{"type": "oauth2", "scopes": TOOL_SCOPES[tool["name"]]}]
        tool["securitySchemes"] = security
        tool.setdefault("_meta", {})["securitySchemes"] = deepcopy(security)
    return result


def supports_events(principal):
    return bool(available_tools(principal) and "agent:mentions:listen" in principal.grant.get("scopes", []))


@_api_errors
@transaction.atomic
def call_tool(principal, name, arguments):
    if name not in NAMES or not set(TOOL_SCOPES[name]) <= set(principal.grant.get("scopes", [])):
        raise PermissionDenied("Reconnect MLAI with the required personal agent permission.", code="scope_denied")
    native = principal_for(principal)
    result = broker.execute(native, NAMES[name], arguments)
    if name == "get_agent_account":
        owner, installation = native.owner, native.grant.installation
        return {"id": str(installation.pk), "name": getattr(owner, "full_name", "") or owner.email,
                "email": owner.email, "nickname": getattr(owner, "first_name", "") or installation.name}
    if name == "reply_to_invocation":
        AgentInvocationReply.objects.filter(invocation_id=result["invocation_id"], status__in=("pending", "unknown")).update(
            receipt={"unified_credential_id": str(native.credential.pk)})
    return result


@_api_errors
@transaction.atomic
def handle_events(principal, method, params):
    if "agent:mentions:listen" not in principal.grant.get("scopes", []):
        raise PermissionDenied("Reconnect MLAI to explicitly enable mention notifications.", code="scope_denied")
    native = principal_for(principal)
    result = event_webhooks.handle_method(native, method, params)
    if method == "events/subscribe":
        row = runtime.locked(AgentSubscription.objects.filter(pk=result["id"], grant=native.grant)).get()
        row.delivery = {**row.delivery, "unified_credential_id": str(native.credential.pk)}
        row.save(update_fields=("delivery",))
    return result
