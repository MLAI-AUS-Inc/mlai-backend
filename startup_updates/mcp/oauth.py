"""PKCE OAuth grants bound to one founder, startup, and revocable account session.

Security state is stored in the existing shared Redis cache. Losing that state
revokes access. Cached access/refresh tokens and authorization codes are hashed.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import time
from contextlib import contextmanager
from dataclasses import dataclass
from urllib.parse import urlencode, urlsplit
from uuid import UUID

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.utils import timezone
from rest_framework.exceptions import AuthenticationFailed, PermissionDenied, ValidationError

from community_chat.account_sessions import InvalidAccountSession, _valid_session, _validate_device_owner
from community_chat.models import CommunityChatAccountSession
from community_chat.onboarding import require_community_access
from founder_tools.models import VibeRaisingCompany
from .config import SCOPES, mcp_url, public_base

CODE_TTL = 300
INTENT_TTL = 600
GRANT_TTL = 30 * 86400
ACCESS_TTL = 3600
PREFIX = "valley-mcp:"
_CURSOR_OAUTH_REDIRECT_URI = "cursor://anysphere.cursor-mcp/oauth/callback"


class OAuthError(ValueError):
    """An OAuth error safe to disclose without credential detail."""
    def __init__(self, error, description=None):
        self.error = error
        self.description = description or error.replace("_", " ")
        super().__init__(self.description)


def digest(value):
    return hashlib.sha256(str(value).encode()).hexdigest()


def key(kind, value):
    return PREFIX + kind + ":" + digest(value)


@contextmanager
def lock(kind, value):
    lock_key = key("lock-" + kind, value)
    nonce = secrets.token_urlsafe(24)
    # Single-use credential locks last as long as that credential. Index locks
    # are short: revocation epochs, not the index, authoritatively revoke access.
    ttl = {"index": 60, "intent": INTENT_TTL + 1, "code": CODE_TTL + 1}.get(kind, GRANT_TTL)
    if not cache.add(lock_key, nonce, timeout=ttl):
        raise OAuthError("temporarily_unavailable", "This request is already being processed.")
    try:
        yield
    finally:
        if cache.get(lock_key) == nonce:
            cache.delete(lock_key)


def redirect_uri(value):
    if not isinstance(value, str) or len(value) > 2048 or any(ord(char) < 33 for char in value):
        raise OAuthError("invalid_client_metadata", "Use a valid callback URL.")
    try:
        parsed = urlsplit(value)
        parsed.port  # Reject malformed/non-numeric ports during validation.
    except ValueError as exc:
        raise OAuthError("invalid_client_metadata", "Use a valid callback URL.") from exc
    loopback = parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    # Cursor registers this native fallback alongside its HTTPS and loopback
    # callbacks. Keep the exception exact; its install-link handler is different.
    cursor_callback = value == _CURSOR_OAUTH_REDIRECT_URI
    if (parsed.scheme != "https" and not loopback and not cursor_callback) or not parsed.netloc or parsed.username or parsed.password or parsed.fragment:
        raise OAuthError("invalid_client_metadata", "Use HTTPS, a local loopback URL or the supported Cursor OAuth callback.")
    return value


def callback_origin(value):
    parsed = urlsplit(redirect_uri(value))
    return f"{parsed.scheme}://{parsed.netloc}"


def register_client(data):
    if not isinstance(data, dict) or not isinstance(data.get("redirect_uris"), list) or not 1 <= len(data["redirect_uris"]) <= 10:
        raise OAuthError("invalid_client_metadata")
    if data.get("token_endpoint_auth_method", "none") != "none":
        raise OAuthError("invalid_client_metadata", "Only public PKCE clients are supported.")
    grant_types = data.get("grant_types", ["authorization_code", "refresh_token"])
    if not isinstance(grant_types, list) or any(not isinstance(item, str) for item in grant_types) or set(grant_types) - {"authorization_code", "refresh_token"} or data.get("response_types", ["code"]) != ["code"]:
        raise OAuthError("invalid_client_metadata")
    client_id = "valley_client_" + secrets.token_urlsafe(32)
    client = {"client_id": client_id, "client_name": str(data.get("client_name") or "Your AI agent")[:100],
        "redirect_uris": [redirect_uri(item) for item in data["redirect_uris"]],
        "token_endpoint_auth_method": "none", "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"]}
    # Hosts reuse a registered public client across grant expiry and later
    # reauthorisation. Expiring its metadata would strand that connection.
    cache.set(key("client", client_id), client, timeout=None)
    return client


def client_for(client_id):
    static = getattr(settings, "VALLEY_MCP_OAUTH_CLIENTS", {})
    client = static.get(client_id) if isinstance(static, dict) else None
    client = client or cache.get(key("client", client_id))
    if not isinstance(client, dict):
        raise OAuthError("invalid_client")
    return client


def create_intent(query):
    client_id = str(query.get("client_id") or "")
    client = client_for(client_id)
    callback = redirect_uri(query.get("redirect_uri"))
    if callback not in client.get("redirect_uris", []):
        raise OAuthError("invalid_request", "The callback URL does not match the registered client.")
    if query.get("response_type") != "code":
        raise OAuthError("unsupported_response_type")
    if query.get("resource") != mcp_url():
        raise OAuthError("invalid_target", "Choose the Valley MCP resource.")
    challenge = str(query.get("code_challenge") or "")
    if query.get("code_challenge_method") != "S256" or not re.fullmatch(r"[A-Za-z0-9_-]{43}", challenge):
        raise OAuthError("invalid_request", "PKCE S256 is required.")
    scopes = set(str(query.get("scope") or " ".join(sorted(SCOPES))).split())
    if not scopes or scopes - SCOPES:
        raise OAuthError("invalid_scope")
    state = str(query.get("state") or "")
    if not state or len(state) > 2048:
        raise OAuthError("invalid_request", "An OAuth state value is required.")
    request_id = secrets.token_urlsafe(32)
    value = {"requestId": request_id, "client_id": client_id, "clientName": str(client.get("client_name") or "Your AI agent"),
        "redirect_uri": callback, "redirectOrigin": callback_origin(callback), "state": state, "code_challenge": challenge,
        "scopes": sorted(scopes), "resource": mcp_url(), "expiresAt": int(time.time()) + INTENT_TTL}
    cache.set(key("intent", request_id), value, timeout=INTENT_TTL)
    return value


def intent_for(request_id):
    value = cache.get(key("intent", request_id))
    if not value or value.get("expiresAt", 0) <= time.time():
        raise ValidationError("This agent connection expired. Start again in your agent.")
    client = client_for(value["client_id"])
    if value["redirect_uri"] not in client.get("redirect_uris", []) or value["resource"] != mcp_url() or set(value["scopes"]) - SCOPES:
        raise ValidationError("This agent connection is no longer available.")
    return value


def callback_url(intent, **parameters):
    separator = "&" if "?" in intent["redirect_uri"] else "?"
    return intent["redirect_uri"] + separator + urlencode({**parameters, "state": intent["state"], "iss": public_base()})


def _company_uuid(value):
    """Parse founder-company IDs once so scope and cache keys use model UUIDs."""
    try:
        if not isinstance(value, (str, UUID)):
            raise ValueError
        return UUID(str(value))
    except ValueError as exc:
        raise PermissionDenied("Choose an authorised startup.") from exc


def company_for(user, company_id, grant=None):
    company_id = _company_uuid(company_id)
    if grant and company_id != _company_uuid(grant["company_id"]):
        raise PermissionDenied("This agent has access to a different startup.")
    company = VibeRaisingCompany.objects.select_related("organization", "profile").filter(pk=company_id, profile__user=user).first()
    if company is None or company.organization is None:
        raise PermissionDenied("This startup is unavailable to this account.")
    return company


def assert_device_owner(session):
    try:
        _validate_device_owner(session)
    except InvalidAccountSession as exc:
        raise AuthenticationFailed("Account device authorisation was revoked.") from exc


def approve_intent(request_id, *, user, session, company_id, approve):
    if not isinstance(approve, bool):
        raise ValidationError("Choose whether to connect this agent.")
    with lock("intent", request_id):
        intent = intent_for(request_id)
        if not _valid_session(session, timezone.now()) or session.user_id != user.pk:
            raise AuthenticationFailed("Sign in again to connect your agent.")
        assert_device_owner(session)
        if not approve:
            cache.delete(key("intent", request_id))
            return callback_url(intent, error="access_denied")
        company = company_for(user, company_id)
        cache.delete(key("intent", request_id))
        grant_id = secrets.token_urlsafe(32)
        value = {"id": grant_id, "user_id": user.pk, "auth_version": user.auth_version,
            "session_id": str(session.pk), "company_id": str(company.pk), "client_id": intent["client_id"],
            "clientName": intent["clientName"], "redirectOrigin": intent["redirectOrigin"], "scopes": intent["scopes"], "resource": intent["resource"],
            "createdAt": timezone.now().isoformat(), "expiresAt": int(time.time()) + GRANT_TTL, "revoked": False,
            "revocation_epoch": cache.get(key("epoch", f"{user.pk}:{company.pk}")) or ""}
        cache.set(key("grant", grant_id), value, timeout=GRANT_TTL)
        with lock("index", f"{user.pk}:{company.pk}"):
            index_key = key("index", f"{user.pk}:{company.pk}")
            cache.set(index_key, [*(cache.get(index_key) or []), grant_id], timeout=GRANT_TTL)
        code = secrets.token_urlsafe(48)
        cache.set(key("code", code), {"grant_id": grant_id, "client_id": intent["client_id"],
            "redirect_uri": intent["redirect_uri"], "code_challenge": intent["code_challenge"],
            "resource": intent["resource"]}, timeout=CODE_TTL)
        return callback_url(intent, code=code)


@dataclass(frozen=True)
class Principal:
    """A validated account with a startup-scoped MCP grant."""
    user: object
    grant: dict


def valid_grant(grant_id, *, scope=None):
    grant = cache.get(key("grant", grant_id))
    if not grant or grant.get("revoked") or grant.get("expiresAt", 0) <= time.time() or grant.get("resource") != mcp_url():
        raise AuthenticationFailed("Agent authorisation expired or was revoked.")
    current_epoch = cache.get(key("epoch", f"{grant['user_id']}:{_company_uuid(grant['company_id'])}")) or ""
    if grant.get("revocation_epoch", "") != current_epoch:
        raise AuthenticationFailed("This startup agent connection was disconnected.")
    user = get_user_model().objects.filter(pk=grant["user_id"], is_active=True).first()
    if user is None or user.auth_version != grant["auth_version"]:
        raise AuthenticationFailed("Account authorisation was revoked.")
    session = CommunityChatAccountSession.objects.select_related("user").filter(pk=grant["session_id"], user=user).first()
    if not _valid_session(session, timezone.now()):
        raise AuthenticationFailed("Account session was revoked.")
    assert_device_owner(session)
    require_community_access(user)
    company_for(user, grant["company_id"], grant)
    if scope and scope not in grant.get("scopes", []):
        raise PermissionDenied("This agent does not have the required permission.")
    return Principal(user=user, grant=grant)


def issue_tokens(grant_id):
    principal = valid_grant(grant_id)
    remaining = max(1, int(principal.grant["expiresAt"] - time.time()))
    access = "valley_access_" + secrets.token_urlsafe(48)
    refresh = "valley_refresh_" + secrets.token_urlsafe(48)
    cache.set(key("access", access), {"grant_id": grant_id}, timeout=min(ACCESS_TTL, remaining))
    cache.set(key("refresh", refresh), {"grant_id": grant_id, "client_id": principal.grant["client_id"]}, timeout=remaining)
    return {"access_token": access, "token_type": "Bearer", "expires_in": min(ACCESS_TTL, remaining),
        "refresh_token": refresh, "scope": " ".join(principal.grant["scopes"])}


def token_exchange(data):
    if not isinstance(data, dict) and not hasattr(data, "get"):
        raise OAuthError("invalid_request", "Token requests must contain named fields.")
    client_id = str(data.get("client_id") or "")
    client_for(client_id)
    if data.get("resource") != mcp_url():
        raise OAuthError("invalid_target")
    if data.get("grant_type") == "authorization_code":
        code = str(data.get("code") or "")
        with lock("code", code):
            record = cache.get(key("code", code))
            if not record or record["client_id"] != client_id or record["redirect_uri"] != data.get("redirect_uri") or record["resource"] != mcp_url():
                raise OAuthError("invalid_grant")
            verifier = str(data.get("code_verifier") or "")
            if not re.fullmatch(r"[A-Za-z0-9._~-]{43,128}", verifier):
                raise OAuthError("invalid_grant")
            expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
            if not hmac.compare_digest(expected, record["code_challenge"]):
                raise OAuthError("invalid_grant")
            cache.delete(key("code", code))
            return issue_tokens(record["grant_id"])
    if data.get("grant_type") == "refresh_token":
        refresh = str(data.get("refresh_token") or "")
        with lock("refresh", refresh):
            record = cache.get(key("refresh", refresh))
            if not record or record["client_id"] != client_id:
                raise OAuthError("invalid_grant")
            if record.get("spent"):
                # A correct-client replay signals a compromised token family.
                # Keep only the hashed-token linkage until grant expiry so all
                # rotated descendants are revoked through their shared grant.
                revoke_grant(record["grant_id"])
                raise OAuthError("invalid_grant")
            principal = valid_grant(record["grant_id"])
            requested = set(str(data.get("scope") or " ".join(principal.grant["scopes"])).split())
            if requested != set(principal.grant["scopes"]):
                raise OAuthError("invalid_scope", "Refresh cannot alter the authorised scopes.")
            remaining = max(1, int(principal.grant["expiresAt"] - time.time()))
            cache.set(key("refresh", refresh), {**record, "spent": True}, timeout=remaining)
            return issue_tokens(record["grant_id"])
    raise OAuthError("unsupported_grant_type")


def authenticate_token(raw_token, *, scope=None):
    if not str(raw_token).startswith("valley_access_"):
        raise AuthenticationFailed("Connect this agent to Valley first.")
    record = cache.get(key("access", raw_token))
    if not record:
        raise AuthenticationFailed("Agent access token expired.")
    return valid_grant(record["grant_id"], scope=scope)


def revoke_grant(grant_id):
    grant = cache.get(key("grant", grant_id))
    if grant:
        grant["revoked"] = True
        cache.set(key("grant", grant_id), grant, timeout=max(1, int(grant["expiresAt"] - time.time())))


def disconnect_company(user, company_id):
    company_id = _company_uuid(company_id)
    epoch = secrets.token_urlsafe(32)
    cache.set(key("epoch", f"{user.pk}:{company_id}"), epoch, timeout=GRANT_TTL + 1)
    for grant_id in cache.get(key("index", f"{user.pk}:{company_id}")) or []:
        grant = cache.get(key("grant", grant_id))
        if grant and grant["user_id"] == user.pk and str(grant["company_id"]) == str(company_id) and grant.get("revocation_epoch", "") != epoch:
            revoke_grant(grant_id)


def grants_for(user, company_id):
    company_id = _company_uuid(company_id)
    rows = []
    for grant_id in cache.get(key("index", f"{user.pk}:{company_id}")) or []:
        grant = cache.get(key("grant", grant_id))
        if grant and not grant.get("revoked") and grant.get("expiresAt", 0) > time.time():
            try:
                valid_grant(grant_id)
            except (AuthenticationFailed, PermissionDenied):
                continue
            rows.append({field: grant[field] for field in ("id", "clientName", "redirectOrigin", "createdAt", "expiresAt")})
    return rows


def metadata():
    base = public_base()
    return {"issuer": base, "authorization_endpoint": base + "/mcp/oauth/authorize", "token_endpoint": base + "/mcp/oauth/token",
        "registration_endpoint": base + "/mcp/oauth/register", "revocation_endpoint": base + "/mcp/oauth/revoke",
        "response_types_supported": ["code"], "grant_types_supported": ["authorization_code", "refresh_token"],
        "token_endpoint_auth_methods_supported": ["none"], "code_challenge_methods_supported": ["S256"],
        "scopes_supported": sorted(SCOPES), "authorization_response_iss_parameter_supported": True}
