"""A renamed, moved, archived or private repository is still the same website.

GitHub's ``repository`` webhook reports a rename, a transfer to another
account, a visibility change and archiving with the repository's permanent
numeric id. The website connection is found by that id, so none of these
changes what was authorized. They used to revoke the connection (new
generation, cleared publish targets and settings, cancelled work), and the
founder could not reconnect because later checks compared the old name.

Here they are an identity refresh instead:

- renamed, transferred, privatized, publicized: write GitHub's current
  ``owner/name`` to the connection and its config copy. Generation, targets,
  settings and running work are kept.
- transferred: the old installation belongs to the old account. Find one of
  the authorizing founder's own installations that can see the repository.
  If none can, add a ``github_access_required`` blocker so the founder sees
  one action: give MLAI access on GitHub.
- archived: an archived repository cannot take a pull request, so add a
  blocker that pauses setup, preview and publish until it is unarchived.
  Nothing is revoked or cancelled; ``unarchived`` removes the blocker.

Deleting the repository, removing it from the installation and uninstalling
the app still revoke (``website_reconciliation.handle_website_github_event``).

``ARTICLES_REPOSITORY_IDENTITY_REFRESH_ENABLED`` (default on) is the kill
switch; off restores the old revoke for these actions.
"""
import logging
import re

from django.conf import settings
from django.db import transaction
from django.utils import timezone

logger = logging.getLogger(__name__)

FLAG = "ARTICLES_REPOSITORY_IDENTITY_REFRESH_ENABLED"

#: Repository webhook actions that keep the same repository (same numeric id).
IDENTITY_ACTIONS = frozenset({"renamed", "transferred", "privatized", "publicized", "archived", "unarchived"})

ACCESS_BLOCKER = "github_access_required"
ARCHIVED_BLOCKER = "github_repository_archived"
#: Blockers that stop setup, preview and publish but keep the connection.
PAUSING_BLOCKERS = frozenset({ACCESS_BLOCKER, ARCHIVED_BLOCKER})

ACCESS_MESSAGE = ("Your website's code moved to a GitHub account MLAI can't see yet. "
                  "Give MLAI access on GitHub to carry on.")
ARCHIVED_MESSAGE = ("Your website's code is archived on GitHub, so MLAI can't add articles to it. "
                    "Unarchive it on GitHub to carry on.")

_FULL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+")


def enabled() -> bool:
    """Kill switch for the identity refresh; default on."""
    return bool(getattr(settings, FLAG, True))


def valid_full_name(value) -> str:
    """GitHub's ``owner/name`` when it is well formed, else an empty string."""
    name = str(value or "").strip()
    if len(name) > 255 or not _FULL_NAME.fullmatch(name) or name.split("/")[-1] in {".", ".."}:
        return ""
    return name


def has_pausing_blocker(connection) -> bool:
    """True while an access or archived blocker holds setup, preview and publish."""
    return any(isinstance(item, dict) and item.get("code") in PAUSING_BLOCKERS
               for item in (getattr(connection, "blockers", None) or []))


def _locked(connection):
    from organizations.models import Organization
    from .website_models import WebsiteConnection
    Organization.objects.select_for_update().get(pk=connection.organization_id)
    return WebsiteConnection.objects.select_for_update().get(pk=connection.pk)


def refresh_repository_name(connection, full_name) -> bool:
    """Store GitHub's current ``owner/name`` on the connection and its config copy.

    Only the name changes. The caller has already matched the repository by
    its permanent id. Returns True when the stored name changed.
    """
    from .models import OrganizationContentConfig
    name = valid_full_name(full_name)
    if not name or name == connection.github_repo:
        return False
    with transaction.atomic():
        locked = _locked(connection)
        if locked.repository_id != connection.repository_id or locked.github_repo == name:
            connection.github_repo = locked.github_repo
            return False
        previous = locked.github_repo
        locked.github_repo = name
        locked.save(update_fields=["github_repo", "updated_at"])
        OrganizationContentConfig.objects.filter(website_connection=locked).update(
            github_repo=name, updated_at=timezone.now())
    connection.github_repo = name
    logger.info("website_repository_name_refreshed connection=%s repository_id=%s previous=%s current=%s",
                connection.pk, connection.repository_id, previous, name)
    return True


def set_blocker(connection, code, message, present) -> bool:
    """Add (``present``) or remove one blocker without touching any other state."""
    with transaction.atomic():
        locked = _locked(connection)
        current = [item for item in (locked.blockers or []) if isinstance(item, dict)]
        kept = [item for item in current if item.get("code") != code]
        updated = [*kept, {"code": code, "message": message, "retryable": False}] if present else kept
        if updated == current:
            connection.blockers = current
            return False
        locked.blockers = updated
        locked.save(update_fields=["blockers", "updated_at"])
    connection.blockers = updated
    return True


def update_installation(connection, installation_id) -> bool:
    """Point the connection and its config at an installation that can see the repository."""
    from .models import OrganizationContentConfig
    installation_id = str(installation_id or "").strip()
    if not installation_id or installation_id == connection.installation_id:
        return False
    with transaction.atomic():
        locked = _locked(connection)
        locked.installation_id = installation_id
        locked.save(update_fields=["installation_id", "updated_at"])
        OrganizationContentConfig.objects.filter(website_connection=locked).update(
            github_installation_id=installation_id, updated_at=timezone.now())
    connection.installation_id = installation_id
    return True


def installation_can_see(installation_id, *, repository_id, full_name):
    """True when GitHub issues a read credential for this repository id, False on a
    definite refusal, None when GitHub could not answer."""
    from integrations import http_client
    from integrations.services.github_app import GitHubAppTokenError, create_installation_access_token
    try:
        credential = create_installation_access_token(installation_id=str(installation_id), repository=full_name,
            repository_id=int(repository_id), permission_mode="read", use_cache=False)
    except GitHubAppTokenError as exc:
        return False if getattr(exc, "status_code", None) in {403, 404, 422} else None
    except Exception:
        logger.warning("website_repository_access_probe_failed installation_id=%s", installation_id)
        return None
    try:
        # A probe credential must not outlive the probe.
        http_client.delete("https://api.github.com/installation/token",
                           headers={"Authorization": f"Bearer {credential.token}"}, timeout=(3, 8))
    except Exception:
        pass
    return True


def _candidate_installations(connection, owner_login):
    """The connection's installation, then the authorizing founder's own installations,
    those on the new owner's account first. Another person's installation is never used."""
    from integrations.models import GitHubInstallation
    candidates = [str(connection.installation_id or "").strip()]
    if connection.authorized_by_id:
        rows = list(GitHubInstallation.objects.filter(user_id=connection.authorized_by_id)
                    .values_list("installation_id", "account_login"))
        owner = str(owner_login or "").casefold()
        rows.sort(key=lambda row: str(row[1] or "").casefold() != owner)
        candidates.extend(str(identifier or "").strip() for identifier, _ in rows)
    seen, ordered = set(), []
    for identifier in candidates:
        if identifier and identifier not in seen:
            seen.add(identifier)
            ordered.append(identifier)
    return ordered


def resolve_access(connection, owner_login="") -> str:
    """After a transfer: keep or find an installation that can see the repository.

    Returns ``kept``, ``moved``, ``access_required`` or ``unknown``. Only a
    definite answer changes the blocker; ``unknown`` leaves things as they are.
    """
    unknown = False
    for identifier in _candidate_installations(connection, owner_login):
        verdict = installation_can_see(identifier, repository_id=connection.repository_id,
                                       full_name=connection.github_repo)
        if verdict is True:
            moved = update_installation(connection, identifier)
            set_blocker(connection, ACCESS_BLOCKER, ACCESS_MESSAGE, False)
            return "moved" if moved else "kept"
        if verdict is None:
            unknown = True
    if unknown:
        return "unknown"
    set_blocker(connection, ACCESS_BLOCKER, ACCESS_MESSAGE, True)
    return "access_required"


def handle_repository_event(action, payload) -> dict:
    """Apply a ``repository`` webhook action from :data:`IDENTITY_ACTIONS`."""
    from .website_models import WebsiteConnection
    repository = payload.get("repository") if isinstance(payload.get("repository"), dict) else {}
    repository_id = repository.get("id")
    result = {"refreshed": 0, "access_required": 0}
    if type(repository_id) is not int or repository_id < 1:
        result["ignored"] = True
        return result
    full_name = valid_full_name(repository.get("full_name"))
    owner = repository.get("owner") if isinstance(repository.get("owner"), dict) else {}
    owner_login = str(owner.get("login") or (full_name.split("/", 1)[0] if full_name else ""))
    for connection in WebsiteConnection.objects.filter(repository_id=repository_id).select_related("organization"):
        changed = refresh_repository_name(connection, full_name) if full_name else False
        if connection.state in {"connected", "paused"}:
            if action == "transferred":
                outcome = resolve_access(connection, owner_login)
                changed = changed or outcome in {"moved", "access_required"}
                result["access_required"] += outcome == "access_required"
            elif action in {"archived", "unarchived"}:
                changed = set_blocker(connection, ARCHIVED_BLOCKER, ARCHIVED_MESSAGE, action == "archived") or changed
        result["refreshed"] += bool(changed)
        logger.info("website_repository_identity_event connection=%s repository_id=%s action=%s changed=%s",
                    connection.pk, repository_id, action, changed)
    return result
