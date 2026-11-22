"""PKCE OAuth grants bound to one founder account and startup.

Startup and article grants follow the account (user id and auth_version), so
signing in again does not end them. Community and personal-agent grants act
through one device session and stay bound to it. Security state is stored in
the existing shared Redis cache. Losing that state revokes access. Cached
access/refresh tokens and authorization codes are hashed.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone as dt_timezone
from urllib.parse import urlencode, urlsplit
from uuid import UUID

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db.models import Q
from django.utils import timezone
from rest_framework.exceptions import APIException, AuthenticationFailed, PermissionDenied, ValidationError

from community_chat.account_sessions import InvalidAccountSession, _valid_session, _validate_device_owner
from community_chat.models import CommunityChatAccountSession, CommunityChatDevice
from community_chat.onboarding import require_community_access
from founder_tools.membership import active_memberships, companies_for_user
from founder_tools.models import VibeRaisingCompany
from .config import ALL_SCOPES, DOT_MESSAGES_SCOPE, DOT_SCOPES, SCOPES, availability, mcp_url, mlai_url, public_base, resource_scopes

CODE_TTL = 300
INTENT_TTL = 600
# Fixed lifetime of personal-agent consents, which only the owner's signed
# renewal extends. Every other grant slides: see grant_idle_ttl().
GRANT_TTL = 30 * 86400
ACCESS_TTL = 3600
REFRESH_LOCK_TTL = 30
REFRESH_LOCK_WAIT = 3
# The listing index may outlive grants (dead entries are pruned), never the reverse.
INDEX_TTL = 730 * 86400
PREFIX = "valley-mcp:"
_CURSOR_OAUTH_REDIRECT_URI = "cursor://anysphere.cursor-mcp/oauth/callback"


class OAuthError(ValueError):
    """An OAuth error safe to disclose without credential detail."""
    def __init__(self, error, description=None):
        self.error = error
        self.description = description or error.replace("_", " ")
        super().__init__(self.description)


class AccessProblem(PermissionDenied):
    """The account, startup or origin blocks this request; more scopes would not help.

    A PermissionDenied subclass so existing callers keep failing closed, while
    transports can report ``reason_code`` instead of an insufficient_scope challenge.
    """
    def __init__(self, detail, reason_code):
        super().__init__(detail)
        self.reason_code = reason_code


MEMBERSHIP_REASON = "Your MLAI community membership is not approved yet. Finish joining MLAI, then connect your AI again."


def grant_idle_ttl():
    """Seconds an unused startup grant stays alive; each refresh restarts it."""
    try:
        days = int(getattr(settings, "MLAI_MCP_GRANT_IDLE_TTL_DAYS", 180))
    except (TypeError, ValueError):
        days = 180
    return max(1, days) * 86400


def replay_grace():
    """Seconds a just-rotated refresh token may be retried for the same successor."""
    try:
        return max(0, min(300, int(getattr(settings, "MLAI_MCP_REFRESH_REPLAY_GRACE_SECONDS", 60))))
    except (TypeError, ValueError):
        return 0


def normal_resource(value):
    """Treat a trailing slash as the same audience; absent stays absent."""
    return value.strip().rstrip("/") or None if isinstance(value, str) else None


def session_bound(scopes):
    """Community and personal-agent tools act through the consenting device session."""
    return any(not scope.startswith(("startup:", "article:")) for scope in scopes)


def digest(value):
    return hashlib.sha256(str(value).encode()).hexdigest()


def key(kind, value):
    return PREFIX + kind + ":" + digest(value)


@contextmanager
def lock(kind, value, *, wait=0):
    lock_key = key("lock-" + kind, value)
    nonce = secrets.token_urlsafe(24)
    # Intent and code locks last as long as that single-use credential. Others
    # are short, so a worker that dies inside one cannot strand the connection:
    # revocation epochs and tombstones, not locks, authoritatively revoke access.
    ttl = {"intent": INTENT_TTL + 1, "code": CODE_TTL + 1, "refresh": REFRESH_LOCK_TTL}.get(kind, 60)
    deadline = time.monotonic() + wait
    while not cache.add(lock_key, nonce, timeout=ttl):
        # A parallel refresh of the same token waits briefly for the first one,
        # then receives its successor through the replay grace window.
        if time.monotonic() >= deadline:
            raise OAuthError("temporarily_unavailable", "This request is already being processed.")
        time.sleep(0.1)
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
    # Some hosts (Codex before rust-v0.142.0) send no resource at authorize.
    # Default to the primary server; a value that is present but unknown fails.
    resource = normal_resource(query.get("resource"))
    if query.get("resource") in (None, ""):
        resource = mlai_url() if getattr(settings, "MLAI_MCP_ENABLED", False) else mcp_url()
    supported = resource_scopes(resource)
    if not supported:
        raise OAuthError("invalid_target", "Choose the MLAI or Valley MCP resource.")
    if resource == mlai_url() and not availability(resource)[0]:
        raise OAuthError("temporarily_unavailable", "MLAI agent connections are not enabled yet.")
    challenge = str(query.get("code_challenge") or "")
    if query.get("code_challenge_method") != "S256" or not re.fullmatch(r"[A-Za-z0-9_-]{43}", challenge):
        raise OAuthError("invalid_request", "PKCE S256 is required.")
    # Some native hosts omit scope entirely. Offer the new catalogue for
    # explicit consent; the UI selects only basic reads by default and approval
    # requires an explicit subset. Existing tokens/refresh are never expanded.
    default = SCOPES if resource == mcp_url() else ALL_SCOPES
    # Hosts may add generic scopes such as offline_access. Ignore what this
    # resource does not offer; a request with nothing supported still fails.
    scopes = set(str(query.get("scope") or " ".join(sorted(default))).split()) & supported
    if not scopes:
        raise OAuthError("invalid_scope")
    state = str(query.get("state") or "")
    if not state or len(state) > 2048:
        raise OAuthError("invalid_request", "An OAuth state value is required.")
    request_id = secrets.token_urlsafe(32)
    value = {"requestId": request_id, "client_id": client_id, "clientName": str(client.get("client_name") or "Your AI agent"),
        "redirect_uri": callback, "redirectOrigin": callback_origin(callback), "state": state, "code_challenge": challenge,
        "scopes": sorted(scopes), "resource": resource, "expiresAt": int(time.time()) + INTENT_TTL}
    cache.set(key("intent", request_id), value, timeout=INTENT_TTL)
    return value


def intent_for(request_id):
    value = cache.get(key("intent", request_id))
    if not value or value.get("expiresAt", 0) <= time.time():
        raise ValidationError("This agent connection expired. Start again in your agent.")
    if value.get("resource") == mlai_url() and not availability(value["resource"])[0]:
        raise ValidationError("MLAI agent connections are not enabled yet.")
    client = client_for(value["client_id"])
    if value["redirect_uri"] not in client.get("redirect_uris", []) or not resource_scopes(value["resource"]) or set(value["scopes"]) - resource_scopes(value["resource"]):
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
        raise AccessProblem("This connection was approved for a different startup.", "startup_not_authorised")
    company = companies_for_user(user).filter(pk=company_id).first()
    if company is None or company.organization is None:
        raise AccessProblem("This startup is unavailable to this account.", "startup_unavailable")
    if grant and company.membership_policy == VibeRaisingCompany.SHARED_ACCESS:
        from django.utils.dateparse import parse_datetime

        member = active_memberships(company).filter(user=user).first()
        granted_at = parse_datetime(str(grant.get("createdAt") or ""))
        if member is None or (
            member.source not in {"legacy_creator", "legacy_roster"}
            and (granted_at is None or timezone.is_naive(granted_at) or granted_at < member.joined_at)
        ):
            raise AccessProblem("Connect your agent again for this startup.", "startup_unavailable")
    return company


def assert_device_owner(session):
    try:
        _validate_device_owner(session)
    except InvalidAccountSession as exc:
        raise AuthenticationFailed("Account device authorisation was revoked.") from exc


def account_binding(session):
    """Grant fields that let a startup grant outlive its session but not its device."""
    try:
        installation_id = str(UUID(str(getattr(session, "installation_id", ""))))
    except ValueError:
        installation_id = ""
    public_key = getattr(session, "public_key", "")
    return {"binding": "account", "boundAt": time.time(), "installation_id": installation_id,
        "public_key": public_key.strip().lower() if isinstance(public_key, str) else ""}


def end_installation_grants(user_id, installation_id):
    """Record a sign-out: account-bound grants that device approved end with it.

    Signing in again revokes the old session too, which must not end them, so
    only an explicit sign-out (and device revocation, checked in the database)
    does. The marker never expires: grants can slide indefinitely.
    """
    if installation_id:
        cache.set(key("signed-out", f"{user_id}:{installation_id}"), time.time(), timeout=None)


def assert_installation_live(user, grant):
    """Fail an account-bound grant once its approving device signed out or was revoked."""
    bound_at = float(grant.get("boundAt") or 0)
    installation_id, public_key = grant.get("installation_id") or "", grant.get("public_key") or ""
    signed_out = cache.get(key("signed-out", f"{user.pk}:{installation_id}")) if installation_id else None
    if signed_out is not None and float(signed_out) >= bound_at:
        raise AuthenticationFailed("You signed out of MLAI on the device that approved this connection.")
    device = Q(public_key=public_key) if public_key else Q()
    if installation_id:
        device = (device | Q(installation_id=installation_id)) if public_key else Q(installation_id=installation_id)
    if (public_key or installation_id) and CommunityChatDevice.objects.filter(device, user_id=user.pk,
            revoked_at__gte=datetime.fromtimestamp(bound_at, tz=dt_timezone.utc)).exists():
        raise AuthenticationFailed("The device that approved this connection was removed from your account.")


def grant_subject(company_id):
    """Use one canonical revocation/index identity for a company or account grant."""
    return str(_company_uuid(company_id)) if company_id else "account"


def approve_intent(request_id, *, user, session, company_id, approve, scopes=None, community_channel_ids=None, native_installation_id=None):
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
        if intent["resource"] == mlai_url() and scopes is None:
            raise ValidationError("Choose the MLAI capabilities to authorise.")
        if scopes is not None and (not isinstance(scopes, list) or any(not isinstance(item, str) for item in scopes)):
            raise ValidationError("Permissions must be a list of scope names.")
        selected = set(intent["scopes"] if scopes is None else scopes)
        if not selected or selected - set(intent["scopes"]):
            raise ValidationError("Choose only permissions requested by this agent.")
        legacy = intent["resource"] == mcp_url()
        if legacy and selected != set(intent["scopes"]):
            raise ValidationError("Reconnect Valley to change its permissions.")
        native = bool(selected & DOT_SCOPES)
        if native and (legacy or not DOT_SCOPES <= selected or not native_installation_id):
            raise ValidationError("Select all personal agent permissions and an enrolled agent.")
        if not native and native_installation_id:
            raise ValidationError("Select personal agent permissions before choosing an agent.")
        if DOT_MESSAGES_SCOPE in selected and not native:
            raise ValidationError("Select personal agent permissions before letting the agent message people.")
        needs_company = legacy or any(scope.startswith(("startup:", "article:")) for scope in selected)
        company = company_for(user, company_id) if needs_company or (company_id and not native) else None
        channels = []
        if selected & {"community:read", "community:write"}:
            from .community import validate_channel_selection
            channels = validate_channel_selection(user, session, community_channel_ids)
        elif community_channel_ids:
            raise ValidationError("Select Community permissions before choosing channels.")
        grant_id = secrets.token_urlsafe(32)
        value = {"id": grant_id, "user_id": user.pk, "auth_version": user.auth_version,
            "session_id": str(session.pk), "company_id": str(company.pk) if company else None, "client_id": intent["client_id"],
            "clientName": intent["clientName"], "redirectOrigin": intent["redirectOrigin"], "scopes": sorted(selected), "resource": intent["resource"], "community_channels": channels,
            "createdAt": timezone.now().isoformat(), "expiresAt": int(time.time()) + (GRANT_TTL if native else grant_idle_ttl()), "revoked": False,
            **({} if session_bound(selected) else account_binding(session)),
            "revocation_epoch": cache.get(key("epoch", f"{user.pk}:{grant_subject(company.pk if company else None)}")) or ""}
        # A disconnect marker must outlive every grant issued after it.
        cache.touch(key("epoch", f"{user.pk}:{grant_subject(company.pk if company else None)}"), None)
        from contextlib import nullcontext
        from . import native_agents
        code = secrets.token_urlsafe(48)
        try:
            # Redis and PostgreSQL are separate stores. Hold the existing owner
            # fence until the binding and cache records are durable; any missing
            # reciprocal linkage makes queued native work fail closed.
            with native_agents.consent_fence(user) if native else nullcontext():
                if native:
                    native_agents.bind_native_grant(user, session, value, native_installation_id)
                remaining = max(1, int(value["expiresAt"] - time.time()))
                cache.set(key("grant", grant_id), value, timeout=remaining)
                with lock("index", f"{user.pk}:{grant_subject(company.pk if company else None)}", wait=REFRESH_LOCK_WAIT):
                    index_key = key("index", f"{user.pk}:{grant_subject(company.pk if company else None)}")
                    earlier = cache.get(index_key) or []
                    live = cache.get_many([key("grant", item) for item in earlier])
                    cache.set(index_key, [*(item for item in earlier if key("grant", item) in live), grant_id], timeout=INDEX_TTL)
                cache.set(key("code", code), {"grant_id": grant_id, "client_id": intent["client_id"],
                    "redirect_uri": intent["redirect_uri"], "code_challenge": intent["code_challenge"],
                    "resource": intent["resource"]}, timeout=CODE_TTL)
        except Exception:
            cache.delete(key("code", code))
            cache.delete(key("grant", grant_id))
            raise
        cache.delete(key("intent", request_id))
        return callback_url(intent, code=code)


@dataclass(frozen=True)
class Principal:
    """A validated account with a startup-scoped MCP grant."""
    user: object
    grant: dict


def valid_grant(grant_id, *, scope=None):
    grant = cache.get(key("grant", grant_id))
    # The tombstone is separate so a refresh that slides the grant's expiry at
    # the same moment as a revocation can never write the grant back as live.
    if not grant or grant.get("revoked") or cache.get(key("revoked", grant_id)) or grant.get("expiresAt", 0) <= time.time() or not resource_scopes(grant.get("resource")) or set(grant.get("scopes", [])) - resource_scopes(grant.get("resource")):
        raise AuthenticationFailed("Agent authorisation expired or was revoked.")
    if grant["resource"] == mlai_url() and not availability(grant["resource"])[0]:
        raise AuthenticationFailed("MLAI agent connections are not enabled yet.")
    current_epoch = cache.get(key("epoch", f"{grant['user_id']}:{grant_subject(grant.get('company_id'))}")) or ""
    if grant.get("revocation_epoch", "") != current_epoch:
        raise AuthenticationFailed("This startup agent connection was disconnected.")
    user = get_user_model().objects.filter(pk=grant["user_id"], is_active=True).first()
    if user is None or user.auth_version != grant["auth_version"]:
        raise AuthenticationFailed("Account authorisation was revoked.")
    if session_bound(grant["scopes"]) or grant.get("binding") != "account":
        # Grants made before account binding keep their session check, so one
        # whose session was signed out or whose device was revoked stays ended.
        session = CommunityChatAccountSession.objects.select_related("user").filter(pk=grant["session_id"], user=user).first()
        if not _valid_session(session, timezone.now()):
            raise AuthenticationFailed("Account session was revoked.")
        assert_device_owner(session)
    else:
        assert_installation_live(user, grant)
    # Pulse and Articles are reviewed and published in MLAI Chat, whose startup
    # APIs all require an approved membership, so startup-only grants need it too.
    try:
        require_community_access(user)
    except PermissionDenied as exc:
        raise AccessProblem(MEMBERSHIP_REASON, "community_membership_required") from exc
    if grant.get("company_id"):
        company_for(user, grant["company_id"], grant)
    elif any(scope.startswith(("startup:", "article:")) for scope in grant["scopes"]):
        raise AuthenticationFailed("Startup authorisation is unavailable.")
    if scope and scope not in grant.get("scopes", []):
        raise PermissionDenied("This agent does not have the required permission.")
    return Principal(user=user, grant=grant)


def issue_tokens(grant_id, principal=None):
    principal = principal or valid_grant(grant_id)
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
    # RFC 8707 lets the token request omit resource: the code or refresh token
    # is already bound to one audience. A value that names another is refused.
    resource = normal_resource(data.get("resource"))
    if resource is not None and not resource_scopes(resource):
        raise OAuthError("invalid_target")
    if data.get("grant_type") == "authorization_code":
        code = str(data.get("code") or "")
        with lock("code", code):
            record = cache.get(key("code", code))
            if not record or record["client_id"] != client_id or record["redirect_uri"] != data.get("redirect_uri") or record["resource"] != (resource or record["resource"]):
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
        with lock("refresh", refresh, wait=REFRESH_LOCK_WAIT):
            record = cache.get(key("refresh", refresh))
            if not record or record["client_id"] != client_id:
                raise OAuthError("invalid_grant")
            successor = _replayed_successor(refresh) if record.get("spent") else None
            if record.get("spent") and successor is None:
                # Outside the short retry window, a correct-client replay signals
                # a compromised token family. Keep only the hashed-token linkage
                # until grant expiry so all rotated descendants are revoked
                # through their shared grant.
                revoke_grant(record["grant_id"])
                raise OAuthError("invalid_grant")
            principal = valid_grant(record["grant_id"])
            if resource is not None and principal.grant["resource"] != resource:
                raise OAuthError("invalid_target")
            if successor is not None:
                # A lost response or a parallel refresh: hand back the tokens
                # this rotation already issued instead of ending the connection.
                return successor
            # The lock is short so a dead worker cannot strand the connection;
            # this claim makes sure only one refresh ever rotates this token even
            # if a slow one outlives that lock.
            claim = key("rotating", refresh)
            if not cache.add(claim, True, timeout=max(1, int(principal.grant["expiresAt"] - time.time()))):
                raise OAuthError("temporarily_unavailable", "This request is already being processed.")
            try:
                # A refresh never alters the authorised scopes. Hosts often resend
                # what they first asked for, so anything not granted is ignored.
                principal = Principal(user=principal.user, grant=_slide_grant(principal.grant))
                tokens = issue_tokens(record["grant_id"], principal)
                remaining = max(1, int(principal.grant["expiresAt"] - time.time()))
                cache.set(key("refresh", refresh), {**record, "spent": True}, timeout=remaining)
            except Exception:
                cache.delete(claim)
                raise
            if replay_grace():
                cache.set(key("replay", refresh), _replay_cipher(refresh).encrypt(json.dumps(tokens).encode()).decode(), timeout=replay_grace())
            return tokens
    raise OAuthError("unsupported_grant_type")


def _replay_cipher(refresh):
    """Seal a successor so only the presenter of the spent token can read it.

    The cache holds token hashes only; the key here derives from the raw spent
    refresh token, which is never stored.
    """
    from cryptography.fernet import Fernet
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(("valley-mcp-replay:" + refresh).encode()).digest()))


def _replayed_successor(refresh):
    sealed = cache.get(key("replay", refresh)) if replay_grace() else None
    if not sealed:
        return None
    try:
        return json.loads(_replay_cipher(refresh).decrypt(sealed.encode(), ttl=replay_grace()))
    except Exception:
        return None


def _slide_grant(grant):
    """Restart the idle period of a used startup grant; return the saved grant.

    Personal-agent consents keep their fixed lifetime: only the owner's signed
    renewal extends those (extend_native_grant).
    """
    if set(grant["scopes"]) & (DOT_SCOPES | {DOT_MESSAGES_SCOPE}) or grant.get("native_agent_grant_id"):
        return grant
    changes = {}
    if grant.get("binding") != "account" and not session_bound(grant["scopes"]):
        # A grant made before account binding just passed its session check:
        # from now on it follows the account and that session's device.
        try:
            session_id = UUID(str(grant.get("session_id")))
        except ValueError:
            session_id = None
        session = CommunityChatAccountSession.objects.filter(pk=session_id).first() if session_id else None
        if session is not None:
            changes.update(account_binding(session))
    expires = int(time.time()) + grant_idle_ttl()
    if expires > grant["expiresAt"]:
        changes["expiresAt"] = expires
    if not changes:
        return grant
    grant = {**grant, **changes}
    cache.set(key("grant", grant["id"]), grant, timeout=max(1, int(grant["expiresAt"] - time.time())))
    subject = f"{grant['user_id']}:{grant_subject(grant.get('company_id'))}"
    # Grants made before this change referenced a marker and an index that
    # expired after 30 days. Keep both for as long as the grant can live.
    cache.touch(key("epoch", subject), None)
    if grant["id"] in (cache.get(key("index", subject)) or []):
        # The common case only needs the longer lifetime, which needs no lock,
        # so a refresh never makes a new approval for this startup wait.
        cache.touch(key("index", subject), INDEX_TTL)
        return grant
    try:
        with lock("index", subject):
            listed = cache.get(key("index", subject)) or []
            cache.set(key("index", subject), listed if grant["id"] in listed else [*listed, grant["id"]], timeout=INDEX_TTL)
    except OAuthError:
        pass  # Listing is best effort; the next refresh repairs it.
    return grant


def authenticate_token(raw_token, *, scope=None, resource=None):
    if not str(raw_token).startswith("valley_access_"):
        raise AuthenticationFailed(f"Connect this agent to {'MLAI' if resource and resource == mlai_url() else 'Valley'} first.")
    record = cache.get(key("access", raw_token))
    if not record:
        raise AuthenticationFailed("Agent access token expired.")
    principal = valid_grant(record["grant_id"], scope=scope)
    if resource and principal.grant["resource"] != resource:
        raise AuthenticationFailed("This token was issued for a different MCP server.")
    if principal.grant["resource"] == mlai_url() and DOT_SCOPES <= set(principal.grant["scopes"]):
        from .native_agents import on_authenticated
        from agent_runtime.policy import AgentPermissionDenied
        try:
            on_authenticated(principal)
        except (APIException, AgentPermissionDenied):
            # Native consent can be paused/revoked independently. Other
            # explicitly granted MLAI capabilities remain available.
            pass
        # Native activation may wait on its durable owner fence. Account/session
        # revocation during that wait still invalidates the entire unified token.
        principal = valid_grant(principal.grant["id"], scope=scope)
    return principal


def extend_native_grant(grant_id, *, native_grant_id, user_id, until):
    """Slide a personal-agent consent forward after the owner's signed renewal.

    Only an unrevoked, unexpired grant already bound to that exact native grant
    and owner is extended, never past GRANT_TTL from now. Scopes, client and
    session are unchanged. Returns the new expiry timestamp, or None.
    """
    with lock("grant", grant_id):
        grant = cache.get(key("grant", grant_id))
        now = time.time()
        if (not grant or grant.get("revoked") or grant.get("expiresAt", 0) <= now
                or grant.get("native_agent_grant_id") != str(native_grant_id) or grant.get("user_id") != user_id):
            return None
        expires = int(min(float(until), now + GRANT_TTL))
        if expires <= grant["expiresAt"]:
            return grant["expiresAt"]
        grant["expiresAt"] = expires
        cache.set(key("grant", grant_id), grant, timeout=max(1, int(expires - now)))
        return expires


def revoke_grant(grant_id):
    grant = cache.get(key("grant", grant_id))
    if grant:
        try:
            if grant.get("native_agent_grant_id"):
                from .native_agents import revoke_native_grant
                revoke_native_grant(grant)
        finally:
            # Even an unavailable native database must not leave the unified
            # cached bearer/refresh family usable after this revocation attempt.
            grant["revoked"] = True
            cache.set(key("grant", grant_id), grant, timeout=max(1, int(grant["expiresAt"] - time.time())))
            # Outlives any expiry a concurrent refresh could have written.
            cache.set(key("revoked", grant_id), True, timeout=max(GRANT_TTL, grant_idle_ttl()) + 86400)


def disconnect_company(user, company_id):
    company_id = grant_subject(company_id)
    epoch = secrets.token_urlsafe(32)
    # Never expires: grants made after a disconnect reference this marker, and
    # a marker that lapsed first would end those later connections.
    cache.set(key("epoch", f"{user.pk}:{company_id}"), epoch, timeout=None)
    for grant_id in cache.get(key("index", f"{user.pk}:{company_id}")) or []:
        grant = cache.get(key("grant", grant_id))
        if grant and grant["user_id"] == user.pk and grant_subject(grant.get("company_id")) == str(company_id) and grant.get("revocation_epoch", "") != epoch:
            revoke_grant(grant_id)


def grants_for(user, company_id):
    company_id = grant_subject(company_id)
    rows = []
    for grant_id in cache.get(key("index", f"{user.pk}:{company_id}")) or []:
        grant = cache.get(key("grant", grant_id))
        if grant and not grant.get("revoked") and grant.get("expiresAt", 0) > time.time():
            try:
                valid_grant(grant_id)
            except (AuthenticationFailed, PermissionDenied):
                continue
            rows.append({field: grant[field] for field in ("id", "clientName", "redirectOrigin", "createdAt", "expiresAt", "resource", "scopes")})
    return rows


def metadata():
    base = public_base()
    return {"issuer": base, "authorization_endpoint": base + "/mcp/oauth/authorize", "token_endpoint": base + "/mcp/oauth/token",
        "registration_endpoint": base + "/mcp/oauth/register", "revocation_endpoint": base + "/mcp/oauth/revoke",
        "response_types_supported": ["code"], "grant_types_supported": ["authorization_code", "refresh_token"],
        "token_endpoint_auth_methods_supported": ["none"], "code_challenge_methods_supported": ["S256"],
        "scopes_supported": sorted(ALL_SCOPES if getattr(settings, "MLAI_MCP_ENABLED", False) else SCOPES), "authorization_response_iss_parameter_supported": True}
