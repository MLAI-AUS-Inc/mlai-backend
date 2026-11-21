"""Repo health at connect (workstream 3): can this founder's repo build and deploy?

Five of La Vie's problems were in her own repository or hosting and were
detectable the day she connected. This module keeps one record of them on the
website connection and turns it into plain founder copy.

* Build probe (Content Factory's preview builder, ``repo_health`` mode): the
  site's own install and build on its current head. Its result arrives here and
  is stored as-is (code, blocking, last 40 log lines).
* Host probe (here, through the GitHub App): deployments, commit statuses and
  check runs for the head and the last five commits on the branch. No host
  signal in 14 days while commits landed raises ``host_not_deploying``. A
  missing Checks: read grant raises ``checks_permission_missing``.
* Sitemap: ``/sitemap.xml`` on the site, through the same pinned public-origin
  fetch website verification uses. Unreachable is a warning.

Owner decision D1: build and host failures block setup, the rest warn.
Owner decision D2: the record lives under ``WebsiteConnection.capabilities``
(key ``repo_health``), so there is no schema change.

Everything is behind ``ARTICLES_REPO_HEALTH_ENABLED`` (default off): with it
off nothing is stored, nothing is fetched, the journey has no ``repoHealth``
block and no charge is gated.
"""
from __future__ import annotations

from datetime import datetime, timedelta
import json
import re

from django.conf import settings
from django.db import transaction
from django.utils import timezone

KEY = "repo_health"
VERSION = 1
BUILD_SCHEMA = "repo_health_build_probe.v1"
TAIL_LINES = 40
HOST_WINDOW = timedelta(days=14)
RECENT_COMMITS = 6  # the head and the last five commits

LOCKFILE_OUT_OF_SYNC = "lockfile_out_of_sync"
FRAMEWORK_BUILD_BLOCKED_BY_ADVISORY = "framework_build_blocked_by_advisory"
TYPE_ERROR = "type_error"
MISSING_ENV = "missing_env"
UNSUPPORTED_NODE = "unsupported_node"
BUILD_FAILED_OTHER = "build_failed_other"
BUILD_CODES = (LOCKFILE_OUT_OF_SYNC, FRAMEWORK_BUILD_BLOCKED_BY_ADVISORY, TYPE_ERROR, MISSING_ENV, UNSUPPORTED_NODE,
               BUILD_FAILED_OTHER)
HOST_NOT_DEPLOYING = "host_not_deploying"
CHECKS_PERMISSION_MISSING = "checks_permission_missing"
SITEMAP_UNREACHABLE = "sitemap_unreachable"
BLOCKING_CODES = frozenset((*BUILD_CODES, HOST_NOT_DEPLOYING))
WARNING_CODES = frozenset((CHECKS_PERMISSION_MISSING, SITEMAP_UNREACHABLE))
# Display order: what stops setup first, then warnings.
ORDER = (*BUILD_CODES, HOST_NOT_DEPLOYING, CHECKS_PERMISSION_MISSING, SITEMAP_UNREACHABLE)

# One plain sentence and the one action that fixes it.
COPY = {
    LOCKFILE_OUT_OF_SYNC: ("Your lockfile is out of date, so a clean install of your site fails.",
                           "Run npm install and commit package-lock.json."),
    FRAMEWORK_BUILD_BLOCKED_BY_ADVISORY: ("Your framework version has a security advisory, so its build refuses to run.",
                                          "Upgrade your framework to its latest patch release and commit the change."),
    TYPE_ERROR: ("Your site doesn't build because of a type error.",
                 "Run your build locally, fix the type error and commit the fix."),
    MISSING_ENV: ("Your site's build needs an environment variable that isn't set.",
                  "Give that variable a safe default in your code, then commit the change."),
    UNSUPPORTED_NODE: ("Your site asks for a Node.js version our builder doesn't run.",
                       "Allow Node.js 22 in the engines field of package.json and commit it."),
    BUILD_FAILED_OTHER: ("Your site doesn't build on its latest commit.",
                         "Run your build locally, fix the error it shows and commit the fix."),
    HOST_NOT_DEPLOYING: ("Your host hasn't deployed any of your recent commits in the last 14 days.",
                         "Check your hosting project is linked to this repository and branch, then push a commit."),
    CHECKS_PERMISSION_MISSING: ("MLAI can't read your repository's checks, so it can't confirm your deploys.",
                                "Give the MLAI GitHub App read access to Checks."),
    SITEMAP_UNREACHABLE: ("We couldn't load your sitemap, so new articles may be slower to show up in search.",
                          "Make sure /sitemap.xml loads on your live site."),
}

_SHA = re.compile(r"[0-9a-f]{40}")
_HOST_WORDS = ("vercel", "netlify", "cloudflare", "pages", "render", "amplify", "railway", "fly", "deploy", "firebase",
               "heroku", "surge", "github-pages")
_BUILD_INPUTS = re.compile(
    r"(?:^|/)(?:package\.json|package-lock\.json|npm-shrinkwrap\.json|pnpm-lock\.yaml|yarn\.lock|bun\.lockb?|\.nvmrc"
    r"|\.node-version|(?:next|nuxt|astro|vite|svelte|remix|gatsby|vue|angular)\.config\.[cm]?[jt]s|angular\.json)$")


def enabled() -> bool:
    return bool(getattr(settings, "ARTICLES_REPO_HEALTH_ENABLED", False))


def fix_pr_enabled() -> bool:
    return enabled() and bool(getattr(settings, "ARTICLES_REPO_HEALTH_FIX_PR_ENABLED", False))


def _mapping(value):
    return value if isinstance(value, dict) else {}


def _sha(value):
    return value if isinstance(value, str) and _SHA.fullmatch(value) else ""


def _moment(value):
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else None


# ---------------------------------------------------------------------------
# Stored record


def stored(connection) -> dict:
    """This generation's record, or an empty one (a new generation starts clean)."""
    value = _mapping(_mapping(getattr(connection, "capabilities", None)).get(KEY))
    if value.get("version") != VERSION or value.get("generation") != getattr(connection, "generation", None):
        return {}
    return value


def _store(connection, patch: dict) -> dict:
    """Merge ``patch`` into this connection's record; only this key changes."""
    from .website_models import WebsiteConnection
    with transaction.atomic():
        row = WebsiteConnection.objects.select_for_update().filter(pk=connection.pk).first()
        if row is None:
            return {}
        record = {**stored(row), **patch, "version": VERSION, "generation": row.generation}
        capabilities = {**_mapping(row.capabilities), KEY: record}
        # updated_at is left alone so no lease or freshness check reads this
        # bookkeeping write as founder activity.
        WebsiteConnection.objects.filter(pk=row.pk).update(capabilities=capabilities)
    connection.capabilities = capabilities
    return record


def current_head(connection) -> str:
    """The newest head this record has seen on the branch, else the verified source."""
    return _sha(stored(connection).get("head_sha")) or _sha(getattr(connection, "verified_sha", ""))


def record_head(connection, sha: str):
    if not enabled() or not _sha(sha):
        return None
    return _store(connection, {"head_sha": sha})


def valid_build_result(value) -> bool:
    """The Content Factory build-probe result shape (``harness.repo_health``)."""
    if not isinstance(value, dict) or value.get("schema") != BUILD_SCHEMA or value.get("status") not in {"passed", "failed"}:
        return False
    tail = value.get("log_tail")
    if not isinstance(tail, list) or len(tail) > TAIL_LINES or not all(isinstance(row, str) for row in tail):
        return False
    if not _sha(value.get("commit_sha")):
        return False
    if value["status"] == "passed":
        return value.get("code") is None and value.get("blocking") is False
    return value.get("code") in BUILD_CODES and value.get("blocking") is True


def record_build_probe(connection, result: dict, *, now=None):
    """Keep one build-probe result for the commit it built."""
    if not enabled():
        return None
    if not valid_build_result(result):
        raise ValueError("repo_health_build_result_invalid")
    now = now or timezone.now()
    build = {key: result[key] for key in ("status", "code", "blocking", "commit_sha")}
    build.update(log_tail=[row[:500] for row in result["log_tail"]], checked_at=now.isoformat())
    return _store(connection, {"build": build})


# ---------------------------------------------------------------------------
# Host probe


class HostProbeUnavailable(Exception):
    """GitHub could not be read; the host state stays unknown, never failed."""


def _host_signal_times(reader, sha):
    """When a host deployed or reported on ``sha``: deployments, host statuses, host check runs."""
    times = []
    for row in reader.deployments(sha):
        times.append(_moment(_mapping(row).get("created_at")))
    for row in reader.statuses(sha):
        row = _mapping(row)
        if any(word in str(row.get("context") or "").lower() for word in _HOST_WORDS):
            times.append(_moment(row.get("updated_at") or row.get("created_at")))
    for row in reader.check_runs(sha):
        row = _mapping(row)
        label = (str(_mapping(row.get("app")).get("slug") or "") + " " + str(row.get("name") or "")).lower()
        if any(word in label for word in _HOST_WORDS):
            times.append(_moment(row.get("completed_at") or row.get("started_at")))
    return [moment for moment in times if moment is not None]


def host_probe(reader, *, checks_granted: bool, now=None) -> dict:
    """Read host evidence for the head and the last five commits on the branch.

    ``reader`` exposes ``commits()``, ``deployments(sha)``, ``statuses(sha)``
    and ``check_runs(sha)``; it raises ``HostProbeUnavailable`` when GitHub
    can't be read, which leaves the host state unknown.
    """
    now = now or timezone.now()
    codes = [] if checks_granted else [CHECKS_PERMISSION_MISSING]
    try:
        commits = [row for row in reader.commits() if _sha(_mapping(row).get("sha"))][:RECENT_COMMITS]
        recent_commits = [row for row in commits
                          if (moment := _moment(row.get("date"))) is not None and now - moment <= HOST_WINDOW]
        deployed = False
        if recent_commits:
            for row in commits:
                if any(now - moment <= HOST_WINDOW for moment in _host_signal_times(reader, row["sha"])):
                    deployed = True
                    break
    except HostProbeUnavailable:
        return {"status": "unknown", "codes": codes, "checked_at": now.isoformat(), "commit_sha": ""}
    if recent_commits and not deployed:
        codes.insert(0, HOST_NOT_DEPLOYING)
    head = commits[0]["sha"] if commits else ""
    return {"status": "failed" if HOST_NOT_DEPLOYING in codes else "passed", "codes": codes,
            "checked_at": now.isoformat(), "commit_sha": head}


class GitHubHostReader:
    """Read-only host evidence through the connection's GitHub App installation."""

    def __init__(self, connection):
        self.connection = connection
        self._token = None

    def _get(self, path):
        from integrations.services.github_app import GitHubAppTokenError
        from .publish_merge_worker import ProviderError, _api, _token
        try:
            if self._token is None:
                self._token = _token(self.connection, checks=True)
            return _api(self.connection, self._token, "GET", path)
        except (GitHubAppTokenError, ProviderError, OSError, ValueError) as exc:
            raise HostProbeUnavailable() from exc

    def commits(self):
        from urllib.parse import quote
        rows = self._get(f"/commits?sha={quote(self.connection.branch or '', safe='')}&per_page={RECENT_COMMITS}")
        return [{"sha": _mapping(row).get("sha"),
                 "date": _mapping(_mapping(_mapping(row).get("commit")).get("committer")).get("date")}
                for row in (rows if isinstance(rows, list) else [])]

    def deployments(self, sha):
        rows = self._get(f"/deployments?sha={sha}&per_page=30")
        return rows if isinstance(rows, list) else []

    def statuses(self, sha):
        return _mapping(self._get(f"/commits/{sha}/status?per_page=100")).get("statuses") or []

    def check_runs(self, sha):
        return _mapping(self._get(f"/commits/{sha}/check-runs?per_page=100")).get("check_runs") or []


def sitemap_probe(site_url: str, *, fetch=None, now=None) -> dict:
    """``/sitemap.xml`` on the live site; unreachable is a warning only."""
    from urllib.parse import urlsplit
    now = now or timezone.now()
    host = (urlsplit(site_url if "://" in (site_url or "") else f"https://{site_url}").hostname or "").lower()
    if not host:
        return {"status": "unknown", "checked_at": now.isoformat()}
    if fetch is None:
        from .website_live_fetch import fetch_live_route
        fetch = fetch_live_route
    from .website_contract import WebsiteAuthorityError
    try:
        body, _headers = fetch(f"https://{host}/sitemap.xml", host)
    except (WebsiteAuthorityError, OSError, ValueError):
        return {"status": "failed", "checked_at": now.isoformat()}
    text = bytes(body or b"")[:4096].decode("utf-8", "replace").lower()
    reachable = "<urlset" in text or "<sitemapindex" in text
    return {"status": "passed" if reachable else "failed", "checked_at": now.isoformat()}


def refresh_host(connection, *, reader=None, checks_granted=None, fetch=None, now=None):
    """Run the host and sitemap probes and store them. Free; no points move."""
    if not enabled():
        return None
    now = now or timezone.now()
    if checks_granted is None:
        from .website_github_permissions import installation_grants
        grants = installation_grants(connection)
        # An outage reading grants is not the founder's missing permission.
        checks_granted = bool(grants.get("checks")) or bool(grants.get("reasonCode"))
    host = host_probe(reader or GitHubHostReader(connection), checks_granted=checks_granted, now=now)
    patch = {"host": host, "sitemap": sitemap_probe(connection.site_url, fetch=fetch, now=now) if connection.site_url else
             {"status": "unknown", "checked_at": now.isoformat()}}
    if host.get("commit_sha"):
        patch["head_sha"] = host["commit_sha"]
    return _store(connection, patch)


# ---------------------------------------------------------------------------
# Push webhook


def build_probe_needed(changed_paths) -> bool:
    """A push re-runs the build probe only when the build's inputs changed."""
    return any(_BUILD_INPUTS.search(str(path)) for path in changed_paths or ())


def on_push(payload, *, reader_for=None, fetch=None, now=None) -> dict:
    """Re-run the host probe for every connected site on the pushed branch.

    Never raises into the webhook: a probe that can't run leaves the last record.
    """
    if not enabled():
        return {}
    from .website_models import WebsiteConnection
    repository = _mapping(payload.get("repository"))
    sha = _sha(str(payload.get("after") or ""))
    if not isinstance(repository.get("id"), int) or not sha:
        return {}
    changed = set()
    for commit in payload.get("commits") or []:
        for key in ("added", "modified", "removed"):
            changed.update(str(path) for path in _mapping(commit).get(key) or [])
    refreshed = stale = 0
    for connection in WebsiteConnection.objects.filter(repository_id=repository["id"], state="connected"):
        if payload.get("ref") != f"refs/heads/{connection.branch}":
            continue
        try:
            record_head(connection, sha)
            if build_probe_needed(changed):
                # The stored build result is for an older head, so the charge
                # gate already treats it as stale; mark it for the founder too.
                _store(connection, {"build_probe_requested_for": sha})
                stale += 1
            refresh_host(connection, reader=reader_for(connection) if reader_for else None, fetch=fetch, now=now)
            refreshed += 1
        except Exception:  # noqa: BLE001 - a health probe never fails a webhook
            continue
    return {"repo_health_refreshed": refreshed, "repo_health_build_stale": stale}


# ---------------------------------------------------------------------------
# Founder projection and the charge gate


def _build_for_head(record, head):
    build = _mapping(record.get("build"))
    return build if head and build.get("commit_sha") == head else {}


def items(connection) -> list[dict]:
    """Every failing item, blocking first, each with one sentence and one action."""
    record = stored(connection)
    head = current_head(connection)
    codes = []
    build = _build_for_head(record, head)
    if build.get("status") == "failed" and build.get("code") in BUILD_CODES:
        codes.append(build["code"])
    codes.extend(code for code in _mapping(record.get("host")).get("codes") or [] if code in COPY)
    if _mapping(record.get("sitemap")).get("status") == "failed":
        codes.append(SITEMAP_UNREACHABLE)
    rows = []
    for code in sorted(set(codes), key=ORDER.index):
        message, action = COPY[code]
        rows.append({"code": code, "severity": "blocking" if code in BLOCKING_CODES else "warning",
                     "message": message, "action": action})
    return rows


def journey_block(connection) -> dict | None:
    """The journey's ``repoHealth`` block, or ``None`` when there is nothing to show."""
    if not enabled() or connection is None:
        return None
    record = stored(connection)
    if not record:
        return None
    head = current_head(connection)
    build = _build_for_head(record, head)
    rows = items(connection)
    return {
        "headSha": head or None,
        "buildStatus": build.get("status") if build.get("status") in {"passed", "failed"} else "not_checked",
        "hostStatus": _mapping(record.get("host")).get("status") if _mapping(record.get("host")).get("status") in
        {"passed", "failed", "unknown"} else "not_checked",
        "blocking": any(row["severity"] == "blocking" for row in rows),
        "items": rows,
        "checkedAt": max((value for value in (build.get("checked_at"), _mapping(record.get("host")).get("checked_at"))
                          if isinstance(value, str)), default=None),
    }


def setup_blocked(connection) -> bool:
    """D1: build and host failures stop setup; warnings never do."""
    return enabled() and any(row["severity"] == "blocking" for row in items(connection))


def require_green_build(connection):
    """Before an article is charged, the current head must have a green build probe."""
    if not enabled():
        return
    from .website_contract import WebsiteAuthorityError
    head = current_head(connection)
    build = _build_for_head(stored(connection), head)
    if build.get("status") != "passed":
        code = build.get("code") if build.get("status") == "failed" else None
        message = COPY[code][0] if code in COPY else "We need to check your site builds on its latest commit first."
        raise WebsiteAuthorityError("repo_health_build_not_green", message, retryable=code is None)
    if any(row["severity"] == "blocking" for row in items(connection)):
        raise WebsiteAuthorityError("repo_health_blocked", COPY[HOST_NOT_DEPLOYING][0])


# ---------------------------------------------------------------------------
# Offer the fix PR (held off: ARTICLES_REPO_HEALTH_FIX_PR_ENABLED)

FIX_BRANCH_PREFIX = "mlai/repo-health-"
_SEMVER = re.compile(r"^(?P<prefix>[\^~]?)(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)$")


class FixNotOffered(ValueError):
    """This failure has no safe automatic fix."""


def patch_bump(package_json: str, package: str, target: str) -> str:
    """``package.json`` with ``package`` moved to ``target``, only within one minor version."""
    data = json.loads(package_json)
    target_match = _SEMVER.match(target or "")
    if not isinstance(data, dict) or not target_match or target_match.group("prefix"):
        raise FixNotOffered("patch_target_invalid")
    for section in ("dependencies", "devDependencies"):
        current = _mapping(data.get(section)).get(package)
        if current is None:
            continue
        match = _SEMVER.match(str(current))
        if (not match or (match["major"], match["minor"]) != (target_match["major"], target_match["minor"])
                or int(target_match["patch"]) <= int(match["patch"])):
            raise FixNotOffered("not_a_patch_bump")
        data[section][package] = match["prefix"] + target
        return json.dumps(data, indent=2) + "\n"
    raise FixNotOffered("package_not_declared")


def open_fix_pull_request(connection, code, *, client, files: dict, base_sha: str):
    """Open a fix PR on the founder's repo as the MLAI GitHub App's bot.

    ``files`` maps paths to their new contents (a regenerated lockfile, or
    ``patch_bump`` output plus its lockfile). ``client`` is the App-authenticated
    repository client: ``create_branch(name, sha)``, ``put_file(branch, path,
    content, message)`` and ``create_pull(head, base, title, body)``. The founder
    merges it. Returns the pull request URL, or ``None`` while held off.
    """
    if not fix_pr_enabled():
        return None
    if code not in {LOCKFILE_OUT_OF_SYNC, FRAMEWORK_BUILD_BLOCKED_BY_ADVISORY}:
        raise FixNotOffered("no_automatic_fix")
    if not _sha(base_sha) or not files or not all(isinstance(path, str) and isinstance(body, str) for path, body in files.items()):
        raise FixNotOffered("fix_inputs_invalid")
    if any(not _BUILD_INPUTS.search(path) or path.startswith("/") or ".." in path.split("/") for path in files):
        raise FixNotOffered("fix_touches_other_files")
    branch = f"{FIX_BRANCH_PREFIX}{code.replace('_', '-')}-{base_sha[:7]}"
    title = ("Update the lockfile so a clean install works" if code == LOCKFILE_OUT_OF_SYNC
             else "Upgrade the framework to its patched release")
    client.create_branch(branch, base_sha)
    for path in sorted(files):
        client.put_file(branch, path, files[path], title)
    body = COPY[code][0] + "\n\nOpened by MLAI so your site builds again. Review and merge it when you're ready."
    return client.create_pull(branch, connection.branch, title, body)
