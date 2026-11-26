"""Website readiness record: one stored answer to "can this website take articles?".

Today "Ready" is recomputed on every read (``activation.durable_integration_evidence``
plus the live GitHub HEAD comparison in ``_article_capabilities_for_context``),
proof is pinned to one commit and expires after seven days, and a founder's push
can send a working site back through a full articles setup. This module keeps
the answer instead: a ``WebsiteReadiness`` row per connection, changed only by
``record`` (from saved facts or the named outcome of a check), with every change
appended to ``WebsiteCheckEvent``.

Vocabulary (shared with Content Factory and MLAI Chat):

- states ``ready``, ``checking``, ``needs_user``, ``fixing``, ``not_set_up``;
- checks ``github_access``, ``repository``, ``write_permission``, ``blog_files``,
  ``build_render``, ``live_page``, each ``passed``/``failed``/``not_run``/``unknown``;
- triggers in ``TRIGGERS``.

``derive`` reads saved state only: never GitHub, Content Factory or the site.
Stage A ships the record in shadow (``ARTICLES_READINESS_SHADOW_ENABLED``, on):
hooks at the write points call ``shadow``, which only logs on failure and never
changes behaviour. ``ARTICLES_READINESS_READS_ENABLED`` (off, for
``ARTICLES_READINESS_DOMAINS``) lets the founder page and the start gate read it.
Publishing safety at write time (authority guards, ``verify_repository_head``,
the reservation re-check) is unchanged.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

logger = logging.getLogger(__name__)

VERSION = 1
STATES = ("ready", "checking", "needs_user", "fixing", "not_set_up")
CHECK_KEYS = ("github_access", "repository", "write_permission", "blog_files", "build_render", "live_page")
CHECK_LABELS = {
    "github_access": "GitHub access",
    "repository": "Website repository",
    "write_permission": "Permission to propose changes",
    "blog_files": "Blog files in place",
    "build_render": "Build and page render",
    "live_page": "Live blog page",
}
CHECK_STATUSES = ("passed", "failed", "not_run", "unknown")
TRIGGERS = ("backfill", "push_no_footprint", "push_footprint", "owned_merge", "access_change", "delivery_failure",
            "manual", "weekly_look", "setup_completed", "verification_recorded", "nightly_compare")
RESULTS = ("passed", "failed", "skipped", "recorded")
DRAFT_STATES = frozenset({"ready", "checking", "fixing"})
FOUNDER_COPY = {
    "ready": ("Ready to write", "Write an article"),
    "checking": ("Ready to write. We're checking a recent change to your site.", "Write an article"),
    "needs_user": ("Something on your website needs you.", "Fix it"),
    "fixing": ("Publishing is paused while we fix something on our side. You can still write drafts.", "Write a draft"),
    "not_set_up": ("Connect your blog page to publish articles.", "Set up articles"),
}
# Reason codes that only the founder can clear, with the one sentence naming the ask.
NEEDS_USER_REASONS = {
    "github_access_required": "GitHub access to your website was removed.",
    "repository_changed": "Your website's repository changed. Choose it again to keep publishing.",
    "CONNECTION_READ_ONLY": "Your website's code is archived on GitHub. Unarchive it to keep publishing.",
    "github_write_required": "Give MLAI permission to propose changes to your website.",
    "website_publishing_paused": "Publishing to your website is paused. Resume it to publish.",
}
FIXING_FAILURE_CLASSES = frozenset({"repairable", "strategy", "unsupported"})
SOURCE_CHANGED = "repository_source_changed"
SETUP_ADAPTER_MODES = ("model_synthesized", "deterministic", "agent", "native")
CHANGED_FILES_LIMIT = 50
EVENT_LIMIT = 50
DOMAIN_FLAGS = {"reads": "ARTICLES_READINESS_READS_ENABLED", "recheck_rules": "ARTICLES_READINESS_RECHECK_RULES_ENABLED",
                "pin": "ARTICLES_READINESS_PIN_ENABLED"}
GLOBAL_FLAGS = {"shadow": ("ARTICLES_READINESS_SHADOW_ENABLED", True),
                "nightly_compare": ("ARTICLES_READINESS_NIGHTLY_COMPARE_ENABLED", True)}
DEFAULT_DOMAINS = "talathrive.com,la-vie.com.au,mlai.au"


def _mapping(value):
    return value if isinstance(value, dict) else {}


def _iso(value):
    return value.isoformat() if value else None


# --- flags -------------------------------------------------------------------

def _domains():
    raw = getattr(settings, "ARTICLES_READINESS_DOMAINS", DEFAULT_DOMAINS)
    raw = raw if isinstance(raw, str) else ""
    return {item.strip().casefold().rstrip(".") for item in raw.split(",") if item.strip()}


def readiness_enabled(kind, organization=None) -> bool:
    """Whether readiness behaviour ``kind`` is on (for ``organization`` when the flag is domain-scoped).

    ``shadow`` and ``nightly_compare`` are fleet-wide; ``reads``, ``recheck_rules``
    and ``pin`` also need the organisation's domain in ``ARTICLES_READINESS_DOMAINS``.
    """
    if kind in GLOBAL_FLAGS:
        name, default = GLOBAL_FLAGS[kind]
        return getattr(settings, name, default) is True
    name = DOMAIN_FLAGS.get(kind)
    if name is None or getattr(settings, name, False) is not True:
        return False
    domain = str(getattr(organization, "domain", "") or "").casefold().rstrip(".")
    return bool(domain) and domain in _domains()


def code_version() -> str:
    """The backend release recorded on check events ("" when unknown)."""
    value = getattr(settings, "APP_RELEASE", "") or ""
    return str(value)[:64]


# --- saved facts -------------------------------------------------------------

def _default_target(config, connection):
    key = str(getattr(config, "default_publish_target_id", "") or "")
    if not key:
        return None
    return connection.targets.filter(generation=connection.generation, target_key=key).first()


def _latest_op(connection, action, **filters):
    return (connection.operations.filter(generation=connection.generation, action=action, **filters)
            .order_by("-created_at", "-pk").first())


def _setup_run(connection, config):
    """The latest completed articles setup run of this generation (saved result only)."""
    from workflow_runs.models import ContentFactoryRun
    cache = _mapping(getattr(config, "article_system_setup_cache", None))
    rows = ContentFactoryRun.objects.filter(organization_id=connection.organization_id, workflow="article_system_setup",
                                            status="completed")
    run_id = str(cache.get("setup_run_id") or "")
    run = rows.filter(run_id=run_id).only("run_id", "result").first() if run_id else None
    if run is None:
        run = (rows.filter(run_request__website_connection_id=str(connection.pk),
                           run_request__connection_generation=connection.generation)
               .order_by("-updated_at", "-pk").only("run_id", "result").first())
    return run


def adapter_mode(result) -> str:
    """The setup run's adapter mode (``model_synthesized``/``deterministic``/...), or ""."""
    result = _mapping(result)
    for source in (result, _mapping(result.get("article_system_setup")), _mapping(result.get("setup_plan")),
                   _mapping(result.get("scaffold_plan"))):
        mode = source.get("adapter_mode")
        if mode in SETUP_ADAPTER_MODES:
            return mode
    return ""


def footprint(config, connection, setup_run_id=""):
    """The files the articles setup owns: ``(paths, source)``.

    From the saved setup cache first (``website_source_carry.setup_paths``), else
    the applied setup mutation's file ledger.
    """
    from .website_source_carry import setup_paths
    paths = setup_paths(getattr(config, "article_system_setup_cache", None))
    if paths:
        return paths, "setup_cache"
    if setup_run_id:
        mutation = (connection.repository_mutations.filter(generation=connection.generation, run_id=setup_run_id,
                                                           status="applied").order_by("-updated_at").first())
        files = [str(item.get("path")) for item in getattr(mutation, "files", None) or []
                 if isinstance(item, dict) and item.get("path")]
        if files:
            return sorted(set(files)), "setup_mutation"
    return [], "derived"


def pinned_contract(config, connection, target):
    """What worked last time: adapter, mode, setup run, route and footprint."""
    run = _setup_run(connection, config)
    setup_run_id = str(getattr(run, "run_id", "") or "")
    paths, source = footprint(config, connection, setup_run_id)
    contract = _mapping(getattr(target, "contract", None))
    route = str(contract.get("route_path") or contract.get("public_path")
                or str(contract.get("route_template") or "").split("{")[0] or "")
    cache = _mapping(getattr(config, "article_system_setup_cache", None))
    route = route or str(cache.get("route_path") or "")
    return {"adapter": str(getattr(target, "adapter", "") or ""), "adapterVersion": str(getattr(target, "adapter_version", "") or ""),
            "adapterMode": adapter_mode(getattr(run, "result", None)), "setupRunId": setup_run_id,
            "targetKey": str(getattr(target, "target_key", "") or ""),
            "routePath": ("/" + route.strip("/")) if route else "", "footprint": paths, "footprintSource": source}


def _open_recheck(connection):
    """The pending source check, or the preparation now re-checking the source; ``{}`` when none."""
    op = (connection.operations.filter(generation=connection.generation, action="source-reverify",
                                       state__in=["pending", "running"]).order_by("-created_at", "-pk").first())
    if op is None:
        prepare = _latest_op(connection, "prepare", state__in=["pending", "running"])
        if prepare is None or not _mapping(prepare.payload).get("source_reverify"):
            return {}
        op = prepare
    payload = _mapping(op.payload)
    reason = "verification_refresh" if payload.get("purpose") == "verification_refresh" else "source_changed"
    trigger = "push_footprint" if reason == "source_changed" else "weekly_look"
    if payload.get("verify_only") and payload.get("trigger") in TRIGGERS:
        # A readiness verify-only check names what queued it (push, owner, weekly look, refused article).
        trigger = payload["trigger"]
        reason = "source_changed" if trigger == "push_footprint" else trigger
    return {"reason": reason, "trigger": trigger,
            "sourceSha": str(payload.get("source_sha") or ""), "queuedAt": _iso(op.created_at), "operationId": str(op.pk)}


def _failed_recheck(connection):
    op = _latest_op(connection, "source-reverify")
    return op is not None and op.state == "needs_attention"


def prepare_outcome(op):
    """Collapse the latest preparation into ``(kind, receipt)``; kind is running|needs_user|fixing|closed|completed|None."""
    if op is None:
        return None, {}
    receipt = _mapping(op.receipt)
    if receipt.get("status") == "cant_be_done_yet":
        return "closed", receipt
    if op.state in {"pending", "running"}:
        return "running", receipt
    if op.state == "completed":
        return "completed", receipt
    if op.state == "needs_user":
        return "needs_user", receipt
    failure = _mapping(receipt.get("failure"))
    klass = failure.get("failure_class") or receipt.get("failure_class")
    if receipt.get("status") == "staff_fixing" or klass in FIXING_FAILURE_CLASSES or op.state in {"needs_attention", "stopped"}:
        return "fixing", receipt
    return None, receipt


def _needs_user_reason(code, receipt=None):
    if code in NEEDS_USER_REASONS:
        return NEEDS_USER_REASONS[code]
    label = str(_mapping(_mapping(receipt).get("user_action")).get("label") or "").strip().rstrip(".")
    return f"{label} to keep publishing." if label else FOUNDER_COPY["needs_user"][0]


def _blocker(connection, code):
    return any(isinstance(item, dict) and item.get("code") == code for item in connection.blockers or [])


def classify(connection, legacy, *, prepare=None, recheck=None, failed_recheck=False):
    """Map saved facts to ``(state, reason_code)`` with no I/O. ``prepare`` is ``prepare_outcome(op)``."""
    kind, receipt = prepare or (None, {})
    code = str(legacy.get("reasonCode") or "")
    if connection.state in {"disconnected", "revoked"}:
        return "not_set_up", "website_disconnected" if connection.state == "disconnected" else "github_access_required"
    if code in NEEDS_USER_REASONS:
        return "needs_user", code
    if _blocker(connection, "github_write_required"):
        return "needs_user", "github_write_required"
    if legacy.get("verified"):
        return ("ready", "") if legacy.get("publishingVerified") else ("checking", "live_check_pending")
    if code == "verification_stale":
        # Age alone is not a failure: the last proof stands (noted on the checks).
        return "ready", ""
    previously_verified = bool(connection.verified_sha and connection.last_verified_at)
    if code == "website_writes_paused":
        return "fixing", code
    if previously_verified and (code == SOURCE_CHANGED or code in {"integration_required", "verification_required"}):
        if recheck or kind == "running":
            return "checking", code
        if kind == "needs_user":
            return "needs_user", str(receipt.get("reason_code") or code)
        if kind in {"fixing", "closed"} or failed_recheck:
            return "fixing", str(receipt.get("reason_code") or code)
        return ("checking", code) if code == SOURCE_CHANGED else ("fixing", code)
    if kind == "needs_user":
        return "needs_user", str(receipt.get("reason_code") or code or "owner_action_required")
    return "not_set_up", code or "integration_required"


# --- checks ------------------------------------------------------------------

def _check(status, *, checked_at=None, source_sha="", detail="", evidence=None):
    return {"status": status, "checkedAt": _iso(checked_at) if hasattr(checked_at, "isoformat") else checked_at,
            "sourceSha": source_sha or "", "codeVersion": code_version(), "detail": detail, "evidence": evidence or {}}


def derive_checks(connection, legacy, target, pinned, *, live_op=None, now=None):
    """The six checks as saved facts show them; nothing here runs a check."""
    now = now or timezone.now()
    from .website_live_verification import build_proof
    code = str(legacy.get("reasonCode") or "")
    sha = connection.verified_sha or ""
    checks = {}
    if connection.state == "revoked" or code == "github_access_required":
        checks["github_access"] = _check("failed", checked_at=connection.updated_at, detail="GitHub access to the website was removed.")
    elif connection.state in {"connected", "paused"} and connection.installation_id:
        checks["github_access"] = _check("passed", checked_at=connection.updated_at, detail="MLAI's GitHub app can read the website.")
    else:
        checks["github_access"] = _check("unknown")
    if code == "repository_changed":
        checks["repository"] = _check("failed", detail="The saved repository is not the connected one.")
    elif connection.repository_id and connection.github_repo:
        checks["repository"] = _check("passed", checked_at=connection.updated_at, detail=f"{connection.github_repo} on {connection.branch or 'its default branch'}.",
                                      evidence={"repo": connection.github_repo, "branch": connection.branch})
    else:
        checks["repository"] = _check("unknown")
    if code == "CONNECTION_READ_ONLY" or _blocker(connection, "github_write_required"):
        checks["write_permission"] = _check("failed", detail="MLAI can't propose changes to the repository.")
    elif sha and connection.last_verified_at:
        checks["write_permission"] = _check("passed", checked_at=connection.last_verified_at, source_sha=sha,
                                            detail="MLAI's setup changes were accepted.")
    else:
        checks["write_permission"] = _check("unknown")
    proven = bool(target is not None and build_proof(target))
    paths = list(pinned.get("footprint") or [])
    if not paths:
        checks["blog_files"] = _check("not_run", detail="No articles setup is recorded yet.")
    elif proven:
        checks["blog_files"] = _check("passed", checked_at=target.verified_at, source_sha=target.source_sha,
                                      detail=f"{len(paths)} blog files recorded by the setup.", evidence={"files": len(paths)})
    else:
        checks["blog_files"] = _check("unknown", detail="The blog files were not checked at the current commit.")
    if proven:
        stale = bool(target.verified_at and now - target.verified_at > timedelta(days=7))
        checks["build_render"] = _check("passed", checked_at=target.verified_at, source_sha=target.source_sha,
            detail="Built and rendered over 7 days ago; age alone is not a failure." if stale else "Built and rendered by MLAI.",
            evidence={"stale": stale})
    elif target is not None and target.source_sha and target.source_sha != sha:
        checks["build_render"] = _check("unknown", source_sha=target.source_sha, detail="The latest commit has not been built yet.")
    else:
        checks["build_render"] = _check("not_run")
    if live_op is not None:
        receipt = _mapping(live_op.receipt)
        checks["live_page"] = _check("passed", checked_at=receipt.get("checked_at") or _iso(live_op.updated_at),
                                     source_sha=str(_mapping(live_op.payload).get("source_sha") or ""),
                                     detail="The live blog page answered.", evidence={"publicUrl": receipt.get("public_url") or ""})
    else:
        checks["live_page"] = _check("not_run", detail="The live blog page was not checked yet.")
    return checks


# --- derive / record ---------------------------------------------------------

def _legacy(config, connection, now):
    from .activation import durable_integration_evidence
    evidence = durable_integration_evidence(config, connection, now=now)
    return {key: evidence.get(key) for key in ("verified", "publishingVerified", "liveVerified", "reasonCode", "routePath",
                                               "verifiedAt", "sha", "branch")}


def legacy_state(legacy) -> str:
    """Today's computed answer in readiness words, for comparison only (``not_ready`` = drafts blocked)."""
    if legacy.get("verified"):
        return "ready" if legacy.get("publishingVerified") else "checking"
    code = legacy.get("reasonCode")
    if code in NEEDS_USER_REASONS:
        return "needs_user"
    return "not_set_up" if code == "website_disconnected" else "not_ready"


def derive(config, connection, *, now=None):
    """The readiness record from saved state only (no GitHub, Content Factory or site calls)."""
    now = now or timezone.now()
    legacy = _legacy(config, connection, now)
    target = _default_target(config, connection)
    prepare = prepare_outcome(_latest_op(connection, "prepare"))
    recheck = _open_recheck(connection)
    state, reason_code = classify(connection, legacy, prepare=prepare, recheck=recheck,
                                  failed_recheck=_failed_recheck(connection))
    pinned = pinned_contract(config, connection, target)
    live_op = (_latest_op(connection, "deployment-verify", state="completed", payload__source_sha=connection.verified_sha)
               if connection.verified_sha else None)
    reason = (_needs_user_reason(reason_code, prepare[1]) if state == "needs_user" else FOUNDER_COPY[state][0])
    return {"generation": connection.generation, "state": state, "can_draft": state in DRAFT_STATES,
            "can_publish": state == "ready", "verified_sha": connection.verified_sha or "",
            "verified_at": connection.last_verified_at, "pinned_contract": pinned,
            "checks": derive_checks(connection, legacy, target, pinned, live_op=live_op, now=now),
            "recheck": recheck if state == "checking" else {}, "reason_code": reason_code, "reason": reason[:300],
            "legacy": {**legacy, "state": legacy_state(legacy)}}


def _connection(connection_or_pk, *, lock=False):
    from .website_models import WebsiteConnection
    pk = getattr(connection_or_pk, "pk", connection_or_pk)
    rows = WebsiteConnection.objects.select_related("organization")
    return (rows.select_for_update() if lock else rows).get(pk=pk)


def _config_for(connection):
    from .models import OrganizationContentConfig
    return OrganizationContentConfig.objects.filter(website_connection=connection).first()


def _next_values(row, derived, *, trigger, state, checks, now, detail=None):
    values = dict(derived)
    if state:
        values.update(state=state, can_draft=state in DRAFT_STATES, can_publish=state == "ready",
                      reason=values["reason"] if state == derived["state"] else FOUNDER_COPY[state][0])
    stored_pin = _mapping(row.pinned_contract)
    repin = trigger == "setup_completed" and _mapping(detail).get("prepare_state", "completed") == "completed"
    if stored_pin.get("setupRunId") and (not repin or not values["pinned_contract"].get("setupRunId")):
        # The pin is what worked last time; only a completed setup replaces it.
        values["pinned_contract"] = stored_pin
    if checks:
        values["checks"] = {**values["checks"], **{key: value for key, value in checks.items() if key in CHECK_KEYS}}
    values["last_known_good_at"] = now if values["state"] == "ready" else row.last_known_good_at
    return values


def record(connection, *, trigger, source_sha="", changed_files=(), checks=None, result="recorded", detail=None,
           state=None, now=None):
    """Recompute and store one connection's readiness, appending one check event. Returns the record dict.

    Locks the connection, creates the readiness row when missing, takes ``state``
    as given (rule paths) or from ``derive``, bumps ``version`` and appends a
    ``WebsiteCheckEvent`` with the state before and after.
    """
    from .website_models import WebsiteCheckEvent, WebsiteReadiness
    if trigger not in TRIGGERS:
        raise ValueError(f"Unknown readiness trigger: {trigger}")
    if result not in RESULTS:
        raise ValueError(f"Unknown readiness result: {result}")
    if state is not None and state not in STATES:
        raise ValueError(f"Unknown readiness state: {state}")
    now = now or timezone.now()
    with transaction.atomic():
        locked = _connection(connection, lock=True)
        config = _config_for(locked)
        row, _ = WebsiteReadiness.objects.select_for_update().get_or_create(
            connection=locked, defaults={"generation": locked.generation})
        before = row.state if row.version else ""
        derived = derive(config, locked, now=now) if config is not None else _unbound(locked)
        for field, value in _next_values(row, derived, trigger=trigger, state=state, checks=checks, now=now,
                                         detail=detail).items():
            setattr(row, field, value)
        row.version += 1
        row.save()
        WebsiteCheckEvent.objects.create(connection=locked, generation=locked.generation, trigger=trigger,
            source_sha=str(source_sha or "")[:64], changed_files=[str(path) for path in list(changed_files or ())[:CHANGED_FILES_LIMIT]],
            checks={key: value for key, value in (checks or {}).items() if key in CHECK_KEYS}, result=result,
            state_before=before, state_after=row.state, code_version=code_version(), detail=_mapping(detail))
    return as_record(row)


def _unbound(connection):
    """A connection no articles config points at any more: not set up."""
    return {"generation": connection.generation, "state": "not_set_up", "can_draft": False, "can_publish": False,
            "verified_sha": connection.verified_sha or "", "verified_at": connection.last_verified_at, "pinned_contract": {},
            "checks": {key: _check("unknown") for key in CHECK_KEYS}, "recheck": {}, "reason_code": "website_disconnected",
            "reason": FOUNDER_COPY["not_set_up"][0], "legacy": {"verified": False, "reasonCode": "website_disconnected", "state": "not_set_up"}}


def as_record(row):
    """The stored row as a plain dict (staff view and tests)."""
    return {"connectionId": str(row.connection_id), "generation": row.generation, "state": row.state,
            "canDraft": row.can_draft, "canPublish": row.can_publish, "verifiedSha": row.verified_sha,
            "verifiedAt": _iso(row.verified_at), "pinnedContract": _mapping(row.pinned_contract), "checks": _mapping(row.checks),
            "recheck": _mapping(row.recheck), "reasonCode": row.reason_code, "reason": row.reason,
            "lastKnownGoodAt": _iso(row.last_known_good_at), "lastLook": _mapping(row.last_look),
            "legacy": _mapping(row.legacy), "version": row.version, "updatedAt": _iso(row.updated_at)}


def shadow(connection, trigger, **kwargs):
    """Flag-guarded ``record`` after the caller's transaction commits; a failure only logs.

    One line at each write point. It never raises and never changes the
    caller's behaviour.
    """
    try:
        if not readiness_enabled("shadow"):
            return
        pk = getattr(connection, "pk", connection)
        if not pk:
            return

        def run():
            try:
                record(pk, trigger=trigger, **kwargs)
            except Exception:  # noqa: BLE001 - shadow bookkeeping never breaks the write it follows
                logger.warning("website_readiness_shadow_failed connection=%s trigger=%s", pk, trigger, exc_info=True)

        transaction.on_commit(run)
    except Exception:  # noqa: BLE001 - see above
        logger.warning("website_readiness_shadow_unavailable trigger=%s", trigger, exc_info=True)


OPERATION_RESULTS = {"completed": "passed", "needs_attention": "failed", "needs_user": "failed", "stopped": "failed"}


def shadow_for_operation(op, trigger, *, state=""):
    """``shadow`` for the connection of a website operation that just settled in ``state``."""
    try:
        payload = _mapping(getattr(op, "payload", None))
        shadow(getattr(op, "connection_id", None), trigger, source_sha=str(payload.get("source_sha") or ""),
               result=OPERATION_RESULTS.get(state, "skipped") if trigger == "verification_recorded" else "recorded",
               detail={"operation": str(getattr(op, "action", "") or ""), "prepare_state": state}
               if trigger == "setup_completed" else {"operation": str(getattr(op, "action", "") or ""), "state": state})
    except Exception:  # noqa: BLE001 - shadow bookkeeping never breaks the operation
        logger.warning("website_readiness_shadow_operation_failed operation=%s", getattr(op, "pk", ""), exc_info=True)


def shadow_for_run(run, trigger, **kwargs):
    """``shadow`` for the connection a Content Factory run was bound to, if any."""
    try:
        identifier = str(_mapping(getattr(run, "run_request", None)).get("website_connection_id") or "")
        if identifier:
            shadow(identifier, trigger, **kwargs)
    except Exception:  # noqa: BLE001 - shadow bookkeeping never breaks the callback
        logger.warning("website_readiness_shadow_run_failed run=%s", getattr(run, "run_id", ""), exc_info=True)


# --- reads -------------------------------------------------------------------

def stored(connection):
    """The connection's readiness row for its current generation, or None (one query)."""
    from .website_models import WebsiteReadiness
    if connection is None:
        return None
    row = WebsiteReadiness.objects.filter(connection_id=connection.pk).first()
    if row is None or row.generation != connection.generation:
        return None
    return row


def projection(readiness, *, site_url="", reads_enabled=False):
    """The compact client payload (``websiteReadiness``, version 1)."""
    checks = _mapping(readiness.checks)
    rows = []
    for key in CHECK_KEYS:
        item = _mapping(checks.get(key))
        status = item.get("status") if item.get("status") in CHECK_STATUSES else "unknown"
        rows.append({"key": key, "label": CHECK_LABELS[key], "status": status, "checkedAt": item.get("checkedAt"),
                     "detail": str(item.get("detail") or "")})
    stamps = sorted(str(row["checkedAt"]) for row in rows if row["checkedAt"])
    route = str(_mapping(readiness.pinned_contract).get("routePath") or "")
    base = str(site_url or "").rstrip("/")
    recheck = _mapping(readiness.recheck)
    return {"version": VERSION, "state": readiness.state, "canDraft": bool(readiness.can_draft),
            "canPublish": bool(readiness.can_publish), "reasonCode": readiness.reason_code, "reason": readiness.reason,
            "blogAddress": (base + route) if base else None, "verifiedSha": readiness.verified_sha or None,
            "lastCheckedAt": stamps[-1] if stamps else _iso(readiness.updated_at), "checks": rows,
            "recheck": {"reason": recheck.get("reason") or "", "since": recheck.get("queuedAt")} if recheck else None,
            "readsEnabled": bool(reads_enabled)}


def founder_payload(config):
    """``websiteReadiness`` for the founder page and bootstrap; None without a stored record. Never raises."""
    try:
        connection = getattr(config, "website_connection", None) if getattr(config, "website_connection_id", None) else None
        row = stored(connection)
        if row is None:
            return None
        return projection(row, site_url=connection.site_url,
                          reads_enabled=readiness_enabled("reads", connection.organization))
    except Exception:  # noqa: BLE001 - the page renders without the record
        logger.warning("website_readiness_payload_failed config=%s", getattr(config, "pk", None), exc_info=True)
        return None


def for_reads(config, organization):
    """The stored record when reads are on for ``organization``, else None."""
    if not readiness_enabled("reads", organization):
        return None
    try:
        connection = getattr(config, "website_connection", None) if getattr(config, "website_connection_id", None) else None
        return stored(connection)
    except Exception:  # noqa: BLE001 - fall back to today's computed answer
        logger.warning("website_readiness_read_failed organization=%s", getattr(organization, "pk", None), exc_info=True)
        return None


STAGES = {"ready": "ready", "checking": "verifying", "fixing": "integration", "not_set_up": "integration"}


def apply_to_capabilities(capabilities, readiness, *, account=None):
    """Answer ``articleCapabilities`` drafting and publishing from the stored record.

    ``canGenerateArticle`` is the record's ``can_draft`` (still requiring the
    founder's own GitHub account, as today); ``canPublishArticle`` is
    ``can_publish``. Write-time publishing guards are unchanged.
    """
    account = account or {}
    can_draft = bool(readiness.can_draft and account.get("owned", True))
    stage = STAGES.get(readiness.state) or ("github" if readiness.reason_code == "github_access_required" else "integration")
    reason = "" if readiness.state == "ready" else readiness.reason
    updated = {**capabilities, "canGenerateArticle": can_draft, "canPublishArticle": bool(readiness.can_publish and can_draft),
               "stage": stage, "reasonCode": readiness.reason_code if readiness.state != "ready" else "", "reason": reason,
               "integrationVerified": readiness.state in DRAFT_STATES, "readinessState": readiness.state}
    if not updated["canPublishArticle"]:
        updated.update(publishingReasonCode=readiness.reason_code or "website_readiness_" + readiness.state,
                       publishingReason=reason or FOUNDER_COPY[readiness.state][0])
    else:
        updated.update(publishingReasonCode="", publishingReason="")
    return updated


# --- staff -------------------------------------------------------------------

def check_events(connection, limit=EVENT_LIMIT):
    """The newest ``limit`` check events of a connection, newest first."""
    rows = connection.check_events.order_by("-created_at", "-pk")[:limit]
    return [{"id": str(row.id), "createdAt": _iso(row.created_at), "generation": row.generation, "trigger": row.trigger,
             "sourceSha": row.source_sha, "changedFiles": list(row.changed_files or []), "checks": _mapping(row.checks),
             "result": row.result, "stateBefore": row.state_before, "stateAfter": row.state_after,
             "codeVersion": row.code_version, "detail": _mapping(row.detail)} for row in rows]


def staff_readiness(connection):
    """The stored record (any generation) for the staff console, or None."""
    from .website_models import WebsiteReadiness
    row = WebsiteReadiness.objects.filter(connection_id=connection.pk).first()
    return {**as_record(row), "currentGeneration": row.generation == connection.generation} if row else None


def readiness_fleet():
    """Connected websites by stored readiness state, in one query (``none`` = no record yet)."""
    from django.db.models import Count
    from .website_models import WebsiteConnection
    counts = {state: 0 for state in (*STATES, "none")}
    rows = (WebsiteConnection.objects.filter(state__in=["connected", "paused"]).values("readiness__state")
            .annotate(total=Count("pk")))
    for row in rows:
        key = row["readiness__state"] if row["readiness__state"] in STATES else "none"
        counts[key] += row["total"]
    return counts
