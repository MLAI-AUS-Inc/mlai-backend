import time
from dataclasses import dataclass, field
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


class GitHubWorkflowPermissionRequired(GitHubAppTokenError):
    """The App or installation has not approved editing workflow files."""


class GitHubCIEvidencePermissionRequired(GitHubAppTokenError):
    """Existing App or installation grants cannot read private CI evidence."""


class GitHubPermissionLookupUnavailable(GitHubAppTokenError):
    """Provider permission evidence is temporarily unavailable."""


WORKFLOW_PERMISSION_DETAIL = (
    "The GitHub App owner must review and enable Workflows: Read and write, "
    "then the repository owner must approve the installation permission update. "
    "No permissions have been changed automatically."
)
CI_EVIDENCE_PERMISSION_DETAIL = (
    "The GitHub App and repository installation must permit Checks and Commit statuses read access "
    "to verify private repository CI evidence. Review the selected GitHub connection."
)


@dataclass(frozen=True)
class GitHubInstallationToken:
    token: str
    expires_at: Optional[datetime]
    installation_id: str
    repository: str
    permissions: dict[str, str] = field(default_factory=dict)
    token_source: str = "github_app_installation"
    permission_profile: str = "repository"

    def as_content_factory_payload(self, *, domain: str = "") -> dict:
        payload = {
            "github_token": self.token,
            "github_repo": self.repository,
            "github_installation_id": self.installation_id,
            "installation_id": self.installation_id,
            "token_source": self.token_source,
            "source": self.token_source,
            "permission_profile": self.permission_profile,
        }
        if self.permissions:
            payload["github_permissions"] = dict(self.permissions)
            payload["permissions"] = dict(self.permissions)
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


def _cache_key(*, installation_id: str, repository: str, permission_mode: str,
               permission_profile: str = "repository") -> str:
    key = f"github_app_installation_token:{installation_id}:{repository.lower()}:{permission_mode}"
    return f"{key}:{permission_profile}" if permission_profile != "repository" else key


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


def _normalize_permissions(value) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    permissions: dict[str, str] = {}
    for key, permission in value.items():
        key_text = str(key or "").strip()
        permission_text = str(permission or "").strip().lower()
        if key_text and permission_text:
            permissions[key_text] = permission_text
    return permissions


def _temporary_permission_failure(response) -> bool:
    headers = getattr(response, "headers", {}) or {}
    return (response.status_code == 429 or response.status_code >= 500
            or (response.status_code == 403 and
                (headers.get("Retry-After") or headers.get("X-RateLimit-Remaining") == "0")))


def _profile_grant_permissions(payload, *, profile) -> dict[str, str]:
    if not isinstance(payload, dict) or not isinstance(payload.get("permissions"), dict):
        raise GitHubPermissionLookupUnavailable("GitHub permission evidence is temporarily unavailable.")
    permissions = _normalize_permissions(payload["permissions"])
    if payload.get("suspended_at"):
        raise GitHubAppTokenError("The selected GitHub installation is suspended.")
    repository_modes = {"write"} if profile == "workflow_files" else {"read", "write"}
    if any(permissions.get(key) not in repository_modes for key in ("contents", "pull_requests")):
        raise GitHubAppTokenError("The selected GitHub installation lacks the required repository permissions.")
    if profile == "workflow_files" and permissions.get("workflows") != "write":
        raise GitHubWorkflowPermissionRequired(WORKFLOW_PERMISSION_DETAIL)
    if profile == "ci_evidence" and any(permissions.get(key) not in {"read", "write"} for key in ("checks", "statuses")):
        raise GitHubCIEvidencePermissionRequired(CI_EVIDENCE_PERMISSION_DETAIL)
    return permissions


def _credential_permissions_match(permissions, *, mode, profile) -> bool:
    required = {"contents": mode, "pull_requests": mode}
    if profile == "workflow_files":
        required["workflows"] = "write"
    elif profile == "ci_evidence":
        required.update(checks="read", statuses="read")
    return (all(permissions.get(key) == value for key, value in required.items())
            and all(key in required or (key == "metadata" and value == "read")
                    for key, value in permissions.items()))


def require_installation_workflow_permissions(installation_id: str) -> dict[str, str]:
    """Read approved App and installation grants without minting credentials."""
    return _require_installation_profile_permissions(installation_id, profile="workflow_files")


def require_installation_repository_permissions(installation_id: str) -> dict[str, str]:
    """Read existing App and installation repository grants without minting writes."""
    return _require_installation_profile_permissions(installation_id, profile="repository")


def require_installation_ci_evidence_permissions(installation_id: str) -> dict[str, str]:
    """Read existing CI grants without changing permissions or minting tokens."""
    return _require_installation_profile_permissions(installation_id, profile="ci_evidence")


def _require_installation_profile_permissions(installation_id, *, profile):
    identifier = str(installation_id or "")
    if not identifier.isdigit():
        raise GitHubAppTokenError("The selected GitHub installation identity is invalid.")
    headers = {"Authorization": f"Bearer {_github_app_jwt()}",
               "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    installation_permissions = {}
    for kind, url in (("app", "https://api.github.com/app"),
                      ("installation", f"https://api.github.com/app/installations/{identifier}")):
        response = http_requests.get(url, headers=headers, timeout=(3, 20))
        if _temporary_permission_failure(response):
            raise GitHubPermissionLookupUnavailable("GitHub permission evidence is temporarily unavailable.")
        if response.status_code != 200:
            raise GitHubAppTokenError("The selected GitHub installation could not be verified.")
        try:
            payload = response.json()
        except (TypeError, ValueError) as exc:
            raise GitHubPermissionLookupUnavailable("GitHub permission evidence is temporarily unavailable.") from exc
        permissions = _profile_grant_permissions(payload, profile=profile)
        if kind == "installation":
            installation_permissions = permissions
    return installation_permissions


def create_installation_access_token(
    *,
    installation_id: str,
    repository: str,
    permission_mode: str = "write",
    use_cache: bool = True,
    repository_id: Optional[int] = None,
    permission_profile: str = "repository",
) -> GitHubInstallationToken:
    """Mint a repository token, preferring immutable identity when supplied."""
    normalized_installation_id = str(installation_id or "").strip()
    normalized_repository = str(repository or "").strip()
    if not normalized_installation_id:
        raise GitHubAppTokenError("GitHub installation id is missing.")
    if not normalized_repository or "/" not in normalized_repository:
        raise GitHubAppTokenError("GitHub repository must be owner/repo.")

    if not isinstance(permission_profile, str) or permission_profile not in {"repository", "workflow_files", "ci_evidence"}:
        raise GitHubAppTokenError("Unknown repository permission profile.")
    if permission_profile == "workflow_files" and (permission_mode != "write" or repository_id is None):
        raise GitHubAppTokenError("Workflow credentials require an immutable repository and explicit write mode.")
    if permission_profile == "ci_evidence" and (permission_mode != "read" or repository_id is None):
        raise GitHubAppTokenError("CI evidence credentials require an immutable repository and explicit read mode.")
    mode = "read" if str(permission_mode or "").strip().lower() == "read" else "write"
    key = _cache_key(installation_id=normalized_installation_id, repository=normalized_repository,
                     permission_mode=mode, permission_profile=permission_profile)
    if repository_id is not None:
        if isinstance(repository_id, bool) or not isinstance(repository_id, int) or repository_id < 1:
            raise GitHubAppTokenError("GitHub repository identity must be a positive integer.")
        key = f"{key}:repository-id:{repository_id}"
    if permission_profile == "workflow_files":
        require_installation_workflow_permissions(normalized_installation_id)
    elif permission_profile == "ci_evidence":
        require_installation_ci_evidence_permissions(normalized_installation_id)
    if use_cache:
        cached = cache.get(key)
        if isinstance(cached, dict) and cached.get("github_token"):
            permissions = _normalize_permissions(cached.get("github_permissions") or cached.get("permissions"))
            if not permissions:
                permissions = _normalize_permissions(cached.get("granted_permissions"))
            if (cached.get("permission_profile", "repository") != permission_profile
                    or not _credential_permissions_match(permissions, mode=mode, profile=permission_profile)
                    or cached.get("installation_id", normalized_installation_id) != normalized_installation_id
                    or str(cached.get("github_repo", normalized_repository)).casefold() != normalized_repository.casefold()
                    or cached.get("token_source", "github_app_installation") != "github_app_installation"):
                cached = None
        if isinstance(cached, dict) and cached.get("github_token"):
            expires_at = _parse_expires_at(cached.get("expires_at"))
            return GitHubInstallationToken(
                token=str(cached["github_token"]),
                expires_at=expires_at,
                installation_id=normalized_installation_id,
                repository=normalized_repository,
                permissions=permissions,
                permission_profile=permission_profile,
            )

    _owner, repo_name = normalized_repository.split("/", 1)
    body = {
        "repositories": [repo_name],
        "permissions": {
            "contents": "read" if mode == "read" else "write",
            "pull_requests": "read" if mode == "read" else "write",
        },
    }
    if repository_id is not None:
        body.pop("repositories")
        body["repository_ids"] = [repository_id]
    if permission_profile == "workflow_files":
        body["permissions"]["workflows"] = "write"
    elif permission_profile == "ci_evidence":
        body["permissions"].update(checks="read", statuses="read")
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
        if permission_profile in {"workflow_files", "ci_evidence"}:
            if _temporary_permission_failure(response):
                raise GitHubPermissionLookupUnavailable("GitHub permission evidence is temporarily unavailable.")
            # A 403 alone cannot prove a missing workflow grant. Re-read the
            # grants to distinguish a raced approval from repository access.
            if response.status_code == 403:
                _require_installation_profile_permissions(normalized_installation_id, profile=permission_profile)
            raise GitHubAppTokenError("The selected GitHub repository credential could not be issued.")
        try:
            error_payload = response.json()
        except (TypeError, ValueError):
            error_payload = {}
        github_message = str(error_payload.get("message") or "").strip()
        github_detail = f" GitHub message: {github_message}" if github_message else ""
        permission_hint = (
            " Ensure the MLAI Tools GitHub App is installed on this repository with "
            "Contents: Read/Write and Pull requests: Read/Write."
            if mode == "write"
            else ""
        )
        raise GitHubAppTokenError(
            f"Could not mint GitHub App installation token for {normalized_repository}: "
            f"GitHub returned {response.status_code}.{github_detail}{permission_hint}"
        )
    try:
        payload = response.json()
    except (TypeError, ValueError) as exc:
        raise GitHubAppTokenError("GitHub returned an invalid installation credential.") from exc
    if not isinstance(payload, dict):
        raise GitHubAppTokenError("GitHub returned an invalid installation credential.")
    token = str(payload.get("token") or "").strip()
    if not token:
        raise GitHubAppTokenError("GitHub App installation token response did not include a token.")
    expires_at = _parse_expires_at(payload.get("expires_at"))
    permissions = _normalize_permissions(payload.get("permissions"))
    if permission_profile in {"workflow_files", "ci_evidence"} and not _credential_permissions_match(permissions, mode=mode, profile=permission_profile):
        # A narrowed or raced provider grant must never escape as a usable token.
        try:
            http_requests.delete("https://api.github.com/installation/token",
                                 headers={"Authorization": f"Bearer {token}"}, timeout=(3, 10))
        except Exception:
            pass
        if permission_profile == "workflow_files" and permissions.get("workflows") != "write" and all(permissions.get(key) == "write" for key in ("contents", "pull_requests")):
            raise GitHubWorkflowPermissionRequired(WORKFLOW_PERMISSION_DETAIL)
        if (permission_profile == "ci_evidence" and all(permissions.get(key) == "read" for key in ("contents", "pull_requests"))
                and any(permissions.get(key) not in {"read", "write"} for key in ("checks", "statuses"))):
            raise GitHubCIEvidencePermissionRequired(CI_EVIDENCE_PERMISSION_DETAIL)
        raise GitHubAppTokenError("GitHub returned a credential outside the approved repository permission profile.")
    result = GitHubInstallationToken(
        token=token,
        expires_at=expires_at,
        installation_id=normalized_installation_id,
        repository=normalized_repository,
        permissions=permissions,
        permission_profile=permission_profile,
    )
    if use_cache:
        cache.set(key, result.as_content_factory_payload(), timeout=_token_ttl_seconds(expires_at))
    return result


def _request_installation_access_token(
    installation_id: str,
    *,
    permissions: Optional[dict] = None,
    repositories: Optional[list] = None,
) -> dict:
    """Mint an installation token via the App JWT and return the raw payload.

    Unlike ``create_installation_access_token`` (which restricts to a single repo
    for write ops), this accepts an optional ``repositories``/``permissions`` scope
    so callers can request an installation-wide, metadata-read token for listing.
    """
    normalized_installation_id = str(installation_id or "").strip()
    if not normalized_installation_id:
        raise GitHubAppTokenError("GitHub installation id is missing.")
    body: dict = {}
    if repositories:
        body["repositories"] = list(repositories)
    if permissions:
        body["permissions"] = dict(permissions)
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
            f"Could not mint GitHub App installation token for installation "
            f"{normalized_installation_id}: GitHub returned {response.status_code}."
        )
    payload = response.json()
    if not str(payload.get("token") or "").strip():
        raise GitHubAppTokenError("GitHub App installation token response did not include a token.")
    return payload


def list_installation_repositories_via_app(installation_id: str) -> list[dict]:
    """Every repo an installation can access, using only the App key (no user token).

    Mints a metadata-read installation token spanning all repos in the
    installation, then pages ``/installation/repositories``. Returns raw GitHub
    repo dicts (caller normalizes). Durable: unaffected by an expired user token.
    """
    normalized_installation_id = str(installation_id or "").strip()
    if not normalized_installation_id:
        return []
    payload = _request_installation_access_token(
        normalized_installation_id, permissions={"metadata": "read"}
    )
    token = str(payload.get("token") or "").strip()
    repos: list[dict] = []
    page = 1
    while page <= 20:  # hard cap 20*100 = 2000 repos
        response = http_requests.get(
            f"https://api.github.com/installation/repositories?per_page=100&page={page}",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=(3, 20),
        )
        if response.status_code != 200:
            break
        data = response.json() if response.content else {}
        batch = data.get("repositories", []) if isinstance(data, dict) else []
        repos.extend(repo for repo in batch if isinstance(repo, dict))
        if len(batch) < 100:
            break
        page += 1
    return repos


# Liveness classifications for a GitHub App installation. ``DEAD`` is only ever
# returned on a definitive GitHub not-found/gone response; every ambiguous
# signal (suspension, 5xx, network trouble, unconfigured credentials) is
# ``UNKNOWN`` so callers never prune on an inconclusive probe.
INSTALLATION_LIVE = "live"
INSTALLATION_DEAD = "dead"
INSTALLATION_UNKNOWN = "unknown"


def probe_installation_liveness(installation_id: str) -> str:
    """Classify a GitHub App installation as live / dead / unknown.

    Attempts to mint a metadata-read installation token — the minimal
    capability the founder registry needs from an installation, and the same
    call ``list_installation_repositories_via_app`` relies on. GitHub answers
    ``404`` (uninstalled) or ``410`` (gone) for an installation that no longer
    exists, which is the authoritative "this row is stale" signal.

    Returns ``INSTALLATION_LIVE`` on ``200``/``201``, ``INSTALLATION_DEAD`` on
    ``404``/``410`` (the founder uninstalled the App), and
    ``INSTALLATION_UNKNOWN`` for anything else — a suspended installation
    (``403``), a transient ``5xx``, a network failure, or missing App
    credentials. Callers MUST treat ``UNKNOWN`` as "cannot prove stale" and
    leave the row alone.
    """
    normalized_installation_id = str(installation_id or "").strip()
    if not normalized_installation_id:
        return INSTALLATION_UNKNOWN
    if not github_app_credentials_configured():
        return INSTALLATION_UNKNOWN
    try:
        jwt_token = _github_app_jwt()
    except GitHubAppTokenError:
        return INSTALLATION_UNKNOWN

    try:
        response = http_requests.post(
            f"https://api.github.com/app/installations/{normalized_installation_id}/access_tokens",
            headers={
                "Authorization": f"Bearer {jwt_token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            json={"permissions": {"metadata": "read"}},
            timeout=(3, 20),
        )
    except Exception:  # noqa: BLE001 - any transport failure is inconclusive
        return INSTALLATION_UNKNOWN

    if response.status_code in (200, 201):
        # This liveness-only credential must not remain active after the probe.
        try:
            credential = response.json().get("token")
            if isinstance(credential, str) and credential:
                http_requests.delete("https://api.github.com/installation/token",
                    headers={"Authorization": f"Bearer {credential}"}, timeout=(3, 8))
        except Exception:
            pass
        return INSTALLATION_LIVE
    if response.status_code in (404, 410):
        return INSTALLATION_DEAD
    return INSTALLATION_UNKNOWN


def list_app_installation_ids() -> Optional[set]:
    """The set of installation ids the *configured* GitHub App currently owns.

    Uses the App JWT to page ``GET /app/installations``. This is the anti-
    footgun for the pruning sweep: a ``404`` on a single installation is
    ambiguous between "the founder uninstalled" and "these credentials
    authenticate as a *different* App than minted this row" (e.g. a staging
    scheduler pointed at the prod DB, or an App migration). Cross-referencing the
    rows we are about to prune against the ids this App actually owns tells the
    two apart.

    Returns a set of string ids on success (possibly empty), or ``None`` when the
    App is unconfigured or the listing could not be retrieved — callers MUST
    treat ``None`` as "cannot confirm ownership", never as "owns nothing".
    """
    if not github_app_credentials_configured():
        return None
    try:
        jwt_token = _github_app_jwt()
    except GitHubAppTokenError:
        return None

    ids: set = set()
    page = 1
    while page <= 20:  # hard cap 20*100 = 2000 installations
        try:
            response = http_requests.get(
                f"https://api.github.com/app/installations?per_page=100&page={page}",
                headers={
                    "Authorization": f"Bearer {jwt_token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
                timeout=(3, 20),
            )
        except Exception:  # noqa: BLE001 - any transport failure is inconclusive
            return None
        if response.status_code != 200:
            return None
        data = response.json() if response.content else []
        batch = data if isinstance(data, list) else []
        for item in batch:
            if isinstance(item, dict) and item.get("id") is not None:
                ids.add(str(item.get("id")))
        if len(batch) < 100:
            break
        page += 1
    return ids
