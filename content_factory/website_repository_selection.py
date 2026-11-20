"""Repository selection: suggestion, permission check and merge-ability (plan E8, section 6.4).

Three things happen when a founder picks the repository that holds their website:

* **Suggestion.** :func:`rank_repositories` orders the installation's repositories
  by how well each matches the startup: the repository ``homepage`` equals the
  domain, the domain appears in ``CNAME`` or the host config (wrangler, Vercel,
  Netlify), the name matches the brand, it has a web framework manifest, it was
  pushed recently, and it is not archived or a fork. The top match is only
  **preselected** in the picker (``selectedRepo``); nothing here binds a
  repository. Binding still happens only when the founder saves their choice.
  The listing token is metadata-only, so the file probes
  (:func:`host_config_hints`) mint a read-only token per candidate, stay inside
  the founder's own installation and look at the top five only.
* **Permission check.** :func:`permission_check` reads the App's and the
  installation's existing grants (``require_installation_repository_permissions``,
  never a clone and never a write). A grant without contents access fails at
  selection with ``github_access_required`` and the missing permission named, so
  setup never starts against a repository it cannot read.
* **Merge-ability.** :func:`merge_ability` reads the branch rules and classic
  protection of the deploy branch, and the founder's own permission on the
  repository. When the founder cannot merge alone (a required review, code
  owners, or read-only access to a developer's repository) the journey's
  "approve it on GitHub" ask becomes ``send_pr_to_reviewer`` with the pull
  request link to share (:func:`reviewer_needed`, :func:`reviewer_ask`).

Nothing here loosens a merge guard. Every merge still re-reads the live branch
policy (``website_setup_merge_policy.setup_merge_policy``), still merges only
the exact validated pull request head, and GitHub still enforces protection.
The recorded merge-ability only chooses which ask the founder sees.
"""
from __future__ import annotations

import logging
import re
from datetime import timedelta, timezone as datetime_timezone
from urllib.parse import quote, urlsplit

from django.core.cache import cache
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .website_setup_merge_policy import REVIEW_REQUIRED_REASON

logger = logging.getLogger(__name__)

REVIEWER_ACTION = "send_pr_to_reviewer"
RECORD_KEY = "mergeAbility"
RECORD_VERSION = 1
PROBE_LIMIT = 5
PROBE_READ_LIMIT = 3
PROBE_MAX_BYTES = 64 * 1024
PROBE_CACHE_SECONDS = 600
PRESELECT_SCORE = 40

HOST_CONFIG_FILES = ("CNAME", "wrangler.toml", "wrangler.json", "wrangler.jsonc", "vercel.json", "netlify.toml")
FRAMEWORK_MANIFESTS = frozenset({
    "package.json", "gemfile", "hugo.toml", "hugo.yaml", "_config.yml", "mkdocs.yml", "pelicanconf.py",
    "composer.json", "astro.config.mjs", "astro.config.ts", "next.config.js", "next.config.mjs", "next.config.ts",
    "gatsby-config.js", "gatsby-config.ts", "nuxt.config.ts", "nuxt.config.js", "svelte.config.js",
    "vite.config.ts", "vite.config.js", "eleventy.config.js", ".eleventy.js", "index.html",
})
WEB_LANGUAGES = frozenset({"typescript", "javascript", "html", "css", "astro", "vue", "svelte", "mdx", "scss"})
WEB_NAME_HINTS = ("site", "web", "www", "landing", "homepage", "frontend", "blog", "marketing")
NOT_WEBSITE_HINTS = ("n8n", "api", "backend", "bot", "infra", "terraform", "scripts", "workflow", "etl",
                     "pipeline", "notebook", "ml", "model", "sdk", "cli", "server", "worker")

# Founder copy (no em or en dashes).
NOTHING_VISIBLE = ("We can't see your website's repository yet",
                   "Give MLAI's GitHub app access to the repository that holds your website, then come back here.",
                   "Choose repositories on GitHub")
NOT_A_WEBSITE = "This repository doesn't look like a website. Check it holds your site's code before you continue."
CONTENTS_PERMISSION_DETAIL = ("MLAI's GitHub app can't read the files in this repository. In GitHub, allow the app "
                              "Contents access for it, then choose it again.")
PULL_REQUESTS_PERMISSION_DETAIL = ("MLAI's GitHub app can't open pull requests in this repository. In GitHub, allow "
                                   "the app Pull requests access for it, then choose it again.")
SUSPENDED_DETAIL = "MLAI's GitHub app is suspended for this account. Unsuspend it in GitHub, then choose again."
REVIEWER_COPY = ("Send this change to your reviewer",
                 "Your website's GitHub settings need someone with review rights to approve this pull request "
                 "before it goes live. Open it and send them the link. If you can review it yourself, approve it on GitHub.",
                 "Open the pull request")
REVIEWER_NAMED_HEADLINE = "Send this change to {reviewer}"
GITHUB_LOGIN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?")


def _mapping(value):
    return value if isinstance(value, dict) else {}


def _host(value) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return ""
    if "://" not in text:
        text = "https://" + text
    try:
        host = urlsplit(text).hostname or ""
    except ValueError:
        return ""
    return host.removeprefix("www.").rstrip(".")


def _slug(value) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def _pushed_at(repo):
    stamp = parse_datetime(str(repo.get("pushedAt") or repo.get("pushed_at") or ""))
    if stamp is not None and timezone.is_naive(stamp):
        stamp = timezone.make_aware(stamp, timezone=datetime_timezone.utc)
    return stamp


def _full_name(repo) -> str:
    return str(repo.get("fullName") or repo.get("full_name") or "").strip()


def _score(repo, *, domain, brand, hints, now):
    """(score, reasons, looks_like_website) for one repository dict."""
    score, reasons = 0, []
    site = _host(domain)
    label = site.split(".", 1)[0] if site else ""
    name = str(repo.get("name") or _full_name(repo).split("/")[-1])
    slug = _slug(name)
    web = False
    if site and _host(repo.get("homepage")) == site:
        score += 100
        reasons.append("homepage_matches_domain")
        web = True
    hint = _mapping(hints.get(_full_name(repo).lower()))
    if hint.get("domainInConfig"):
        score += 80
        reasons.append("domain_in_host_config")
        web = True
    if site and slug and slug in {_slug(site), _slug(site.replace(".", "-"))}:
        score += 50
        reasons.append("name_matches_domain")
        web = True
    elif slug and slug in {_slug(label), _slug(brand)} - {""}:
        score += 40
        reasons.append("name_matches_brand")
    elif slug and any(token and len(token) >= 3 and token in slug for token in {_slug(label), _slug(brand)}):
        score += 20
        reasons.append("name_contains_brand")
    if hint.get("frameworkManifest"):
        score += 15
        reasons.append("framework_manifest")
        web = True
    language = str(repo.get("language") or "").lower()
    topics = {str(item).lower() for item in repo.get("topics") or [] if isinstance(item, str)}
    if language in WEB_LANGUAGES or topics & {"website", "nextjs", "astro", "hugo", "jekyll", "gatsby", "react"}:
        score += 10
        reasons.append("web_language")
        web = True
    if any(token in name.lower() for token in WEB_NAME_HINTS):
        web = True
    pushed = _pushed_at(repo)
    if pushed is not None:
        age = now - pushed
        bonus = 15 if age <= timedelta(days=30) else 8 if age <= timedelta(days=90) else 3 if age <= timedelta(days=365) else 0
        if bonus:
            score += bonus
            reasons.append("recently_pushed")
    if repo.get("archived") is True:
        score -= 100
        reasons.append("archived")
    if repo.get("fork") is True:
        score -= 30
        reasons.append("fork")
    return score, reasons, web


def rank_repositories(repos, *, domain="", brand="", hints=None, now=None) -> list[dict]:
    """Every repository with its score and reasons, best first (stable for equal scores).

    ``hints`` maps a lower-cased ``owner/name`` to :func:`host_config_hints` output.
    Pure: no network, no database.
    """
    now = now or timezone.now()
    hints = hints if isinstance(hints, dict) else {}
    ranked = []
    for index, repo in enumerate(repo for repo in repos or [] if isinstance(repo, dict) and _full_name(repo)):
        score, reasons, web = _score(repo, domain=domain, brand=brand, hints=hints, now=now)
        ranked.append({"fullName": _full_name(repo), "score": score, "reasons": reasons, "looksLikeWebsite": web,
                       "_order": index, "_pushed": _pushed_at(repo)})
    ranked.sort(key=lambda row: (-row["score"], -(row["_pushed"].timestamp() if row["_pushed"] else 0), row["_order"]))
    for row in ranked:
        row.pop("_order")
        row.pop("_pushed")
    return ranked


def suggestion(repos, *, domain="", brand="", hints=None, now=None, access_url=None) -> dict:
    """The picker's suggestion: which repository to preselect, why, and any soft warning.

    Preselects only a clear winner (a positive score above :data:`PRESELECT_SCORE`
    and ahead of the runner-up), or the only repository there is. Never binds.
    """
    ranked = rank_repositories(repos, domain=domain, brand=brand, hints=hints, now=now)
    live = [row for row in ranked if "archived" not in row["reasons"]]
    top = live[0] if live else None
    clear = bool(top and (len(ranked) == 1 or (top["score"] >= PRESELECT_SCORE
                                               and (len(live) == 1 or top["score"] > live[1]["score"]))))
    if len(ranked) == 1:
        top, clear = ranked[0], True
    plausible = any(row["score"] > 0 or row["looksLikeWebsite"] for row in live)
    result = {"repo": top["fullName"] if clear else None, "reasons": top["reasons"] if clear else [],
              "looksLikeWebsite": bool(top and top["looksLikeWebsite"]) if clear else None,
              "warning": None, "nothingVisible": not plausible, "ranked": [
                  {key: row[key] for key in ("fullName", "score", "reasons", "looksLikeWebsite")} for row in ranked[:10]]}
    if clear and not top["looksLikeWebsite"]:
        name = top["fullName"].split("/")[-1].lower()
        if any(token in re.split(r"[^a-z0-9]+", name) for token in NOT_WEBSITE_HINTS) or not top["reasons"]:
            result["warning"] = {"reason": "not_a_website", "message": NOT_A_WEBSITE}
    if not plausible:
        headline, body, label = NOTHING_VISIBLE
        result["nothingVisibleCopy"] = {"headline": headline, "body": body, "label": label,
                                        "url": access_url if _github_url(access_url) else None}
    return result


def _github_url(value) -> bool:
    try:
        parts = urlsplit(str(value or ""))
    except ValueError:
        return False
    return parts.scheme == "https" and parts.hostname == "github.com" and not parts.username and not parts.password


def installation_access_url(installation_id, *, account_login="", account_type="") -> str | None:
    """The GitHub page where the founder chooses which repositories the App may see."""
    identifier = str(installation_id or "").strip()
    if not identifier.isdigit():
        return None
    login = str(account_login or "").strip()
    if str(account_type or "").lower() == "organization" and GITHUB_LOGIN.fullmatch(login):
        return f"https://github.com/organizations/{login}/settings/installations/{identifier}"
    return f"https://github.com/settings/installations/{identifier}"


# ---------------------------------------------------------------------------
# File probes (read-only, top five, the founder's own installation)


def _probe_one(repo, *, domain, reader):
    """Hints for one candidate from its root listing and up to three host-config reads."""
    listing = reader(repo, "")
    if not isinstance(listing, list):
        return {}
    names = {str(item.get("name") or ""): str(item.get("type") or "") for item in listing if isinstance(item, dict)}
    lowered = {name.lower() for name, kind in names.items() if kind == "file"}
    hint = {"frameworkManifest": bool(lowered & FRAMEWORK_MANIFESTS), "domainInConfig": False, "configFile": None}
    site = _host(domain)
    reads = 0
    for name in HOST_CONFIG_FILES:
        if not site or names.get(name) != "file" or reads >= PROBE_READ_LIMIT:
            continue
        reads += 1
        text = reader(repo, name)
        if isinstance(text, str) and _mentions_domain(text[:PROBE_MAX_BYTES], site):
            hint.update(domainInConfig=True, configFile=name)
            break
    return hint


def _mentions_domain(text, site) -> bool:
    pattern = r"(?<![a-z0-9.-])(?:www\.)?" + re.escape(site) + r"(?![a-z0-9-])"
    return re.search(pattern, text.lower()) is not None


def host_config_hints(repos, ranked, *, domain, installation_for=None, reader=None) -> dict:
    """Probe the top :data:`PROBE_LIMIT` non-archived candidates; never raises.

    ``reader(repo_dict, path)`` returns the root listing (``path == ""``) or a
    file's text, or None. The default reader mints a read-only installation
    token per candidate and deletes it after use. Results are cached per
    repository, push time and domain.
    """
    by_name = {_full_name(repo).lower(): repo for repo in repos or [] if isinstance(repo, dict)}
    hints = {}
    candidates = [row["fullName"] for row in ranked if "archived" not in row["reasons"]][:PROBE_LIMIT]
    for full_name in candidates:
        repo = by_name.get(full_name.lower())
        if repo is None:
            continue
        key = "website-repository-hints-v1:" + ":".join(str(part) for part in (
            repo.get("installationId") or repo.get("installation_id"), repo.get("id"), full_name.lower(),
            repo.get("pushedAt") or repo.get("pushed_at"), _host(domain)))
        cached = cache.get(key)
        if isinstance(cached, dict):
            hints[full_name.lower()] = cached
            continue
        try:
            if reader is not None:
                hint = _probe_one(repo, domain=domain, reader=reader)
            else:
                with _ReadOnlyReader(repo) as read:
                    hint = _probe_one(repo, domain=domain, reader=read)
        except Exception:  # noqa: BLE001 - a hint is advice, never a gate
            logger.info("website_repository_hint_unavailable repo=%s", full_name)
            continue
        cache.set(key, hint, PROBE_CACHE_SECONDS)
        hints[full_name.lower()] = hint
    return hints


class _ReadOnlyReader:
    """A read-only, repository-scoped installation token for one candidate's probe."""

    def __init__(self, repo):
        self.repo = repo
        self.credential = None

    def __enter__(self):
        from integrations.services.github_app import create_installation_access_token
        repository_id = self.repo.get("id")
        self.credential = create_installation_access_token(
            installation_id=str(self.repo.get("installationId") or self.repo.get("installation_id") or ""),
            repository=_full_name(self.repo), permission_mode="read", use_cache=False,
            repository_id=repository_id if type(repository_id) is int and repository_id > 0 else None)
        return self.read

    def __exit__(self, *exc):
        from integrations import http_client
        if self.credential is not None:
            try:
                http_client.delete("https://api.github.com/installation/token",
                                   headers={"Authorization": f"Bearer {self.credential.token}"}, timeout=(3, 8))
            except Exception:  # noqa: BLE001
                pass
        return False

    def read(self, repo, path):
        from integrations import http_client
        url = f"https://api.github.com/repos/{quote(_full_name(repo), safe='/')}/contents/{quote(path, safe='/')}"
        accept = "application/vnd.github+json" if not path else "application/vnd.github.raw+json"
        response = http_client.get(url, headers={"Authorization": f"Bearer {self.credential.token}", "Accept": accept,
                                                 "X-GitHub-Api-Version": "2022-11-28"}, timeout=(3, 8))
        if response.status_code != 200:
            return None
        if not path:
            return response.json()
        return response.text[:PROBE_MAX_BYTES] if len(response.content or b"") <= PROBE_MAX_BYTES else None


# ---------------------------------------------------------------------------
# Permission check at selection


def permission_check(installation_id) -> dict:
    """The installation's grants for setup, or a clear refusal (``WebsiteAuthorityError``).

    Reads existing App and installation grants only. A provider outage or a
    platform configuration fault is not the founder's problem: the result is
    ``{"checked": False}`` and selection continues (setup re-checks before it
    writes). A grant without contents (or pull request) access, or a
    suspended installation, refuses with ``github_access_required`` and names
    the missing permission in ``fieldErrors``.
    """
    from integrations.services.github_app import (GitHubInstallationAccessRequired, GitHubAppTokenError,
                                                  require_installation_repository_permissions)
    from integrations import http_client
    from .website_contract import WebsiteAuthorityError
    try:
        permissions = require_installation_repository_permissions(str(installation_id or ""))
    except GitHubInstallationAccessRequired as exc:
        if getattr(exc, "grant", "installation") == "app":
            # The App's own grant lacks a permission: a platform fault the
            # founder cannot fix in their installation settings.
            logger.warning("website_selection_app_grant_incomplete installation=%s", installation_id)
            return {"checked": False}
        missing = tuple(getattr(exc, "missing_permissions", ()) or ())
        if "contents" in missing:
            reason, detail, permission = "contents_permission_missing", CONTENTS_PERMISSION_DETAIL, "contents"
        elif "pull_requests" in missing:
            reason, detail, permission = "pull_requests_permission_missing", PULL_REQUESTS_PERMISSION_DETAIL, "pull_requests"
        else:
            reason, detail, permission = "installation_suspended", SUSPENDED_DETAIL, None
        raise WebsiteAuthorityError("github_access_required", detail, field_errors=[
            {"field": "githubRepo", "reason": reason, "permission": permission}]) from exc
    except (GitHubAppTokenError, http_client.RequestException):
        return {"checked": False}
    return {"checked": True, "contents": permissions.get("contents"), "pullRequests": permissions.get("pull_requests"),
            "writable": permissions.get("contents") == "write" and permissions.get("pull_requests") == "write"}


# ---------------------------------------------------------------------------
# Merge-ability at selection


def merge_ability(*, repo, branch, owner_login="", founder_login="", get=None) -> dict:
    """Whether the founder can merge the setup pull request alone, from GitHub's own payloads.

    ``get(path)`` returns ``(status_code, payload)`` for a repository-relative
    API path. Unknown evidence (403/404 on classic protection, an outage) is
    recorded as unknown and never as "can merge": the live merge policy and
    GitHub still decide at merge time.
    """
    from .website_setup_merge_policy import _reviews_required
    branch_path = quote(str(branch or ""), safe="")
    review, code_owners, unknown = False, False, False
    for page in range(1, 11):
        status, rules = get(f"/rules/branches/{branch_path}?per_page=100&page={page}")
        if status in {403, 404}:
            unknown = True
            break
        if status != 200 or not isinstance(rules, list):
            return _ability(unknown=True)
        for rule in rules:
            if isinstance(rule, dict) and rule.get("type") == "pull_request":
                parameters = _mapping(rule.get("parameters"))
                review |= _reviews_required(parameters)
                code_owners |= bool(parameters.get("require_code_owner_review"))
        if len(rules) < 100:
            break
    status, protection = get(f"/branches/{branch_path}/protection")
    if status == 200 and isinstance(protection, dict):
        reviews = _mapping(protection.get("required_pull_request_reviews"))
        review |= _reviews_required(reviews) if reviews else False
        code_owners |= bool(reviews.get("require_code_owner_reviews"))
    elif status in {403, 404}:
        # Classic protection needs Administration read, which these credentials
        # deliberately lack: the policy is unknown, never "unprotected".
        unknown = True
    else:
        return _ability(unknown=True)
    permission = None
    login = str(founder_login or "").strip()
    if GITHUB_LOGIN.fullmatch(login):
        status, payload = get(f"/collaborators/{quote(login, safe='')}/permission")
        if status == 200 and isinstance(payload, dict):
            value = str(payload.get("permission") or "").lower()
            permission = value if value in {"admin", "maintain", "write", "triage", "read", "none"} else None
        # Anything else (including a 404 for a renamed login or a token that
        # cannot see collaborators) leaves the founder's permission unknown.
    can_write = permission in {"admin", "maintain", "write"} if permission else None
    if review or code_owners or can_write is False:
        can_merge = False
    elif can_write and not unknown:
        can_merge = True
    else:
        can_merge = None
    reviewer = None
    if can_write is False and GITHUB_LOGIN.fullmatch(str(owner_login or "")) and owner_login.lower() != login.lower():
        reviewer = owner_login
    return _ability(review=review, code_owners=code_owners, permission=permission, can_merge=can_merge,
                    reviewer=reviewer, unknown=unknown)


def _ability(*, review=None, code_owners=None, permission=None, can_merge=None, reviewer=None, unknown=False):
    return {"reviewRequired": review, "codeOwnerReview": code_owners, "founderPermission": permission,
            "founderCanMerge": can_merge, "reviewer": reviewer, "policyUnknown": bool(unknown)}


def _founder_login(user, installation_id) -> str:
    from integrations.models import GitHubInstallation
    row = (GitHubInstallation.objects.filter(user=user, installation_id=str(installation_id or ""))
           .only("github_user_name").first()) if user is not None and getattr(user, "pk", None) else None
    return str(getattr(row, "github_user_name", "") or "").strip()


def record_merge_ability(connection, *, user=None) -> dict | None:
    """Read and store the merge-ability of ``connection`` at selection; never raises.

    Stored on ``WebsiteConnection.capabilities["mergeAbility"]`` with the
    connection's generation, repository id and branch, so a later bind or
    branch change makes it stale rather than wrong. Writes nothing else.
    """
    from integrations import http_client
    from integrations.services.github_app import create_installation_access_token, github_app_credentials_configured
    if connection is None or not github_app_credentials_configured():
        return None
    credential = None
    try:
        credential = create_installation_access_token(installation_id=connection.installation_id,
            repository=connection.github_repo, repository_id=connection.repository_id, permission_mode="read",
            use_cache=False)
        headers = {"Authorization": f"Bearer {credential.token}", "Accept": "application/vnd.github+json",
                   "X-GitHub-Api-Version": "2022-11-28"}
        base = f"https://api.github.com/repos/{quote(connection.github_repo, safe='/')}"

        def get(path):
            response = http_client.get(base + path, headers=headers, timeout=(3, 15))
            try:
                payload = response.json() if response.status_code == 200 else None
            except ValueError:
                payload = None
            return response.status_code, payload
        ability = merge_ability(repo=connection.github_repo, branch=connection.branch,
                                owner_login=connection.github_repo.split("/", 1)[0],
                                founder_login=_founder_login(user or connection.authorized_by, connection.installation_id),
                                get=get)
    except Exception:  # noqa: BLE001 - merge-ability is advice; the merge path re-checks
        logger.info("website_merge_ability_unavailable connection=%s", getattr(connection, "pk", None))
        return None
    finally:
        if credential is not None:
            try:
                http_client.delete("https://api.github.com/installation/token",
                                   headers={"Authorization": f"Bearer {credential.token}"}, timeout=(3, 8))
            except Exception:  # noqa: BLE001
                pass
    return store_merge_ability(connection, ability)


def store_merge_ability(connection, ability) -> dict:
    """Merge ``ability`` into the connection's capabilities under a row lock."""
    from django.db import transaction
    from .website_models import WebsiteConnection
    record = {**ability, "version": RECORD_VERSION, "generation": connection.generation,
              "repositoryId": connection.repository_id, "branch": connection.branch,
              "checkedAt": timezone.now().isoformat()}
    with transaction.atomic():
        row = WebsiteConnection.objects.select_for_update().only("capabilities").get(pk=connection.pk)
        capabilities = {**_mapping(row.capabilities), RECORD_KEY: record}
        WebsiteConnection.objects.filter(pk=connection.pk).update(capabilities=capabilities)
    connection.capabilities = capabilities
    return record


def current_record(connection) -> dict | None:
    """The stored merge-ability, only while it describes this connection's generation and branch."""
    record = _mapping(_mapping(getattr(connection, "capabilities", None)).get(RECORD_KEY))
    if (not record or record.get("version") != RECORD_VERSION
            or record.get("generation") != getattr(connection, "generation", None)
            or record.get("repositoryId") != getattr(connection, "repository_id", None)
            or record.get("branch") != getattr(connection, "branch", None)):
        return None
    return record


def _result(run):
    return _mapping(getattr(run, "result", None))


def reviewer_needed(connection=None, run=None) -> bool:
    """Whether an "approve it on GitHub" ask should be "send this PR to your reviewer".

    True when GitHub refused the merge for a required review (the article merge
    worker's ``BRANCH_PROTECTION_APPROVAL_REQUIRED``, or the setup merge policy's
    review reason), or the merge-ability read at selection says the founder
    cannot merge alone.
    """
    result = _result(run)
    setup = _mapping(result.get("article_system_setup"))
    if result.get("error_code") == "BRANCH_PROTECTION_APPROVAL_REQUIRED":
        return True
    if REVIEW_REQUIRED_REASON in {result.get("merge_blocked_reason"), setup.get("merge_blocked_reason")}:
        return True
    record = current_record(connection) if connection is not None else None
    return bool(record and record.get("founderCanMerge") is False)


def reviewer_ask(connection=None, *, url=None) -> dict:
    """The ``send_pr_to_reviewer`` copy and share link (the pull request on GitHub)."""
    headline, body, label = REVIEWER_COPY
    record = current_record(connection) if connection is not None else None
    reviewer = str((record or {}).get("reviewer") or "")
    if GITHUB_LOGIN.fullmatch(reviewer):
        headline = REVIEWER_NAMED_HEADLINE.format(reviewer=reviewer)
    share = url if _github_url(url) and re.fullmatch(r"/[^/]+/[^/]+/pull/[1-9][0-9]*", urlsplit(url).path) else None
    return {"id": REVIEWER_ACTION, "headline": headline, "body": body, "label": label, "url": share,
            "reviewer": reviewer or None}


__all__ = ["REVIEWER_ACTION", "rank_repositories", "suggestion", "host_config_hints", "permission_check",
           "merge_ability", "record_merge_ability", "store_merge_ability", "current_record", "reviewer_needed",
           "reviewer_ask", "installation_access_url"]
