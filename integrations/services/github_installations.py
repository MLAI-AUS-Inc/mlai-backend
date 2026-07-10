"""Founder-scoped GitHub access.

GitHub is shared *per founder* — the inverse of Gmail / financial connectors,
which are isolated per startup. A founder authorizes the GitHub App once (per
account) and every one of their companies can list + publish to the repos in that
installation. This module resolves GitHub *by user*:

- ``list_user_repos`` — union of repos across all the founder's installations.
- ``installation_for_repo`` — which installation contains a given ``owner/repo``.
- ``mint_user_repo_token`` — a write token for any repo the founder authorized,
  usable from any of their companies.
- ``upsert_github_installation`` — record/refresh an installation (used by the
  OAuth callback).

Everything degrades to the legacy per-org path when the registry is empty, so an
un-backfilled deployment behaves exactly as before.
"""

from __future__ import annotations

import logging
from typing import Optional

from django.utils import timezone

from integrations.models import GitHubInstallation
from integrations.services.github_app import (
    GitHubAppTokenError,
    create_installation_access_token,
    github_app_credentials_configured,
    list_installation_repositories_via_app,
)

logger = logging.getLogger(__name__)


def resolve_user_for_actor_id(actor_id):
    """Map a connected_slack_user_id / slack_user_id back to a core.User.

    Actor ids are either a real Slack id or the synthetic ``mlai_user:{id}``
    (founder_tools.services.actor_ids_for_user). Returns None when unresolvable.
    """
    from django.contrib.auth import get_user_model

    value = str(actor_id or "").strip()
    if not value:
        return None
    User = get_user_model()
    if value.startswith("mlai_user:"):
        try:
            uid = int(value.split(":", 1)[1])
        except (ValueError, IndexError):
            return None
        return User.objects.filter(id=uid).first()
    return User.objects.filter(slack_id=value).first()


def upsert_github_installation(
    *,
    user,
    installation_id,
    account_login: str = "",
    account_type: str = "",
    github_user_name: str = "",
    user_token: Optional[str] = None,
    refresh_token: Optional[str] = None,
    token_expires_at=None,
    scopes: Optional[list] = None,
) -> Optional[GitHubInstallation]:
    """Create/refresh a founder's installation row.

    Token fields are only overwritten when fresh values are supplied, so an
    identity-only re-auth never wipes a still-usable token.
    """
    installation_id = str(installation_id or "").strip()
    if user is None or not getattr(user, "id", None) or not installation_id:
        return None

    defaults = {
        "account_login": (account_login or github_user_name or "").strip(),
        "account_type": (account_type or "").strip(),
        "github_user_name": (github_user_name or "").strip(),
    }
    if scopes is not None:
        defaults["github_scopes"] = scopes
    if user_token:
        defaults["github_user_token_encrypted"] = user_token
    if refresh_token:
        defaults["github_refresh_token_encrypted"] = refresh_token
    if token_expires_at is not None:
        defaults["github_token_expires_at"] = token_expires_at

    obj, _created = GitHubInstallation.objects.update_or_create(
        user=user,
        installation_id=installation_id,
        defaults=defaults,
    )
    return obj


def user_github_installations(user) -> list:
    if user is None or not getattr(user, "id", None):
        return []
    return list(GitHubInstallation.objects.filter(user=user))


def _normalize_repo(repo: dict, installation_id: str, account_login: str = "") -> dict:
    """Shape a raw GitHub repo dict to the payload the frontend repo picker uses.

    Matches vibe_marketing_views._serialize_github_repo_payload, plus an
    ``accountLogin`` so the picker can group repos by GitHub account.
    """
    owner = repo.get("owner") if isinstance(repo.get("owner"), dict) else {}
    full_name = str(repo.get("full_name") or "").strip()
    owner_name = str(
        owner.get("login") or (full_name.split("/", 1)[0] if "/" in full_name else "")
    ).strip()
    name = str(repo.get("name") or (full_name.split("/", 1)[-1] if full_name else "")).strip()
    default_branch = str(repo.get("default_branch") or "").strip()
    installation_id = str(installation_id or "").strip()
    account_login = (account_login or owner_name).strip()
    return {
        "fullName": full_name,
        "full_name": full_name,
        "owner": owner_name,
        "name": name,
        "private": bool(repo.get("private")),
        "defaultBranch": default_branch,
        "default_branch": default_branch,
        "installationId": installation_id,
        "installation_id": installation_id,
        "accountLogin": account_login,
        "account_login": account_login,
    }


def _list_repos_for_installation(inst: GitHubInstallation) -> list:
    installation_id = str(inst.installation_id or "").strip()
    if not installation_id:
        return []
    account_login = (inst.account_login or inst.github_user_name or "").strip()

    normalized = None
    # Durable path: App key only, unaffected by an expired user token.
    if github_app_credentials_configured():
        try:
            raw = list_installation_repositories_via_app(installation_id)
            normalized = [_normalize_repo(r, installation_id, account_login) for r in raw]
        except GitHubAppTokenError as exc:
            logger.warning(
                "github_app_repo_list_failed installation_id=%s error=%s",
                installation_id,
                exc,
            )

    # Fallback: the stored user token (legacy, may be expired).
    if normalized is None:
        token = str(inst.github_user_token_encrypted or "").strip()
        if token:
            try:
                from content_factory.vibe_marketing_views import (
                    _list_github_repositories_for_token,
                )

                normalized = _list_github_repositories_for_token(
                    token=token, installation_id=installation_id
                )
                for repo in normalized:
                    repo.setdefault("accountLogin", account_login)
                    repo.setdefault("account_login", account_login)
            except Exception as exc:  # noqa: BLE001 - listing is best-effort
                logger.warning(
                    "github_user_token_repo_list_failed installation_id=%s error=%s",
                    installation_id,
                    exc,
                )

    return normalized or []


def list_user_repos(user) -> list:
    """Union of repos across every installation the founder has authorized."""
    repos_by_name: dict = {}
    for inst in user_github_installations(user):
        for repo in _list_repos_for_installation(inst):
            name = str(repo.get("fullName") or repo.get("full_name") or "").strip().lower()
            if name and name not in repos_by_name:
                repos_by_name[name] = repo
    return list(repos_by_name.values())


def installation_for_repo(user, repo) -> Optional[GitHubInstallation]:
    """Which of the founder's installations can access ``owner/repo``."""
    repo = str(repo or "").strip()
    if not repo or user is None:
        return None
    installs = user_github_installations(user)
    if not installs:
        return None
    owner = repo.split("/", 1)[0].strip().lower() if "/" in repo else ""
    owner_matches = [
        inst for inst in installs if (inst.account_login or "").strip().lower() == owner
    ]
    if len(owner_matches) == 1:
        return owner_matches[0]

    if len(installs) == 1:
        # An installation belongs to exactly one GitHub account. Never pair a
        # repository owned by another account with the founder's sole legacy
        # installation; that produces GitHub's opaque 422 response when a
        # repository-scoped token is minted. Unknown legacy account metadata is
        # confirmed by listing below.
        account_login = (installs[0].account_login or "").strip().lower()
        if owner and account_login and owner != account_login:
            return None
        if owner and account_login == owner:
            return installs[0]

    # Ambiguous (or account_login unknown): confirm by listing.
    for inst in owner_matches or installs:
        names = {
            str(r.get("fullName") or r.get("full_name") or "").strip().lower()
            for r in _list_repos_for_installation(inst)
        }
        if repo.lower() in names:
            return inst
    return None


def mint_user_repo_token(user, repo, *, permission_mode: str = "write"):
    """A GitHub App installation token for ``repo`` from the founder's registry.

    Returns a GitHubInstallationToken, or None when no installation covers the
    repo. Works from any company because the installation is the founder's.
    """
    inst = installation_for_repo(user, repo)
    if inst is None:
        return None
    token = create_installation_access_token(
        installation_id=str(inst.installation_id).strip(),
        repository=str(repo).strip(),
        permission_mode=permission_mode,
    )
    GitHubInstallation.objects.filter(pk=inst.pk).update(last_used_at=timezone.now())
    return token
