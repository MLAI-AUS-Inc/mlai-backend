import secrets
import urllib.parse
from dataclasses import dataclass
from typing import Optional

from django.conf import settings
from django.core import signing
from django.core.cache import cache

from content_factory.models import OrganizationContentConfig
from integrations.utils import normalize_domain

GITHUB_APP_SLUG = "mlai-tools"
GITHUB_OAUTH_STATE_TIMEOUT_SECONDS = 600
GITHUB_OAUTH_STATE_SALT = "integrations.github.oauth.state"


@dataclass(frozen=True)
class GitHubOAuthState:
    raw: str
    nonce: str
    slack_user_id: Optional[str]
    domain: Optional[str]
    job_id: Optional[str]
    is_org_oauth: bool
    return_url: Optional[str] = None


def _cache_key(nonce: str) -> str:
    return f"github_oauth_state:{nonce}"


def _serialize_oauth_state(
    *,
    nonce: str,
    slack_user_id: Optional[str],
    domain: Optional[str],
    job_id: Optional[str],
    is_org_oauth: bool,
    return_url: Optional[str] = None,
) -> str:
    return signing.dumps(
        {
            "nonce": nonce,
            "slack_user_id": slack_user_id,
            "domain": domain,
            "job_id": job_id,
            "type": "org" if is_org_oauth else "user",
            "return_url": return_url,
        },
        salt=GITHUB_OAUTH_STATE_SALT,
        compress=True,
    )


def _build_state(
    *,
    raw: str,
    nonce: str,
    slack_user_id: Optional[str],
    domain: Optional[str],
    job_id: Optional[str],
    is_org_oauth: bool,
    return_url: Optional[str] = None,
) -> GitHubOAuthState:
    return GitHubOAuthState(
        raw=raw,
        nonce=nonce,
        slack_user_id=slack_user_id,
        domain=domain,
        job_id=job_id,
        is_org_oauth=is_org_oauth,
        return_url=return_url,
    )


def build_github_oauth_state(
    *,
    domain: Optional[str] = None,
    slack_user_id: str = "",
    job_id: Optional[str] = None,
    return_url: Optional[str] = None,
) -> GitHubOAuthState:
    normalized_domain = normalize_domain(domain or "") or None
    normalized_slack_user_id = (slack_user_id or "").strip()
    normalized_return_url = (return_url or "").strip() or None
    nonce = secrets.token_urlsafe(16)
    is_org_oauth = bool(normalized_domain)
    normalized_job_id = None if is_org_oauth else (job_id or None)
    raw = _serialize_oauth_state(
        nonce=nonce,
        slack_user_id=normalized_slack_user_id or None,
        domain=normalized_domain,
        job_id=normalized_job_id,
        is_org_oauth=is_org_oauth,
        return_url=normalized_return_url,
    )
    return _build_state(
        raw=raw,
        nonce=nonce,
        slack_user_id=normalized_slack_user_id or None,
        domain=normalized_domain,
        job_id=normalized_job_id,
        is_org_oauth=is_org_oauth,
        return_url=normalized_return_url,
    )

def _parse_signed_github_oauth_state(raw_state: str) -> GitHubOAuthState:
    payload = signing.loads(
        raw_state,
        salt=GITHUB_OAUTH_STATE_SALT,
        max_age=GITHUB_OAUTH_STATE_TIMEOUT_SECONDS,
    )
    if not isinstance(payload, dict):
        raise ValueError("Invalid signed state payload")

    nonce = str(payload.get("nonce") or "").strip()
    if not nonce:
        raise ValueError("Missing nonce")

    state_type = payload.get("type")
    normalized_domain = normalize_domain(payload.get("domain") or "") or None
    slack_user_id = (payload.get("slack_user_id") or "").strip() or None
    job_id = (payload.get("job_id") or "").strip() or None
    return_url = (payload.get("return_url") or "").strip() or None
    is_org_oauth = state_type == "org"
    if is_org_oauth:
        job_id = None
    elif state_type != "user":
        raise ValueError("Invalid state type")

    return _build_state(
        raw=raw_state,
        nonce=nonce,
        slack_user_id=slack_user_id,
        domain=normalized_domain,
        job_id=job_id,
        is_org_oauth=is_org_oauth,
        return_url=return_url,
    )


def _parse_legacy_github_oauth_state(raw_state: str) -> GitHubOAuthState:
    parts = raw_state.split("::")
    if len(parts) >= 4 and parts[3] == "org":
        normalized_domain = normalize_domain(parts[0] or "")
        return _build_state(
            raw=raw_state,
            nonce=parts[1],
            slack_user_id=parts[2] or None,
            domain=normalized_domain or None,
            job_id=None,
            is_org_oauth=True,
        )

    if len(parts) < 2:
        raise ValueError("Invalid state format")

    job_id = None
    if len(parts) >= 3 and parts[2] not in {"", "None"}:
        job_id = parts[2]

    return _build_state(
        raw=raw_state,
        nonce=parts[1],
        slack_user_id=parts[0] or None,
        domain=None,
        job_id=job_id,
        is_org_oauth=False,
    )


def parse_github_oauth_state(raw_state: str) -> GitHubOAuthState:
    if not raw_state:
        raise ValueError("Missing state")

    try:
        return _parse_signed_github_oauth_state(raw_state)
    except signing.SignatureExpired as exc:
        raise ValueError("Expired state") from exc
    except (signing.BadSignature, ValueError, TypeError):
        return _parse_legacy_github_oauth_state(raw_state)


def store_github_oauth_state(state: GitHubOAuthState, request=None) -> None:
    if request is not None:
        request.session["github_oauth_state"] = state.raw


def validate_github_oauth_state(raw_state: str, request=None) -> GitHubOAuthState:
    try:
        parsed_state = _parse_signed_github_oauth_state(raw_state)
    except signing.SignatureExpired as exc:
        raise ValueError("Invalid or expired state") from exc
    except (signing.BadSignature, ValueError, TypeError):
        parsed_state = _parse_legacy_github_oauth_state(raw_state)
    else:
        if request is not None and request.session.get("github_oauth_state") == raw_state:
            request.session.pop("github_oauth_state", None)
        return parsed_state

    cached_state = cache.get(_cache_key(parsed_state.nonce))
    session_state = None
    if request is not None:
        session_state = request.session.get("github_oauth_state")

    is_valid = False
    if cached_state and secrets.compare_digest(cached_state, raw_state):
        is_valid = True
    elif session_state and secrets.compare_digest(session_state, raw_state):
        is_valid = True

    if not is_valid:
        raise ValueError("Invalid or expired state")

    cache.delete(_cache_key(parsed_state.nonce))
    if request is not None and request.session.get("github_oauth_state") == raw_state:
        request.session.pop("github_oauth_state", None)

    return parsed_state


def build_github_installation_url(raw_state: str) -> str:
    install_url = f"https://github.com/apps/{GITHUB_APP_SLUG}/installations/new"
    return install_url + "?" + urllib.parse.urlencode({"state": raw_state})


def build_github_oauth_url(
    domain: str,
    slack_user_id: str = "",
    request=None,
    return_url: Optional[str] = None,
) -> str:
    state = build_github_oauth_state(
        domain=domain,
        slack_user_id=slack_user_id,
        return_url=return_url,
    )
    store_github_oauth_state(state, request=request)
    return build_github_installation_url(state.raw)


def _origin_of(url: str) -> Optional[str]:
    """Return the ``scheme://host[:port]`` origin of ``url`` if it is http(s)."""
    try:
        parts = urllib.parse.urlsplit((url or "").strip())
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None
    return f"{parts.scheme}://{parts.netloc}"


def _allowed_frontend_origins() -> set:
    """Origins we are willing to redirect a browser back to after install.

    Sourced from the same allowlist the API already trusts (CORS) plus the
    configured frontend URLs, so adding a new frontend host only needs the
    existing env vars — there is no second list to keep in sync.
    """
    origins = set()
    for origin in getattr(settings, "CORS_ALLOWED_ORIGINS", None) or []:
        normalized = _origin_of(origin)
        if normalized:
            origins.add(normalized)
    for candidate in (
        getattr(settings, "CONTENT_FACTORY_FRONTEND_URL", ""),
        getattr(settings, "DEFAULT_FRONTEND_URL", ""),
    ):
        normalized = _origin_of(candidate)
        if normalized:
            origins.add(normalized)
    return origins


def is_allowed_return_url(url: Optional[str]) -> bool:
    """True only for absolute http(s) URLs whose origin we explicitly trust.

    Guards against open-redirect abuse: the ``return_url`` round-trips through
    GitHub as part of ``state``, so it must be re-validated on the way back in.
    """
    origin = _origin_of(url or "")
    if not origin:
        return False
    return origin in _allowed_frontend_origins()


def default_frontend_url() -> str:
    return (
        getattr(settings, "CONTENT_FACTORY_FRONTEND_URL", "")
        or getattr(settings, "DEFAULT_FRONTEND_URL", "")
        or "https://mlai.au"
    )


def build_post_install_redirect_url(return_url: Optional[str], **params) -> str:
    """Resolve where to send the browser after a GitHub install completes.

    Uses ``return_url`` when it points at a trusted frontend origin (so the user
    lands exactly where they left off), otherwise falls back to the configured
    app home. Status fields (e.g. ``github=connected``) are merged into the
    query string without clobbering any params the caller already included.
    """
    base = return_url if is_allowed_return_url(return_url) else default_frontend_url()
    parts = urllib.parse.urlsplit(base)
    query = dict(urllib.parse.parse_qsl(parts.query, keep_blank_values=True))
    for key, value in params.items():
        if value is None:
            continue
        text = str(value).strip()
        if not text:
            continue
        query[key] = text
    new_query = urllib.parse.urlencode(query)
    return urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, parts.path, new_query, parts.fragment)
    )


def _actor_id_filter_values(slack_user_id) -> list[str]:
    if isinstance(slack_user_id, (list, tuple, set)):
        return [str(value).strip() for value in slack_user_id if str(value).strip()]
    value = str(slack_user_id or "").strip()
    return [value] if value else []


def get_owned_org_configs(slack_user_id: str):
    actor_ids = _actor_id_filter_values(slack_user_id)
    return (
        OrganizationContentConfig.objects
        .select_related("organization")
        .filter(connected_slack_user_id__in=actor_ids)
        .order_by("organization__domain")
    )


def get_owned_org_config(slack_user_id: str, domain: str) -> Optional[OrganizationContentConfig]:
    normalized_domain = normalize_domain(domain or "")
    actor_ids = _actor_id_filter_values(slack_user_id)
    if not actor_ids or not normalized_domain:
        return None

    return (
        OrganizationContentConfig.objects
        .select_related("organization")
        .filter(
            connected_slack_user_id__in=actor_ids,
            organization__domain=normalized_domain,
        )
        .first()
    )
