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
from datetime import timedelta
from typing import Optional

from django.conf import settings
from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone

from integrations.models import GitHubInstallation
from integrations.services.github_app import (
    GitHubAppTokenError,
    INSTALLATION_DEAD,
    INSTALLATION_LIVE,
    create_installation_access_token,
    github_app_credentials_configured,
    list_app_installation_ids,
    list_installation_repositories_via_app,
    probe_installation_liveness,
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


def installation_liveness(inst) -> str:
    """live / dead / unknown for a stored installation row.

    Thin wrapper over ``github_app.probe_installation_liveness`` so callers work
    with a ``GitHubInstallation`` rather than a raw id. ``dead`` is only ever
    returned on a definitive GitHub not-found/gone; every ambiguity is
    ``unknown``.
    """
    return probe_installation_liveness(str(getattr(inst, "installation_id", "") or ""))


def _installation_confirmed_dead(inst, *, now=None) -> bool:
    """Whether we can currently confirm this installation is gone.

    Reuses the sweep's ``liveness_checked_at`` as a read-time cache so the
    "registry exists" guards do not hit the GitHub API on every request: the
    sweep stamps only live/unknown rows (dead rows are deleted, not stamped), so
    a row stamped within the probe interval was observed non-dead and is trusted
    without a network call. Only rows never checked (the pre-first-sweep window)
    or checked longer ago than the interval are probed live. Never treats a row
    as dead when the App can't be probed (unconfigured credentials).
    """
    if not github_app_credentials_configured():
        return False
    now = now or timezone.now()
    checked = getattr(inst, "liveness_checked_at", None)
    if checked is not None and (now - checked) < _installation_probe_interval():
        return False
    return installation_liveness(inst) == INSTALLATION_DEAD


def user_has_registered_installation(user) -> bool:
    """Whether the founder registry should be treated as the source of truth.

    A row whose installation GitHub reports as gone (uninstalled) is stale
    poison: it lists no repos yet its mere presence flips "registry exists"
    truthiness checks, hard-blocking the legacy per-org fallback and dead-ending
    the wizard (golden-repo baseline N1). This returns ``True`` when *any*
    installation is live — or cannot be disproven (unknown: suspended /
    transient / App credentials unconfigured) — so the only thing that
    un-blocks the legacy fallback is a registry whose every installation is
    *confirmed* dead. When the App can't be probed at all, behavior is
    unchanged (rows still count).

    Short-circuits on the first non-dead installation, and trusts the sweep's
    recent ``liveness_checked_at`` stamp instead of re-probing GitHub, so in
    steady state (post-sweep) this is a pure DB read.
    """
    installs = user_github_installations(user)
    if not installs:
        return False
    now = timezone.now()
    for inst in installs:
        if not _installation_confirmed_dead(inst, now=now):
            return True
    return False


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


# --- Reconciliation sweep: prune stale (uninstalled) installations -----------
#
# A founder who uninstalls the GitHub App (or lets an old install/uninstall
# cycle lapse) leaves a GitHubInstallation row that lists no repos yet still
# trips "registry exists" guards, hard-blocking the legacy fallback and
# dead-ending the wizard (golden-repo baseline N1: user 1 held two dead
# drsamdonegan installations, 114549385 + 140961835). This periodic sweep
# probes each row's liveness against GitHub and deletes the confirmed-dead
# ones. Deletion (over a stale flag) keeps every consumer correct with no
# filter to thread through — a row that does not exist cannot poison a guard —
# and the OAuth callback re-creates the row on a genuine reinstall.
#
# It is registered as a runner in the run_scheduled_discovery loop (mlai's only
# scheduler; ~60s tick) so it must stay cheap and self-throttling: only rows
# older than a minimum age are eligible, each row is re-probed at most once per
# probe interval (``liveness_checked_at``), and at most a small batch is probed
# per tick. Knobs (all optional settings; sane defaults):
#   GITHUB_INSTALLATION_RECONCILIATION_MIN_AGE_DAYS         (default 1)
#   GITHUB_INSTALLATION_RECONCILIATION_PROBE_INTERVAL_HOURS (default 24)
#   GITHUB_INSTALLATION_RECONCILIATION_BATCH_LIMIT          (default 10)


def _setting_int(name: str, default: int) -> int:
    try:
        value = int(getattr(settings, name, default) or default)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _installation_min_age() -> timedelta:
    return timedelta(days=_setting_int("GITHUB_INSTALLATION_RECONCILIATION_MIN_AGE_DAYS", 1))


def _installation_probe_interval() -> timedelta:
    return timedelta(hours=_setting_int("GITHUB_INSTALLATION_RECONCILIATION_PROBE_INTERVAL_HOURS", 24))


def _installation_batch_limit() -> int:
    return _setting_int("GITHUB_INSTALLATION_RECONCILIATION_BATCH_LIMIT", 10)


def run_github_installation_reconciliation_sweep(*, limit: Optional[int] = None, now=None) -> dict:
    """One liveness pass over the founder installation registry.

    Idempotent, self-throttling, and safe to tick every scheduler loop. Deletes
    installations GitHub confirms are gone (404/410); leaves live and
    inconclusive (``unknown``) rows untouched, stamping ``liveness_checked_at``
    so they are not re-probed until the probe interval elapses. A dead-probed row
    that GitHub's own ``/app/installations`` list still reports as owned is a
    contradiction (an incident / eventual-consistency window), so it is withheld
    rather than pruned.
    """
    now = now or timezone.now()
    summary = {
        "status": "completed",
        "checked": 0,
        "pruned": 0,
        "live": 0,
        "unknown": 0,
        "skipped_raced": 0,
        "withheld_contradiction": 0,
    }

    # Every probe would be inconclusive without App credentials; do not stamp a
    # whole registry as "checked" on a probe we cannot actually perform.
    if not github_app_credentials_configured():
        summary["status"] = "skipped"
        summary["reason"] = "github_app_unconfigured"
        return summary

    limit = limit if isinstance(limit, int) and limit > 0 else _installation_batch_limit()
    age_cutoff = now - _installation_min_age()
    probe_cutoff = now - _installation_probe_interval()

    candidates = list(
        GitHubInstallation.objects.filter(created_at__lt=age_cutoff)
        .filter(Q(liveness_checked_at__isnull=True) | Q(liveness_checked_at__lt=probe_cutoff))
        .order_by(F("liveness_checked_at").asc(nulls_first=True), "created_at")[:limit]
    )
    if not candidates:
        return summary

    # Probe first, defer deletes. A 404 is ambiguous between "uninstalled" and
    # "these credentials authenticate as a different App than minted this row",
    # so live/unknown rows are stamped immediately but dead rows are only pruned
    # once we confirm the configured App actually owns this DB's registry.
    dead_rows = []
    for inst in candidates:
        summary["checked"] += 1
        liveness = installation_liveness(inst)
        if liveness == INSTALLATION_DEAD:
            dead_rows.append(inst)
            continue
        _stamp_installation_checked(inst.pk, now)
        if liveness == INSTALLATION_LIVE:
            summary["live"] += 1
        else:
            summary["unknown"] += 1

    if not dead_rows:
        return summary

    # Anti-mass-delete guard: prune only when the configured App demonstrably
    # owns at least one installation stored in THIS database. If it owns none of
    # them (or the App's installation list can't be read), every 404 this pass is
    # far more likely a credential/App-id mismatch than a real uninstall wave, so
    # refuse to delete and alarm instead of silently draining a live registry.
    owned_ids = list_app_installation_ids()
    app_owns_local_registry = bool(owned_ids) and GitHubInstallation.objects.filter(
        installation_id__in=owned_ids
    ).exists()
    if not app_owns_local_registry:
        summary["status"] = "aborted_ownership_unconfirmed"
        summary["dead_withheld"] = len(dead_rows)
        logger.error(
            "github_installation_reconciliation ABORTED pruning of %d installation(s) "
            "probed 404: the configured GitHub App owns none of this database's stored "
            "installations (likely a credential/App-id mismatch, not real uninstalls). "
            "Withheld ids: %s",
            len(dead_rows),
            [inst.installation_id for inst in dead_rows],
        )
        return summary

    # Per-row contradiction guard: GitHub is not always internally consistent. In
    # a rare incident / eventual-consistency window, GET /app/installations still
    # LISTS an installation as owned while POST .../access_tokens returns 404 for
    # that SAME id (the signal ``installation_liveness`` reads as DEAD). Pruning on
    # that 404 alone would hard-delete a still-live founder installation, dropping
    # its stored user/refresh token — recovery then needs a full OAuth re-auth. We
    # already hold the authoritative owned set, so withhold any dead row whose id
    # GitHub still lists as owned and prune only ids the list agrees are gone.
    owned = {str(i) for i in owned_ids}

    for inst in dead_rows:
        if str(inst.installation_id) in owned:
            summary["withheld_contradiction"] += 1
            logger.warning(
                "github_installation_reconciliation withholding installation "
                "id=%s user_id=%s account=%s: GitHub /app/installations lists it as "
                "owned but access-token mint returned 404 — treating as inconclusive, "
                "not pruning",
                inst.installation_id,
                inst.user_id,
                inst.account_login or inst.github_user_name,
            )
            continue
        # Conditional on updated_at so a concurrent reinstall/refresh of this exact
        # row (which bumps auto_now updated_at) is never clobbered by a probe that
        # observed the pre-refresh state.
        deleted, _ = GitHubInstallation.objects.filter(
            pk=inst.pk, updated_at=inst.updated_at
        ).delete()
        if deleted:
            summary["pruned"] += 1
            logger.warning(
                "github_installation_reconciliation pruned stale installation "
                "id=%s user_id=%s account=%s: GitHub reports it uninstalled",
                inst.installation_id,
                inst.user_id,
                inst.account_login or inst.github_user_name,
            )
        else:
            summary["skipped_raced"] += 1

    return summary


def _stamp_installation_checked(pk, now) -> None:
    # .update() skips auto_now, so recording the probe never disturbs updated_at
    # (which the conditional delete keys on) or last_used_at.
    with transaction.atomic():
        GitHubInstallation.objects.filter(pk=pk).update(liveness_checked_at=now)
