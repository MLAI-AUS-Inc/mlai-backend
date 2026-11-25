"""The founder's existing GitHub connection, lent to the security scanner.

Security and Articles use the same GitHub App (MLAI Tools). A founder who
connected GitHub for Articles or Pulse already has ``GitHubInstallation`` rows,
so the scanner can list their repositories and read one of them without asking
the founder to connect GitHub a second time.

Only the scanner's service key reaches this, and only for a live MLAI sign-in
grant. Tokens are read-only and scoped to a single repository the founder's
installations can see; they expire within an hour.
"""
import re

import requests
from django.contrib.auth import get_user_model
from django.core.cache import cache

from integrations.models import GitHubInstallation
from integrations.services import github_app
from integrations.services.github_installations import list_user_repos

from .cybertest_entry import EntryError

MAX_REPOSITORIES = 2000
LOGIN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
FULL_NAME = re.compile(r"^[A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100}$")


def founder(identity):
    user = get_user_model().objects.filter(pk=identity["user_id"]).first()
    if user is None:
        raise EntryError("ACCESS_REVOKED", "Your MLAI account is unavailable. Sign in again.", 401)
    return user


def _installation(user):
    """The founder's most recent installation that names who authorized it."""
    return (
        GitHubInstallation.objects.filter(user=user)
        .exclude(github_user_name="")
        .order_by("-updated_at", "-pk")
        .first()
    )


def _github_user_id(installation):
    """GitHub's permanent id for the login that authorized ``installation``.

    The scanner keys saved checks by this id, so a founder sees the same
    history whichever way they connected GitHub.
    """
    login = (installation.github_user_name or "").strip()
    if not LOGIN.match(login):
        return None
    key = f"cybertest_github_user_id:{login.lower()}"
    cached = cache.get(key)
    if cached:
        return cached
    try:
        payload = github_app._request_installation_access_token(
            str(installation.installation_id), permissions={"metadata": "read"}
        )
        response = requests.get(
            f"https://api.github.com/users/{login}",
            headers={
                "Authorization": f"Bearer {payload.get('token') or ''}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=(3, 10),
            allow_redirects=False,
        )
    except (github_app.GitHubAppTokenError, requests.RequestException):
        return None
    if response.status_code != 200:
        return None
    user_id = (response.json() or {}).get("id")
    if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id < 1:
        return None
    cache.set(key, str(user_id), timeout=24 * 60 * 60)
    return str(user_id)


def _repository(repo):
    repository_id = repo.get("id")
    full_name = str(repo.get("fullName") or repo.get("full_name") or "").strip()
    if isinstance(repository_id, bool) or not isinstance(repository_id, int) or repository_id < 1:
        return None
    if not FULL_NAME.match(full_name):
        return None
    return {
        "repository_id": str(repository_id),
        "full_name": full_name,
        "private": bool(repo.get("private")),
        "installation_id": str(repo.get("installationId") or repo.get("installation_id") or ""),
    }


def connection(identity):
    """Who the founder is on GitHub and which repositories they can choose.

    ``connected`` is false when the founder has never connected GitHub (or the
    login is unknown), and the scanner then offers its own Connect GitHub.
    """
    user = founder(identity)
    installation = _installation(user)
    user_id = _github_user_id(installation) if installation else None
    if installation is None or user_id is None:
        return {"connected": False}
    repositories = []
    for repo in list_user_repos(user):
        item = _repository(repo)
        if item:
            repositories.append({key: item[key] for key in ("repository_id", "full_name", "private")})
        if len(repositories) >= MAX_REPOSITORIES:
            break
    repositories.sort(key=lambda item: item["full_name"].lower())
    return {
        "connected": True,
        "login": installation.github_user_name.strip(),
        "github_user_id": user_id,
        "repositories": repositories,
    }


def read_token(identity, repository_id, full_name):
    """A read-only token for one of the founder's repositories."""
    if not isinstance(repository_id, str) or not repository_id.isdigit() or not isinstance(full_name, str):
        raise EntryError("INVALID_ENTRY", "Choose a repository.")
    user = founder(identity)
    match = None
    for repo in list_user_repos(user):
        item = _repository(repo)
        if item and item["repository_id"] == repository_id and item["full_name"].lower() == full_name.lower():
            match = item
            break
    if match is None or not match["installation_id"]:
        raise EntryError("REPOSITORY_UNAVAILABLE", "This repository is not available through your GitHub connection.", 403)
    try:
        token = github_app.create_installation_access_token(
            installation_id=match["installation_id"],
            repository=match["full_name"],
            repository_id=int(repository_id),
            permission_mode="read",
        )
    except github_app.GitHubAppTokenError as exc:
        raise EntryError("GITHUB_UNAVAILABLE", "GitHub access could not be confirmed. Please try again.", 503) from exc
    return {
        "token": token.token,
        "expires_at": token.expires_at.isoformat() if token.expires_at else None,
    }
