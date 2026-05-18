import time
from dataclasses import dataclass
from datetime import datetime, timezone as datetime_timezone
from typing import Optional

import jwt
from django.conf import settings
from django.core.cache import cache
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from integrations import http_client as http_requests


class GitHubAppTokenError(Exception):
    """Raised when an installation token cannot be minted."""


@dataclass(frozen=True)
class GitHubInstallationToken:
    token: str
    expires_at: Optional[datetime]
    installation_id: str
    repository: str
    token_source: str = "github_app_installation"

    def as_content_factory_payload(self, *, domain: str = "") -> dict:
        payload = {
            "github_token": self.token,
            "github_repo": self.repository,
            "github_installation_id": self.installation_id,
            "installation_id": self.installation_id,
            "token_source": self.token_source,
            "source": self.token_source,
        }
        if domain:
            payload["domain"] = domain
        if self.expires_at:
            payload["expires_at"] = self.expires_at.isoformat()
        return payload


def _github_app_private_key() -> str:
    raw = str(getattr(settings, "GITHUB_APP_PRIVATE_KEY", "") or "").strip()
    if not raw:
        return ""
    return raw.replace("\\n", "\n")


def github_app_credentials_configured() -> bool:
    return bool(str(getattr(settings, "GITHUB_APP_ID", "") or "").strip() and _github_app_private_key())


def _github_app_jwt() -> str:
    app_id = str(getattr(settings, "GITHUB_APP_ID", "") or "").strip()
    private_key = _github_app_private_key()
    if not app_id or not private_key:
        raise GitHubAppTokenError("GitHub App credentials are not configured.")

    now = int(time.time())
    payload = {
        "iat": now - 60,
        "exp": now + 540,
        "iss": app_id,
    }
    encoded = jwt.encode(payload, private_key, algorithm="RS256")
    return encoded.decode("utf-8") if isinstance(encoded, bytes) else str(encoded)


def _cache_key(*, installation_id: str, repository: str, permission_mode: str) -> str:
    return f"github_app_installation_token:{installation_id}:{repository.lower()}:{permission_mode}"


def _parse_expires_at(value) -> Optional[datetime]:
    parsed = parse_datetime(str(value or ""))
    if parsed is None:
        return None
    if timezone.is_naive(parsed):
        return timezone.make_aware(parsed, timezone=datetime_timezone.utc)
    return parsed


def _token_ttl_seconds(expires_at: Optional[datetime]) -> int:
    if expires_at is None:
        return 300
    seconds = int((expires_at - timezone.now()).total_seconds()) - 300
    return max(60, seconds)


def create_installation_access_token(
    *,
    installation_id: str,
    repository: str,
    permission_mode: str = "write",
    use_cache: bool = True,
) -> GitHubInstallationToken:
    normalized_installation_id = str(installation_id or "").strip()
    normalized_repository = str(repository or "").strip()
    if not normalized_installation_id:
        raise GitHubAppTokenError("GitHub installation id is missing.")
    if not normalized_repository or "/" not in normalized_repository:
        raise GitHubAppTokenError("GitHub repository must be owner/repo.")

    mode = "read" if str(permission_mode or "").strip().lower() == "read" else "write"
    key = _cache_key(installation_id=normalized_installation_id, repository=normalized_repository, permission_mode=mode)
    if use_cache:
        cached = cache.get(key)
        if isinstance(cached, dict) and cached.get("github_token"):
            expires_at = _parse_expires_at(cached.get("expires_at"))
            return GitHubInstallationToken(
                token=str(cached["github_token"]),
                expires_at=expires_at,
                installation_id=normalized_installation_id,
                repository=normalized_repository,
            )

    _owner, repo_name = normalized_repository.split("/", 1)
    body = {
        "repositories": [repo_name],
        "permissions": {
            "contents": "read" if mode == "read" else "write",
            "pull_requests": "read" if mode == "read" else "write",
        },
    }
    response = http_requests.post(
        f"https://api.github.com/app/installations/{normalized_installation_id}/access_tokens",
        headers={
            "Authorization": f"Bearer {_github_app_jwt()}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        json=body,
        timeout=(3, 20),
    )
    if response.status_code not in {200, 201}:
        raise GitHubAppTokenError(
            f"Could not mint GitHub App installation token for {normalized_repository}: "
            f"GitHub returned {response.status_code}."
        )
    payload = response.json()
    token = str(payload.get("token") or "").strip()
    if not token:
        raise GitHubAppTokenError("GitHub App installation token response did not include a token.")
    expires_at = _parse_expires_at(payload.get("expires_at"))
    result = GitHubInstallationToken(
        token=token,
        expires_at=expires_at,
        installation_id=normalized_installation_id,
        repository=normalized_repository,
    )
    if use_cache:
        cache.set(key, result.as_content_factory_payload(), timeout=_token_ttl_seconds(expires_at))
    return result
