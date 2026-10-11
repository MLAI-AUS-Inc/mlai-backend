"""Preregistered public OAuth clients, authorization code + S256 and rotation.

No dynamic redirect registration, password grant, upstream token passthrough,
implicit flow, or reusable authorization code is supported.
"""
import base64
import hashlib
import re
import secrets
import uuid
from datetime import timedelta
from urllib.parse import urlsplit, parse_qsl, urlencode, urlunsplit

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import AuthenticationFailed, PermissionDenied, ValidationError

from .authentication import token_hash, validate_credential
from .models import AgentCredential, AgentGrant, AgentInstallation, AgentAuditEvent
from .oauth_models import AgentOAuthCode, AgentOAuthBinding
from .policy import SCOPES, validate_grant_base

PKCE_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")
VERIFIER_RE = re.compile(r"^[A-Za-z0-9._~-]{43,128}$")
READ_SCOPES = frozenset(SCOPES - {"messages:write"})


def permitted_scopes():
    allowed = set(SCOPES if getattr(settings, "AGENT_RUNTIME_ACTIONS_ENABLED", False) else READ_SCOPES)
    if not getattr(settings, "AGENT_RUNTIME_PUBLIC_WEB_ENABLED", False):
        allowed.discard("web:access")
    return allowed


def configured_client(client_id):
    clients = getattr(settings, "AGENT_RUNTIME_OAUTH_CLIENTS", {})
    client = clients.get(client_id) if isinstance(clients, dict) else None
    if not isinstance(client, dict) or client.get("enabled", True) is not True:
        raise ValidationError({"error": "invalid_client"})
    return client


def validate_authorization(params):
    if not hasattr(params, "get"):
        raise ValidationError({"error": "invalid_request"})
    client_id = str(params.get("client_id", ""))
    client = configured_client(client_id)
    redirect = str(params.get("redirect_uri", ""))
    try:
        parts = urlsplit(redirect)
    except ValueError as exc:
        raise ValidationError({"error": "invalid_redirect_uri"}) from exc
    valid_transport = parts.scheme == "https" or (
        parts.scheme == "http" and parts.hostname in {"127.0.0.1", "[::1]", "::1"})
    if (not valid_transport or not parts.hostname or parts.username or parts.password or parts.fragment
            or any(key in {"code", "state"} for key, _ in parse_qsl(parts.query))
            or redirect not in client.get("redirect_uris", [])):
        raise ValidationError({"error": "invalid_redirect_uri"})
    scopes = str(params.get("scope", "")).split()
    allowed = set(client.get("scopes", [])) & permitted_scopes()
    resource = str(params.get("resource", ""))
    challenge = str(params.get("code_challenge", ""))
    state = str(params.get("state", ""))
    if (params.get("response_type") != "code" or params.get("code_challenge_method") != "S256"
            or not PKCE_RE.fullmatch(challenge) or not scopes or not set(scopes).issubset(allowed)
            or resource != getattr(settings, "AGENT_RUNTIME_RESOURCE", "")
            or not resource or not state or len(state) > 1024):
        raise ValidationError({"error": "invalid_request"})
    return {"client_id": client_id, "client_name": str(client.get("name") or client_id)[:120],
            "redirect_uri": redirect, "scopes": sorted(set(scopes)), "audience": resource,
            "code_challenge": challenge, "state": state}


def issue_credential(grant, *, kind, audience, scopes, expires_at, task=None, family=None, client_id=None):
    raw = "mlai_agent_" + secrets.token_urlsafe(48)
    credential = AgentCredential.objects.create(
        grant=grant, kind=kind, token_hash=token_hash(raw), generation=grant.generation,
        audience=audience, scopes=list(scopes), expires_at=expires_at,
        task=task, lease_epoch=task.lease_epoch if task else None, refresh_family=family,
    )
    if client_id:
        AgentOAuthBinding.objects.create(credential=credential, client_id=client_id)
    return raw, credential


def issue_pair(grant, *, client_id, scopes, audience, family=None):
    now = timezone.now()
    family = family or uuid.uuid4()
    access_expiry = min(grant.expires_at, now + timedelta(seconds=900))
    access, _ = issue_credential(grant, kind="access", audience=audience, scopes=scopes,
                                  expires_at=access_expiry, family=family, client_id=client_id)
    refresh, row = issue_credential(grant, kind="refresh", audience=audience, scopes=scopes,
                                     expires_at=grant.expires_at, family=family, client_id=client_id)
    return {"token_type": "Bearer", "access_token": access, "refresh_token": refresh,
            "expires_in": max(0, int((access_expiry - now).total_seconds())),
            "scope": " ".join(scopes)}, row


def create_grant(owner, *, name, runtime, scopes, resources, expires_in_days):
    from .sources import live_access
    if not set(scopes).issubset(permitted_scopes()) or not scopes:
        raise PermissionDenied("The requested capabilities are not enabled for this pilot.")
    if not resources or len(resources) > 20:
        raise ValidationError("Select one to twenty channels.")
    # Existing user-first OAuth/revocation lock also fences this consent snapshot.
    with transaction.atomic():
        current = get_user_model().objects.select_for_update().get(pk=owner.pk)
        if not current.is_active:
            raise PermissionDenied("Account is no longer active.")
        for resource in resources:
            if live_access(current, "messages:read", resource) is not True:
                raise PermissionDenied("Channel is not available to this member.")
        installation = AgentInstallation.objects.create(owner=current, name=name, runtime=runtime)
        grant = AgentGrant.objects.create(installation=installation, scopes=sorted(set(scopes)),
                                         resources=sorted(set(resources)),
                                         expires_at=timezone.now() + timedelta(days=expires_in_days))
        AgentAuditEvent.objects.create(grant=grant, event_type="grant.consented",
                                      metadata={"owner_id": owner.pk, "consent_version": "agent-public-channels-v1"})
        return grant


def authorize_client(owner, authorization, consent):
    if not set(consent["scopes"]).issubset(set(authorization["scopes"])):
        raise PermissionDenied("Consent exceeds requested client scopes.")
    with transaction.atomic():
        grant = create_grant(owner, **consent)
        raw_code = secrets.token_urlsafe(48)
        AgentOAuthCode.objects.create(code_hash=token_hash(raw_code), grant=grant,
            generation=grant.generation, client_id=authorization["client_id"],
            redirect_uri=authorization["redirect_uri"], code_challenge=authorization["code_challenge"],
            audience=authorization["audience"], scopes=consent["scopes"],
            expires_at=timezone.now() + timedelta(minutes=3))
    parts = urlsplit(authorization["redirect_uri"])
    query = parse_qsl(parts.query, keep_blank_values=True) + [("code", raw_code), ("state", authorization["state"])]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


def exchange_code(data):
    configured_client(data.get("client_id"))
    code_hash = token_hash(str(data.get("code", "")))
    identity = AgentOAuthCode.objects.filter(code_hash=code_hash).values("grant__installation__owner_id").first()
    if identity is None:
        raise AuthenticationFailed("invalid_grant")
    with transaction.atomic():
        get_user_model().objects.select_for_update().get(pk=identity["grant__installation__owner_id"])
        code = AgentOAuthCode.objects.select_for_update().select_related("grant__installation__owner").get(code_hash=code_hash)
        verifier = str(data.get("code_verifier", ""))
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        now = timezone.now()
        if (code.consumed_at is not None or code.expires_at <= now
                or code.client_id != data.get("client_id") or code.redirect_uri != data.get("redirect_uri")
                or code.audience != data.get("resource") or not VERIFIER_RE.fullmatch(verifier)
                or not secrets.compare_digest(challenge, code.code_challenge)):
            raise AuthenticationFailed("invalid_grant")
        validate_grant_base(code.grant, expected_generation=code.generation)
        code.consumed_at = now
        code.save(update_fields=["consumed_at"])
        payload, _ = issue_pair(code.grant, client_id=code.client_id, scopes=code.scopes, audience=code.audience)
        return payload


def rotate_refresh(data):
    configured_client(data.get("client_id"))
    hashed = token_hash(str(data.get("refresh_token", "")))
    identity = AgentCredential.objects.filter(token_hash=hashed).values("grant__installation__owner_id").first()
    if identity is None:
        raise AuthenticationFailed("invalid_grant")
    replay = False
    payload = None
    with transaction.atomic():
        get_user_model().objects.select_for_update().get(pk=identity["grant__installation__owner_id"])
        credential = AgentCredential.objects.select_for_update().select_related("grant__installation__owner", "oauth_binding").get(token_hash=hashed)
        binding = getattr(credential, "oauth_binding", None)
        if (credential.kind != "refresh" or binding is None or binding.client_id != data.get("client_id")
                or credential.audience != data.get("resource")):
            raise AuthenticationFailed("invalid_grant")
        if credential.revoked_at is not None or credential.replacement_id:
            AgentCredential.objects.filter(grant=credential.grant, refresh_family=credential.refresh_family,
                                           revoked_at__isnull=True).update(revoked_at=timezone.now())
            replay = True
        else:
            validate_credential(credential, audience=data.get("resource"), allowed_kinds=("refresh",))
            scopes = str(data.get("scope", " ".join(credential.scopes))).split()
            if not scopes or not set(scopes).issubset(set(credential.scopes)):
                raise AuthenticationFailed("invalid_scope")
            payload, replacement = issue_pair(credential.grant, client_id=binding.client_id,
                scopes=sorted(set(scopes)), audience=credential.audience, family=credential.refresh_family)
            credential.revoked_at = timezone.now()
            credential.replacement = replacement
            credential.save(update_fields=["revoked_at", "replacement"])
    # Raise after committing the replay fence; raising inside atomic undoes it.
    if replay:
        raise AuthenticationFailed("invalid_grant")
    return payload
