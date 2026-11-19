"""Sticky stops: a push re-opens a stopped website preparation only when it can change the outcome (plan B3).

Pawshare (13 Nov 2026): every founder push re-opened the stopped preparation
at ``scan``, so each push paid for a repository scan and a setup that failed
the same way (230 "attempts"). The push's own source-reverify operation was
closed as ``managed_by_prepare`` only when preparation re-opened; left alone it
dispatched its own scan, so stopping the re-open alone only moved the cost.

With ``ARTICLES_PREPARE_STICKY_STOP_ENABLED`` (default on):

- every stop records ``stop_v1 = {reason_code, failure_class,
  failure_fingerprint, source_sha, code_version, relevant_paths,
  relevant_routes, at}`` on the preparation receipt;
- a push re-opens a stopped (``needs_attention`` or ``stopped``) preparation
  only when the stop is transient or waiting, the code is a moved-source code,
  or the code depends on the source and one GitHub compare from the stop's
  commit shows a change under its relevant paths or articles route. An owned
  setup merge still re-opens (the caller never asks this module). A stop held
  by the setup or scan budget is staff's, and a push never re-opens it;
- otherwise the preparation stays stopped, its receipt records
  ``source_changed_since_stop`` (so the founder can be offered "Your site
  changed, check again"), and the push's paid source-reverify scan is settled
  without a scan, by the wake and again by the source-reverify worker in case
  it runs first;
- a finished preparation whose push carried the proof forward is left to the
  free carried re-check instead of re-opening;
- automatic repository scans count toward a child-dispatch budget: per
  preparation operation (``OPERATION_SCAN_LIMIT``) and per website per day
  (``DAILY_SCAN_LIMIT``), each past-budget stop raising one deduplicated staff
  alert.

The compare is read-only, runs outside every authority lock and never writes
to the repository. Nothing here loosens consent, spend or publish checks.
"""
from __future__ import annotations

import logging
from datetime import timedelta, timezone as dt_timezone
from typing import Any

from django.conf import settings
from django.db import transaction
from django.utils import timezone

logger = logging.getLogger(__name__)

STOP_KEY = "stop_v1"
CHANGED_KEY = "source_changed_since_stop"
STICKY_STATES = frozenset({"needs_attention", "stopped"})
# A stop in one of these classes waits on something a push can resolve.
REOPEN_CLASSES = frozenset({"transient", "waiting"})
# Stops a push never resolves: the operation is staff's, or no source change
# can make the contract valid.
NOT_SOURCE_DEPENDENT_CODES = frozenset({"website_prepare_contract_invalid", "website_prepare_unavailable",
    "operation_key_conflict", "callback_contract_missing"})
# Files whose change can alter any build, whatever the articles route.
BUILD_MANIFESTS = frozenset({"package.json", "package-lock.json", "pnpm-lock.yaml", "yarn.lock", "bun.lock", "bun.lockb",
    "next.config.js", "next.config.mjs", "next.config.ts", "vite.config.js", "vite.config.ts", "vite.config.mjs",
    "astro.config.mjs", "astro.config.ts", "svelte.config.js", "nuxt.config.ts", "gatsby-config.js", "tsconfig.json",
    "Gemfile", "Gemfile.lock", "hugo.toml", "config.toml", "_config.yml", "requirements.txt", "pyproject.toml"})
MAX_PATHS = 40
MAX_ROUTES = 10

# The child-dispatch budget: automatic repository scans per preparation
# operation and per website per day. Setup runs keep their own cap
# (website_prepare_setup_cap) and count toward neither.
SCAN_RECEIPT_KEY = "scan_dispatch_run_ids"
OPERATION_SCAN_LIMIT = 6
DAILY_SCAN_LIMIT = 6
BUDGET_HELD_KEY = "child_dispatch_budget"
BUDGET_CODE = "prepare_no_progress"


def enabled() -> bool:
    """``ARTICLES_PREPARE_STICKY_STOP_ENABLED``, default on."""
    return bool(getattr(settings, "ARTICLES_PREPARE_STICKY_STOP_ENABLED", True))


def _mapping(value) -> dict:
    return value if isinstance(value, dict) else {}


def _sha(value) -> str:
    from .website_contract import SHA_PATTERN
    text = str(value or "")
    return text if SHA_PATTERN.fullmatch(text) else ""


def _route(value) -> str:
    text = str(value or "").strip().strip("/")
    return text.split("?", 1)[0][:200]


# --- Recording a stop --------------------------------------------------------

def _stop_source(op) -> str:
    """The commit preparation was working on when it stopped (best known)."""
    from .website_models import WebsiteScanSnapshot
    goal_source = _sha(_mapping(_mapping(op.receipt).get("goal")).get("current_source_sha"))
    if goal_source:
        return goal_source
    snapshot = (WebsiteScanSnapshot.objects.filter(connection_id=op.connection_id, generation=op.generation)
                .order_by("-created_at").values_list("source_sha", flat=True).first())
    return (_sha(snapshot) or _sha(_mapping(getattr(op.connection, "capabilities", None)).get("repositorySourceSha"))
            or _sha(getattr(op.connection, "verified_sha", "")))


def _relevant(op) -> tuple[list[str], list[str]]:
    """Files and articles routes whose change can alter this stop's outcome."""
    from .models import OrganizationContentConfig
    from .website_source_carry import setup_paths
    config = OrganizationContentConfig.objects.filter(website_connection_id=op.connection_id).only(
        "article_system_setup_cache", "article_system").first()
    paths = setup_paths(getattr(config, "article_system_setup_cache", None)) if config is not None else []
    receipt = _mapping(op.receipt)
    for finding in (_mapping(receipt.get("failure")).get("findings") or [])[:MAX_PATHS]:
        path = str(_mapping(finding).get("path") or _mapping(finding).get("file") or "").strip().strip("/")
        if path and path not in paths:
            paths.append(path)
    routes: list[str] = []
    system = _mapping(getattr(config, "article_system", None)) if config is not None else {}
    candidates = [_mapping(system.get("pending_article_system_setup")).get("route_path"), system.get("route_path"),
                  _mapping(receipt.get("article_page_choice")).get("route"),
                  *(_mapping(receipt.get("user_action")).get("candidates") or [])]
    for value in candidates:
        route = _route(value)
        if route and route not in routes:
            routes.append(route)
    return paths[:MAX_PATHS], routes[:MAX_ROUTES]


def record_stop(op, *, now=None) -> None:
    """Stamp ``stop_v1`` on a preparation that just stopped; never raises.

    A stop for the same code on the same attempt keeps its first stamp, so a
    later replayed save does not move the stop's commit.
    """
    receipt = _mapping(op.receipt)
    if op.state not in STICKY_STATES:
        return
    code = str(receipt.get("reason_code") or _mapping(receipt.get("failure")).get("code") or "")
    attempt = op.payload.get("attempt", 1) if isinstance(op.payload, dict) else 1
    previous = _mapping(receipt.get(STOP_KEY))
    if previous.get("reason_code") == code and previous.get("attempt") == attempt:
        return
    try:
        paths, routes = _relevant(op)
        source = _stop_source(op)
    except Exception:  # noqa: BLE001 - a missing stamp only means a push is judged without it
        logger.exception("website_prepare sticky_stop_record_failed operation=%s", getattr(op, "pk", None))
        paths, routes, source = [], [], ""
    op.receipt[STOP_KEY] = {"version": 1, "reason_code": code, "failure_class": str(receipt.get("failure_class") or ""),
        "failure_fingerprint": str(receipt.get("failure_fingerprint") or ""), "source_sha": source,
        "code_version": str(getattr(settings, "APP_RELEASE", "") or "unknown")[:40], "relevant_paths": paths,
        "relevant_routes": routes, "attempt": attempt, "held": _held(receipt),
        "at": (now or timezone.now()).isoformat()}


def budget_hold_active(receipt, *, now=None) -> bool:
    """Whether the scan budget still holds this receipt.

    An operation-budget hold waits for staff. A daily hold lapses one day
    after it was taken, so a quiet day never leaves the site stopped for good.
    """
    held = _mapping(_mapping(receipt).get(BUDGET_HELD_KEY))
    if not held:
        return False
    if held.get("scope") != "day":
        return True
    from django.utils.dateparse import parse_datetime
    try:
        held_at = parse_datetime(str(held.get("held_at") or ""))
    except (TypeError, ValueError):
        held_at = None
    if held_at is None:
        return True
    if timezone.is_naive(held_at):
        held_at = timezone.make_aware(held_at, dt_timezone.utc)
    return (now or timezone.now()) - held_at < timedelta(days=1)


def _held(receipt, *, now=None) -> bool:
    from . import website_prepare_setup_cap as setup_cap
    return isinstance(receipt.get(setup_cap.HELD_KEY), dict) or budget_hold_active(receipt, now=now)


def reset_scan_budget(op) -> None:
    """Forget this operation's scan count once preparation finished (or the owner restarts it).

    The operation budget bounds a loop that never makes progress; a website
    that reached Ready and later re-checks after real pushes starts again.
    """
    receipt = op.receipt if isinstance(op.receipt, dict) else {}
    for key in (SCAN_RECEIPT_KEY, "scan_dispatches", BUDGET_HELD_KEY):
        receipt.pop(key, None)


# --- Judging a push ----------------------------------------------------------

def _touches(changed: dict[str, str], paths: list[str], routes: list[str]) -> list[str]:
    hits = []
    protected = set(paths)
    for filename, previous in changed.items():
        names = [name for name in (filename, previous) if name]
        hit = False
        for name in names:
            parts = name.split("/")
            if name in protected or parts[-1] in BUILD_MANIFESTS:
                hit = True
            for route in routes:
                segments = route.split("/")
                # A route ``blog`` is served from ``.../blog/...``, ``.../blog.tsx``
                # or ``.../(group)/blog/...``: match its segments in order.
                stems = [part.split(".", 1)[0] for part in parts]
                for start in range(len(stems)):
                    if stems[start:start + len(segments)] == segments:
                        hit = True
                        break
        if hit:
            hits.append(filename)
    return sorted(hits)


def judge(stop: dict, comparison: dict[str, Any] | None) -> dict[str, Any]:
    """Pure decision for a source-dependent stop from a GitHub compare (None when unreadable)."""
    if comparison is None:
        # No proof the change is relevant: the stop stands, and the owner's
        # Try again still restarts from a fresh scan.
        return {"reopen": False, "reason": "compare_unreadable"}
    from .website_source_carry import COMPARE_FILE_LIMIT, _normalize
    if comparison.get("status") not in {"ahead", "identical"}:
        return {"reopen": True, "reason": "not_fast_forward"}
    files = comparison.get("files")
    if not isinstance(files, list):
        return {"reopen": False, "reason": "compare_unreadable"}
    if len(files) >= COMPARE_FILE_LIMIT:
        return {"reopen": True, "reason": "compare_truncated"}
    changed: dict[str, str] = {}
    for item in files:
        name = _normalize(_mapping(item).get("filename"))
        if name:
            changed[name] = _normalize(item.get("previous_filename")) if item.get("status") == "renamed" else ""
    hits = _touches(changed, list(stop.get("relevant_paths") or []), list(stop.get("relevant_routes") or []))
    if hits:
        return {"reopen": True, "reason": "relevant_change", "changed_relevant": hits[:20]}
    return {"reopen": False, "reason": "no_relevant_change", "changed_files": len(changed)}


def decide(op, connection, source_sha: str) -> dict[str, Any]:
    """Whether a push to ``source_sha`` may re-open stopped ``op``. Reads GitHub at most once; call unlocked."""
    from .website_prepare import SOURCE_CHANGED_CODES
    receipt = _mapping(op.receipt)
    stop = _mapping(receipt.get(STOP_KEY))
    code = str(stop.get("reason_code") or receipt.get("reason_code") or "")
    failure_class = str(stop.get("failure_class") or receipt.get("failure_class") or "")
    if _held(receipt):
        return {"reopen": False, "reason": "budget_held"}
    if failure_class in REOPEN_CLASSES or code in SOURCE_CHANGED_CODES:
        return {"reopen": True, "reason": "waiting_stop"}
    if failure_class == "unsupported" or code in NOT_SOURCE_DEPENDENT_CODES:
        return {"reopen": False, "reason": "not_source_dependent"}
    cached = _mapping(receipt.get(CHANGED_KEY))
    if source_sha and cached.get("source_sha") == source_sha and "reopen" in cached:
        return {"reopen": bool(cached["reopen"]), "reason": str(cached.get("reason") or "cached")}
    from_sha = _sha(stop.get("source_sha"))
    to_sha = _sha(source_sha)
    if not from_sha or not to_sha:
        return {"reopen": False, "reason": "no_stop_source"}
    if from_sha == to_sha:
        return {"reopen": False, "reason": "same_source"}
    from .website_source_carry import compare_sources
    return judge(stop, compare_sources(connection, from_sha, to_sha))


def _note_change(op, source_sha: str, decision: dict, *, now) -> None:
    """Record ``source_changed_since_stop`` without moving ``updated_at`` (the stop's age stays honest)."""
    from .website_models import WebsiteConnectionOperation
    with transaction.atomic():
        current = WebsiteConnectionOperation.objects.select_for_update().filter(pk=op.pk).first()
        if current is None or current.state != op.state:
            return
        receipt = dict(_mapping(current.receipt))
        receipt[CHANGED_KEY] = {"source_sha": source_sha, "reopen": bool(decision.get("reopen")),
            "reason": str(decision.get("reason") or ""), "at": now.isoformat(),
            **({"changed_relevant": decision["changed_relevant"]} if decision.get("changed_relevant") else {})}
        WebsiteConnectionOperation.objects.filter(pk=current.pk).update(receipt=receipt)
        op.receipt = receipt


def settle_source_scans(connection, op, *, source_sha="", now=None) -> int:
    """Settle this generation's queued paid source rescans without a scan; free re-checks are left alone."""
    from .website_models import WebsiteConnectionOperation
    now = now or timezone.now()
    rows = WebsiteConnectionOperation.objects.filter(connection_id=connection.pk, generation=connection.generation,
        action="source-reverify", state="pending", payload__scan_required=True).exclude(receipt__has_key="scan_run_id")
    if source_sha:
        rows = rows.filter(payload__source_sha=source_sha)
    settled = 0
    for row in rows:
        settled += WebsiteConnectionOperation.objects.filter(pk=row.pk, state="pending").exclude(
            receipt__has_key="scan_run_id").update(state="completed", receipt={**_mapping(row.receipt),
                **held_receipt(op)}, updated_at=now)
    return settled


def held_receipt(op) -> dict:
    return {"status": "held_by_stopped_prepare", "prepare_operation_id": str(op.pk), "scan_skipped": True,
            "repository_modified": False, "retryable": False}


def hold_push(connection, latest, *, source_sha="", carried=False, now=None) -> bool:
    """Whether this push leaves ``latest`` as it is (True) instead of re-opening it.

    Called by ``wake_prepare_for_source`` for a push that is not an owned
    setup merge, outside every lock. On a hold the push's paid source rescan
    is settled without a scan.
    """
    if not enabled() or latest is None:
        return False
    now = now or timezone.now()
    if latest.state == "completed":
        # The carried proof is re-checked for free by the push's own source
        # check; re-opening a finished preparation would read the site again.
        return bool(carried)
    if latest.state not in STICKY_STATES:
        return False
    decision = decide(latest, connection, source_sha)
    if source_sha:
        _note_change(latest, source_sha, decision, now=now)
    if decision["reopen"]:
        logger.info("website_prepare sticky_stop_reopen operation=%s reason=%s", latest.pk, decision["reason"])
        return False
    settled = settle_source_scans(connection, latest, source_sha=source_sha, now=now)
    logger.info("website_prepare sticky_stop_held operation=%s reason=%s settled_scans=%s",
        latest.pk, decision["reason"], settled)
    return True


class ScanHeld(Exception):
    """A source rescan settled without dispatching a scan."""

    def __init__(self, state: str, receipt: dict):
        super().__init__(state)
        self.state = state
        self.receipt = receipt


def check_source_scan(website, op, *, now) -> None:
    """Raise ``ScanHeld`` instead of letting a source-reverify operation dispatch a paid scan it must not.

    The same rule as the push wake (which may not have run yet), then the
    per-website daily scan budget. An owned setup merge never reaches here
    with a scan to hold: the caller checks ``owned_merge_run_id`` first.
    """
    if not enabled() or op.payload.get("owned_merge_run_id"):
        return
    try:
        latest = (website.operations.filter(generation=website.generation, action="prepare")
                  .order_by("-created_at", "-pk").first())
    except Exception:  # noqa: BLE001 - no readable preparation means no stop to hold for
        logger.warning("website_prepare sticky_stop_latest_unavailable connection=%s", getattr(website, "pk", None))
        latest = None
    if latest is not None and getattr(latest, "state", None) in STICKY_STATES:
        source_sha = str(op.payload.get("source_sha") or "")
        decision = decide(latest, website, source_sha)
        if source_sha:
            _note_change(latest, source_sha, decision, now=now)
        if not decision["reopen"]:
            raise ScanHeld("completed", held_receipt(latest))
    count = daily_scans(website, now=now)
    if count >= DAILY_SCAN_LIMIT:
        queue_budget_alert(website, op, scope="day", count=count, limit=DAILY_SCAN_LIMIT, now=now)
        raise ScanHeld("needs_attention", {"status": "needs_attention", "code": BUDGET_CODE, "retryable": False,
            "scan_skipped": True, "repository_modified": False,
            BUDGET_HELD_KEY: {"scope": "day", "count": count, "limit": DAILY_SCAN_LIMIT, "held_at": now.isoformat()},
            "message": "We paused re-checking this website for today and told the MLAI team."})


# --- The child-dispatch budget -----------------------------------------------

def scans(op) -> list[str]:
    values = _mapping(op.receipt).get(SCAN_RECEIPT_KEY)
    return [str(value) for value in values if value] if isinstance(values, list) else []


def record_scan(op, run_id) -> None:
    """Count one automatic scan this operation dispatched; a replayed run counts once."""
    run_id = str(run_id or "")
    runs = scans(op)
    if run_id and run_id not in runs:
        op.receipt[SCAN_RECEIPT_KEY] = [*runs, run_id][-50:]
        op.receipt["scan_dispatches"] = int(_mapping(op.receipt).get("scan_dispatches") or len(runs)) + 1


def daily_scans(connection, *, now) -> int:
    """Repository scans of this website's organisation dispatched in the last 24 hours."""
    from workflow_runs.models import ContentFactoryRun
    try:
        count = ContentFactoryRun.objects.filter(organization_id=connection.organization_id, workflow="repo_scan",
            created_at__gte=now - timedelta(days=1)).count()
    except Exception:  # noqa: BLE001 - an unreadable count never blocks work on its own
        logger.warning("website_prepare daily_scan_count_unavailable connection=%s", getattr(connection, "pk", None))
        return 0
    return count if type(count) is int else 0


def scan_budget_reached(op, *, now) -> tuple[str, int, int] | None:
    """``(scope, count, limit)`` when dispatching one more scan for ``op`` would exceed the budget."""
    if not enabled():
        return None
    count = int(_mapping(op.receipt).get("scan_dispatches") or len(scans(op)))
    if count >= OPERATION_SCAN_LIMIT:
        return "operation", count, OPERATION_SCAN_LIMIT
    daily = daily_scans(op.connection, now=now)
    if daily >= DAILY_SCAN_LIMIT:
        return "day", daily, DAILY_SCAN_LIMIT
    return None


def hold_for_budget(op, budget, *, now) -> None:
    """Wait for staff instead of dispatching another scan (the alert is queued once the stop is saved)."""
    scope, count, limit = budget
    op.state, op.next_attempt_at = "needs_attention", None
    for key in ("failure", "failure_fingerprint", "failure_occurrence_id", "failure_taxonomy_observation",
                "failure_taxonomy_policy_applied", "failure_class", "child_resume_pending"):
        op.receipt.pop(key, None)
    op.receipt.update(status="needs_attention", reason_code=BUDGET_CODE, retryable=False, current_step="scan",
        blocked_step="scan", user_action=None,
        message="We stopped checking your website for now and told the MLAI team. They will pick it up from here.",
        **{BUDGET_HELD_KEY: {"scope": scope, "count": count, "limit": limit, "held_at": now.isoformat()}})
    logger.warning("website_prepare child_dispatch_budget operation=%s connection=%s scope=%s count=%s limit=%s",
        op.pk, op.connection_id, scope, count, limit)


def queue_budget_alert(connection, op, *, scope, count, limit, now) -> int:
    """One staff alert per website, scope and day (or per operation); never raises."""
    try:
        from .founder_stuck_alerts import queue_budget_alert as queue
        return queue(connection, op, scope=scope, count=count, limit=limit, now=now)
    except Exception:  # noqa: BLE001 - the stuck sweep reports the stop later
        logger.exception("website_prepare child_dispatch_budget_alert_failed operation=%s", getattr(op, "pk", None))
        return 0
