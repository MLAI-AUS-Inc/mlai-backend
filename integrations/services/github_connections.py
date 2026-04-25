import secrets
import urllib.parse
from dataclasses import dataclass
from typing import Optional

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


def _cache_key(nonce: str) -> str:
    return f"github_oauth_state:{nonce}"


def _serialize_oauth_state(
    *,
    nonce: str,
    slack_user_id: Optional[str],
    domain: Optional[str],
    job_id: Optional[str],
    is_org_oauth: bool,
) -> str:
    return signing.dumps(
        {
            "nonce": nonce,
            "slack_user_id": slack_user_id,
            "domain": domain,
            "job_id": job_id,
            "type": "org" if is_org_oauth else "user",
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
) -> GitHubOAuthState:
    return GitHubOAuthState(
        raw=raw,
        nonce=nonce,
        slack_user_id=slack_user_id,
        domain=domain,
        job_id=job_id,
        is_org_oauth=is_org_oauth,
    )


def build_github_oauth_state(
    *,
    domain: Optional[str] = None,
    slack_user_id: str = "",
    job_id: Optional[str] = None,
) -> GitHubOAuthState:
    normalized_domain = normalize_domain(domain or "") or None
    normalized_slack_user_id = (slack_user_id or "").strip()
    nonce = secrets.token_urlsafe(16)
    is_org_oauth = bool(normalized_domain)
    normalized_job_id = None if is_org_oauth else (job_id or None)
    raw = _serialize_oauth_state(
        nonce=nonce,
        slack_user_id=normalized_slack_user_id or None,
        domain=normalized_domain,
        job_id=normalized_job_id,
        is_org_oauth=is_org_oauth,
    )
    return _build_state(
        raw=raw,
        nonce=nonce,
        slack_user_id=normalized_slack_user_id or None,
        domain=normalized_domain,
        job_id=normalized_job_id,
        is_org_oauth=is_org_oauth,
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


def build_github_oauth_url(domain: str, slack_user_id: str = "", request=None) -> str:
    state = build_github_oauth_state(domain=domain, slack_user_id=slack_user_id)
    store_github_oauth_state(state, request=request)
    return build_github_installation_url(state.raw)


def get_owned_org_configs(slack_user_id: str):
    return (
        OrganizationContentConfig.objects
        .select_related("organization")
        .filter(connected_slack_user_id=slack_user_id)
        .order_by("organization__domain")
    )


def get_owned_org_config(slack_user_id: str, domain: str) -> Optional[OrganizationContentConfig]:
    normalized_domain = normalize_domain(domain or "")
    if not slack_user_id or not normalized_domain:
        return None

    return (
        OrganizationContentConfig.objects
        .select_related("organization")
        .filter(
            connected_slack_user_id=slack_user_id,
            organization__domain=normalized_domain,
        )
        .first()
    )
