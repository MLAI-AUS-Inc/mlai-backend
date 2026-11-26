"""Readiness recheck rules: keep a working website Ready across a founder's push.

Stage B of the website readiness record (``website_readiness``). With
``ARTICLES_READINESS_RECHECK_RULES_ENABLED`` on for an organisation (and its
domain in ``ARTICLES_READINESS_DOMAINS``), a push to a website whose stored
record is ``ready`` or ``checking`` no longer reopens preparation:

- **No footprint file changed** (one GitHub compare, fast-forward, readable):
  the verified commit and the default target's source move forward, as
  ``website_source_carry.apply_carry`` does, even when the proof is not MLAI's
  own build proof. Sealed-CI targets keep the old push-time lock. No blocker,
  no dropped ``publishingReady``, no preparation wake. Event ``push_no_footprint``.
- **Footprint touched** (or the compare is unreadable / not a fast-forward):
  the record moves to ``checking`` (drafts allowed, publishing paused) and one
  ``source-reverify`` operation with ``verify_only`` is queued. Preparation is
  not woken, so no full articles setup runs. Event ``push_footprint``.

A verify-only check (:func:`process_verify_only`) runs ``blog_files`` (one git
tree read at the new commit), ``live_page`` (one pinned fetch of the blog
address) and ``build_render`` (Content Factory's
``POST /api/runs/{setupRunId}/verify-installed``, ``not_run`` when that
answer is unavailable). Blog files and a build passing (or, for a stack Content
Factory cannot build in place, blog files and the live page) move the verified commit
forward and clears ``repository_source_changed``; a failed check moves the
record to ``fixing`` (``needs_user`` for access) with drafts still allowed and
never dispatches a setup.

The same verify-only check is queued by the owner's ``readiness-check``
action, by the weekly look (:func:`run_weekly_look_sweep`, two failed looks in
a row) and by an article start refused for a changed source
(:func:`queue_after_delivery_failure`) or reported failed by Content Factory
(:func:`queue_after_run_failure`). Age alone is never a failure
(:func:`age_waived`). :func:`setup_pin` sends the pinned setup to Content
Factory under ``ARTICLES_READINESS_PIN_ENABLED``, and
:func:`run_nightly_compare_sweep` reports, never changes, records whose saved
state differs from what saved facts derive today.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.utils import timezone

logger = logging.getLogger(__name__)

# The switches these rules read (through ``website_readiness.readiness_enabled``).
RECHECK_RULES_FLAG = "ARTICLES_READINESS_RECHECK_RULES_ENABLED"
PIN_FLAG = "ARTICLES_READINESS_PIN_ENABLED"
NIGHTLY_COMPARE_FLAG = "ARTICLES_READINESS_NIGHTLY_COMPARE_ENABLED"
COMPARE_ALERTS_PER_DAY = 10
HELD_STATES = frozenset({"ready", "checking"})
# A fixing website keeps the push rules (a push queues a verify-only check and
# never reopens preparation); only ready/checking keep publishing and the age waiver.
RULE_STATES = HELD_STATES | {"fixing"}
SOURCE_CHANGED = "repository_source_changed"
VERIFY_ONLY_TRIGGERS = frozenset({"push_footprint", "manual", "weekly_look", "delivery_failure"})
ACCESS_CODES = frozenset({"github_access_required", "github_installation_revoked", "github_repository_inaccessible",
                          "website_repository_changed", "CONNECTION_READ_ONLY", "github_write_required",
                          "website_disconnected"})
TRANSIENT_LIVE_CODES = frozenset({"deployment_route_transient"})
VERIFY_ONLY_MAX_ATTEMPTS = 6
LOOK_INTERVAL = timedelta(days=7)
LOOK_FAILURES_BEFORE_RECHECK = 2
SWEEP_LIMIT = 50
LOOK_THROTTLE_KEY = "website_readiness_weekly_look"
LOOK_THROTTLE_SECONDS = 3600
COMPARE_DAY_KEY = "website_readiness_nightly_compare:{day}"
COMPARE_DAY_TTL = 26 * 3600
MISSING_LIMIT = 20
CF_VERIFY_TIMEOUT = (5, 180)
# Content Factory's ``build_render`` ``evidence.reason`` values meaning "this stack is
# not built in place by verify-installed" (setup_verify_installed.check_build_render),
# as opposed to a transient failure.
UNSUPPORTED_BUILD_REASONS = frozenset({"no_static_materialization_contract", "witness_not_required"})
UNSUPPORTED_BUILD_DETAIL = ("This site's build can't be checked in place, so we checked the blog files "
                            "and the live blog page instead.")
STRATEGY_FOR_MODE = {"model_synthesized": "model_adapter", "agent": "agent", "native": "native_collection",
                     "deterministic": "deterministic"}


def _mapping(value):
    return value if isinstance(value, dict) else {}


# --- gates -------------------------------------------------------------------

def enabled_for(connection) -> bool:
    """Whether the recheck rules apply to this connection's organisation. Never raises."""
    try:
        from .website_readiness import readiness_enabled
        return readiness_enabled("recheck_rules", getattr(connection, "organization", None))
    except Exception:  # noqa: BLE001 - a broken gate falls back to today's behaviour
        logger.warning("website_readiness_rules_gate_failed connection=%s", getattr(connection, "pk", ""), exc_info=True)
        return False


def age_waived(connection) -> bool:
    """Whether proof age alone must not make this website stale (rules on, record held; the weekly look re-checks it)."""
    return connection is not None and getattr(connection, "pk", None) is not None and held_record(connection) is not None


def held_record(connection):
    """The stored readiness row when the rules hold this website (ready/checking with a pin), else None."""
    row = rules_record(connection)
    return row if row is not None and row.state in HELD_STATES else None


def rules_record(connection):
    """The stored readiness row when the push rules apply (ready/checking/fixing with a pin), else None."""
    if not enabled_for(connection):
        return None
    from .website_readiness import stored
    try:
        row = stored(connection)
    except Exception:  # noqa: BLE001 - no record means today's behaviour
        logger.warning("website_readiness_rules_record_failed connection=%s", connection.pk, exc_info=True)
        return None
    if row is None or row.state not in RULE_STATES:
        return None
    pin = _mapping(row.pinned_contract)
    return row if pin.get("setupRunId") or pin.get("footprint") else None


def holds_prepare(connection) -> bool:
    """Whether a source change must not reopen preparation (full setup only via explicit actions).

    Sealed-CI targets keep today's behaviour (preparation re-verifies them),
    matching :func:`push_plan`.
    """
    return rules_record(connection) is not None and not sealed_for(connection)


def sealed_for(connection) -> bool:
    """Whether the connection's default target needs sealed CI proof; True when it cannot be told (today's path)."""
    try:
        from .models import OrganizationContentConfig
        from .website_sealed_workflow import required
        config = OrganizationContentConfig.objects.filter(website_connection_id=connection.pk).first()
        target = _default_target(connection, config) if config is not None else None
        return target is None or bool(required(connection, target))
    except Exception:  # noqa: BLE001 - unknown means today's behaviour
        logger.warning("website_readiness_rules_sealed_check_failed connection=%s", getattr(connection, "pk", ""), exc_info=True)
        return True


def is_verify_only(op) -> bool:
    """Whether ``op`` is a readiness verify-only source check."""
    return bool(_mapping(getattr(op, "payload", None)).get("verify_only"))


# --- push rule ---------------------------------------------------------------

def footprint_for(row, config) -> list[str]:
    """The pinned footprint, else the saved setup cache's paths."""
    from .website_source_carry import setup_paths
    pinned = [str(path) for path in _mapping(getattr(row, "pinned_contract", None)).get("footprint") or [] if path]
    return pinned or setup_paths(getattr(config, "article_system_setup_cache", None))


def _default_target(website, config):
    key = str(getattr(config, "default_publish_target_id", "") or "")
    return website.targets.filter(generation=website.generation, target_key=key).first() if key else None


def footprint_decision(footprint, comparison) -> dict:
    """Pure: ``{"rule": "no_footprint"|"footprint", "reason", "changed"}`` from a GitHub compare (or None)."""
    from .website_source_carry import decide
    if comparison is None:
        return {"rule": "footprint", "reason": "compare_unreadable", "changed": []}
    decision = decide({"managed_files": list(footprint)}, comparison, owned_merge=False)
    if decision.get("carry"):
        return {"rule": "no_footprint", "reason": decision.get("reason", ""), "changed": []}
    return {"rule": "footprint", "reason": decision.get("reason", ""), "changed": list(decision.get("changed_setup_files") or [])}


def push_plan(website, config, source_sha, *, owned_merge=False):
    """Decide a push outside the authority locks; None leaves it to today's push handling.

    Owned merges, websites the rules do not hold, websites with no verified
    commit or footprint, and sealed-CI targets keep the existing behaviour.
    """
    from .website_contract import SHA_PATTERN
    if owned_merge:
        return None
    row = rules_record(website)
    from_sha = str(website.verified_sha or "")
    if row is None or not SHA_PATTERN.fullmatch(from_sha) or not SHA_PATTERN.fullmatch(str(source_sha or "")):
        return None
    target = _default_target(website, config)
    from .website_sealed_workflow import required as sealed_required
    if target is None or sealed_required(website, target):
        return None
    footprint = footprint_for(row, config)
    if not footprint:
        return None
    if row.state == "fixing":
        # Publishing is already paused: any push is checked, nothing is carried.
        return {"rule": "footprint", "reason": "record_fixing", "changed": [], "from_sha": from_sha,
                "footprint": footprint, "target_key": target.target_key}
    from .website_source_carry import compare_sources
    comparison = compare_sources(website, from_sha, source_sha, setup_files=footprint)
    return {**footprint_decision(footprint, comparison), "from_sha": from_sha, "footprint": footprint,
            "target_key": target.target_key}


def _move_target(target, source_sha, from_sha, *, key):
    contract = dict(target.contract or {})
    contract["verification"] = {**dict(contract.get("verification") or {}), "source_sha": source_sha, key: from_sha}
    target.source_sha = source_sha
    target.contract = contract
    target.capabilities = {**(target.capabilities or {}), "proofCarriedFrom": from_sha}


def apply_push(website, config, plan, source_sha, *, previous_revision):
    """Apply ``plan`` inside the push handler's lock; returns the readiness trigger used.

    The caller saves ``website`` (``configuration_version``, ``capabilities``,
    ``blockers``, ``verified_sha``) afterwards and then calls :func:`record_push`.
    """
    target = _default_target(website, config)
    carry = (plan.get("rule") == "no_footprint" and website.verified_sha == plan.get("from_sha")
             and target is not None and target.source_sha == plan.get("from_sha"))
    if carry:
        had_lock = any(item.get("code") == SOURCE_CHANGED for item in website.blockers or [])
        _move_target(target, source_sha, plan["from_sha"], key="carried_from")
        target.save(update_fields=["source_sha", "contract", "capabilities", "updated_at"])
        website.verified_sha = source_sha
        website.blockers = [item for item in website.blockers or [] if item.get("code") != SOURCE_CHANGED]
        if had_lock:
            website.capabilities = {**(website.capabilities or {}), "publishingReady": True}
        return "push_no_footprint"
    website.capabilities = {**(website.capabilities or {}), "publishingReady": False, "previewSupported": False}
    website.blockers = [item for item in website.blockers or [] if item.get("code") != SOURCE_CHANGED] + [{
        "code": SOURCE_CHANGED, "source_sha": source_sha,
        "message": "The repository changed. We're checking the blog files at the new commit before publishing."}]
    _queue(website, source_sha, trigger="push_footprint", key=f"{website.pk}:source-reverify:{website.generation}:{source_sha}",
           extra={"previous_configuration_revision": previous_revision, "configuration_revision": website.configuration_version,
                  "changed_files": list(plan.get("changed") or [])[:MISSING_LIMIT], "footprint": list(plan.get("footprint") or []),
                  "from_sha": plan.get("from_sha", ""), "target_id": getattr(config, "default_publish_target_id", "") or ""},
           coalesce=True)
    return "push_footprint"


def record_push(website, trigger, plan, source_sha):
    """Store the rule's readiness outcome in the caller's transaction; a failure only logs."""
    from .website_readiness import record
    try:
        with transaction.atomic():
            record(website, trigger=trigger, source_sha=source_sha, changed_files=plan.get("changed") or (),
                   state="checking" if trigger == "push_footprint" else None,
                   detail={"rule": plan.get("rule"), "reason": plan.get("reason", ""), "from_sha": plan.get("from_sha", "")})
    except Exception:  # noqa: BLE001 - the push itself is already consistent
        logger.warning("website_readiness_push_record_failed connection=%s", website.pk, exc_info=True)


# --- queueing verify-only checks -------------------------------------------------

def _queue(website, source_sha, *, trigger, key, extra=None, coalesce=False):
    """Create (or return) the verify-only ``source-reverify`` operation for ``source_sha``."""
    from .website_models import WebsiteConnectionOperation
    existing = (website.operations.filter(generation=website.generation, action="source-reverify",
                                          state__in=["pending", "running"], payload__source_sha=source_sha,
                                          payload__verify_only=True).order_by("-created_at").first())
    if existing is not None:
        return existing, False
    payload = {"source_sha": source_sha, "verify_only": True, "trigger": trigger, "scan_required": False,
               "target_id": "", "owned_merge_run_id": "", "carried_from": ""}
    if trigger != "push_footprint":
        # Not a push: never superseded by source-sync coalescing, never a refresh.
        payload["purpose"] = "readiness_check"
    payload.update(extra or {})
    op, created = WebsiteConnectionOperation.objects.get_or_create(idempotency_key=key, defaults={
        "connection": website, "generation": website.generation, "action": "source-reverify", "payload": payload,
        "receipt": {"status": "verification_queued", "repository_modified": False, "verify_only": True}})
    if created and coalesce:
        from .website_source_sync import coalesce as coalesce_checks
        coalesce_checks(website, op)
    return op, created


def _check_sha(website, row):
    recheck = _mapping(getattr(row, "recheck", None))
    return str(recheck.get("sourceSha") or website.verified_sha or "")


def queue_verify_only(website, *, trigger, source_sha="", now=None, bucket=""):
    """Queue one verify-only check for ``trigger`` (idempotent per sha and ``bucket``). Returns ``(op, created)``."""
    from .website_contract import SHA_PATTERN
    if trigger not in VERIFY_ONLY_TRIGGERS:
        raise ValueError(f"Unknown verify-only trigger: {trigger}")
    now = now or timezone.now()
    from .website_readiness import stored
    sha = str(source_sha or _check_sha(website, stored(website)))
    if not SHA_PATTERN.fullmatch(sha) or sealed_for(website):
        # Sealed-CI targets are re-proven by today's CI path, never by a verify-only check.
        return None, False
    key = f"{website.pk}:readiness-check:{website.generation}:{sha}:{trigger}:{bucket}"
    return _queue(website, sha, trigger=trigger, key=key[:255])


def request_manual_check(config, *, now=None):
    """The owner's ``readiness-check`` action: one verify-only check per commit per hour."""
    from .website_contract import WebsiteAuthorityError
    website = getattr(config, "website_connection", None)
    if website is None or website.state not in {"connected", "paused"}:
        raise WebsiteAuthorityError("website_disconnected", "Connect your website before checking it.")
    if not enabled_for(website) or sealed_for(website):
        raise WebsiteAuthorityError("source_reverification_unavailable", "Website checks are not available for this website yet.")
    now = now or timezone.now()
    op, created = queue_verify_only(website, trigger="manual", now=now, bucket=now.strftime("%Y%m%d%H"))
    if op is None:
        raise WebsiteAuthorityError("verified_target_required", "Set up articles before checking your website.")
    if created:
        from .website_readiness import shadow
        shadow(website, "manual", source_sha=op.payload.get("source_sha", ""), detail={"operation_id": str(op.pk)})
    return op


DELIVERY_FAILURE_CODES = frozenset({"website_source_changed", "verified_target_required",
                                    "website_publish_verification_required"})


# Content Factory's codes for an article run whose delivery or publish failed because
# the website's source or publishing target no longer matches the verified setup.
RUN_FAILURE_CODES = {"SOURCE_CHANGED": "website_source_changed", "WEBSITE_SOURCE_CHANGED": "website_source_changed",
                     "website_source_changed": "website_source_changed",
                     "verified_target_required": "verified_target_required",
                     "website_publish_verification_required": "website_publish_verification_required",
                     "PUBLISH_TARGET_UNVERIFIED": "verified_target_required",
                     "PUBLISHING_ADAPTER_REQUIRED": "website_publish_verification_required"}
# Only article delivery runs queue this check; a failed setup or scan is the prepare flow's to handle.
ARTICLE_WORKFLOWS = frozenset({"article_generation", "auto_discovery", "confirmed_topic", "direct_generate",
                               "article_revision", "publish_article", "island_refresh"})


def queue_after_delivery_failure(website, exc):
    """An article start refused for a changed source queues one verify-only check per commit. Never raises."""
    try:
        codes = {getattr(exc, "code", ""), getattr(exc, "authority_code", "")}
        _queue_delivery_recheck(website, codes, detail={})
    except Exception:  # noqa: BLE001 - see _queue_delivery_recheck
        logger.warning("website_readiness_delivery_recheck_unavailable", exc_info=True)
    return None


def queue_after_run_failure(run, error_code):
    """Content Factory reported an article run's delivery/publish failure: one verify-only check per commit.

    ``run`` is the backend's own ``ContentFactoryRun``; the website comes from
    its saved request (``website_connection_id``), never from the callback
    body, and must belong to the run's organisation domain. Setup and scan
    runs are ignored. Never dispatches a setup; never raises.
    """
    try:
        code = RUN_FAILURE_CODES.get(str(error_code or "").strip())
        if run is None or code is None or str(getattr(run, "workflow", "") or "") not in ARTICLE_WORKFLOWS:
            return None
        identifier = str(_mapping(getattr(run, "run_request", None)).get("website_connection_id") or "")
        if not identifier:
            return None
        from .website_models import WebsiteConnection
        website = WebsiteConnection.objects.select_related("organization").filter(pk=identifier).first()
        domain = str(getattr(run, "domain", "") or "").casefold()
        if website is None or str(website.organization.domain or "").casefold() != domain:
            return None
        _queue_delivery_recheck(website, {code}, detail={"run_id": str(getattr(run, "run_id", "") or ""),
                                                        "run_error_code": str(error_code)[:64]})
    except Exception:  # noqa: BLE001 - the failure callback answer stands
        logger.warning("website_readiness_run_failure_recheck_unavailable run=%s", getattr(run, "run_id", ""), exc_info=True)
    return None


def _queue_delivery_recheck(website, codes, *, detail):
    """After commit, queue the ``delivery_failure`` verify-only check (bucket ``once``: once per commit)."""
    if website is None or not codes & DELIVERY_FAILURE_CODES or not enabled_for(website):
        return
    if rules_record(website) is None:
        # Only a website the rules track (pinned, ready/checking/fixing) gets a verify-only check.
        return

    def run():
        try:
            from .website_models import WebsiteConnection
            current = WebsiteConnection.objects.select_related("organization").get(pk=website.pk)
            op, created = queue_verify_only(current, trigger="delivery_failure", bucket="once")
            if created:
                from .website_readiness import shadow
                shadow(current, "delivery_failure", source_sha=op.payload.get("source_sha", ""),
                       detail={**detail, "refusal": sorted(code for code in codes if code)[0], "operation_id": str(op.pk)})
        except Exception:  # noqa: BLE001 - the refusal answer stands
            logger.warning("website_readiness_delivery_recheck_failed connection=%s", website.pk, exc_info=True)

    transaction.on_commit(run)


def direct_setup_pin(connection, data, *, route_path=""):
    """:func:`setup_pin` for a founder's own POST to the setup view (not a prepare dispatch).

    An explicit "set up again" (``set_up_again`` / ``repair_setup``) or a
    different article route than the pinned one is a fresh setup, so no pin.
    """
    data = data if hasattr(data, "get") else {}
    fresh = any(_truthy(data.get(key)) for key in ("set_up_again", "setUpAgain", "repair_setup", "repairSetup"))
    pin = setup_pin(connection, fresh=fresh)
    if not pin:
        return {}
    try:
        from .website_readiness import stored
        pinned_route = str(_mapping(getattr(stored(connection), "pinned_contract", None)).get("routePath") or "")
    except Exception:  # noqa: BLE001 - unknown route means no pin
        return {}
    if pinned_route and route_path and pinned_route.rstrip("/") != str(route_path).rstrip("/"):
        return {}
    return pin


def _truthy(value) -> bool:
    return value is True or str(value).strip().lower() in {"1", "true", "yes"}


# --- verify-only checks --------------------------------------------------------

def _check(status, detail, *, source_sha="", now=None, **evidence):
    from .website_readiness import code_version
    stamp = (now or timezone.now()).isoformat()
    return {"status": status, "checkedAt": stamp, "sourceSha": source_sha, "codeVersion": code_version(),
            "detail": detail, "evidence": evidence}


def check_blog_files(website, source_sha, footprint, *, now=None):
    """One recursive git tree read at ``source_sha``: every footprint file present."""
    from integrations.services.github_connections import read_bound_repository
    if not footprint:
        return _check("not_run", "No blog files are recorded for this setup.", source_sha=source_sha, now=now)
    response = read_bound_repository(website, f"https://api.github.com/repos/{website.github_repo}/git/trees/{source_sha}?recursive=1")
    if getattr(response, "status_code", 0) != 200:
        return _check("not_run", "The repository files could not be read right now.", source_sha=source_sha, now=now,
                      httpStatus=getattr(response, "status_code", 0))
    data = response.json()
    if not isinstance(data, dict) or data.get("truncated") is not False or not isinstance(data.get("tree"), list):
        return _check("not_run", "The repository is too large to list in one read.", source_sha=source_sha, now=now)
    present = {str(item.get("path")) for item in data["tree"] if isinstance(item, dict) and item.get("type") == "blob"}
    missing = sorted(path for path in footprint if path not in present)
    if missing:
        return _check("failed", f"{len(missing)} blog file(s) are missing at the latest commit.", source_sha=source_sha,
                      now=now, missing=missing[:MISSING_LIMIT], files=len(footprint))
    return _check("passed", f"All {len(footprint)} blog files are in place.", source_sha=source_sha, now=now,
                  files=len(footprint))


def blog_address(website, row):
    """The live blog page: the site address plus the pinned route, or ""."""
    route = str(_mapping(getattr(row, "pinned_contract", None)).get("routePath") or "")
    base = str(getattr(website, "site_url", "") or "").rstrip("/")
    return base + route if base and route else ""


def check_live_page(website, row, *, source_sha="", now=None, fetch=None):
    """One pinned fetch of the blog address (``website_live_fetch``)."""
    from .website_contract import WebsiteAuthorityError
    url = blog_address(website, row)
    if not url:
        return _check("not_run", "No blog address is recorded yet.", source_sha=source_sha, now=now)
    if fetch is None:
        from .website_live_fetch import fetch_live_route as fetch
    try:
        fetch(url, website.organization.domain)
    except WebsiteAuthorityError as exc:
        status = "not_run" if exc.code in TRANSIENT_LIVE_CODES else "failed"
        return _check(status, "The live blog page did not answer." if status == "failed" else
                      "The live blog page could not be reached right now.", source_sha=source_sha, now=now,
                      fetchCode=exc.code, publicUrl=url)
    return _check("passed", "The live blog page answered.", source_sha=source_sha, now=now, publicUrl=url)


def check_build_render(website, row, op, *, source_sha, attempt=0, now=None, post=None):
    """Content Factory's verify-installed at ``source_sha`` for the pinned setup run; ``not_run`` when unavailable.

    Returns ``(build_render check, Content Factory's blog_files status or "")``.
    """
    from .website_connections import contract_for
    setup_run_id = str(_mapping(getattr(row, "pinned_contract", None)).get("setupRunId") or "")
    if not setup_run_id:
        return _check("not_run", "No pinned setup run to rebuild from.", source_sha=source_sha, now=now), ""
    from .vibe_marketing_views import _content_factory_headers, _content_factory_remote_config
    remote = _content_factory_remote_config()
    if not remote["enabled"]:
        return _check("not_run", "The build check is unavailable right now.", source_sha=source_sha, now=now,
                      reason="content_factory_unavailable"), ""
    binding = contract_for(website)
    body = {"head_sha": source_sha, "github_repo": binding["github_repo"], "website_connection_id": binding["website_connection_id"],
            "connection_generation": binding["connection_generation"], "repository_id": binding["repository_id"],
            "operation_id": str(op.pk), "operation_attempt": int(attempt)}
    target_key = str(_mapping(row.pinned_contract).get("targetKey") or "")
    if target_key:
        body["connection_target_id"] = target_key
    if post is None:
        from integrations import http_client
        post = http_client.post
    try:
        response = post(f"{remote['base_url']}/api/runs/{setup_run_id}/verify-installed", json=body,
                        headers=_content_factory_headers(), timeout=CF_VERIFY_TIMEOUT)
        answer = response.json() if getattr(response, "status_code", 0) == 200 else None
    except Exception:  # noqa: BLE001 - an unavailable build check is not_run, never a failure
        logger.warning("website_readiness_verify_installed_unavailable connection=%s", website.pk, exc_info=True)
        answer, response = None, None
    if not isinstance(answer, dict):
        return _check("not_run", "The build check is unavailable right now.", source_sha=source_sha, now=now,
                      reason="verify_installed_unavailable", httpStatus=getattr(response, "status_code", 0)), ""
    checks = _mapping(answer.get("checks"))
    build = _mapping(checks.get("build_render"))
    status = build.get("status") if build.get("status") in {"passed", "failed"} else "not_run"
    reason = str(_mapping(build.get("evidence")).get("reason") or "")
    unsupported = build.get("status") == "not_run" and reason in UNSUPPORTED_BUILD_REASONS
    detail = {"passed": "Built and rendered at the latest commit.", "failed": "The blog page did not build or render at the latest commit."}
    text = (UNSUPPORTED_BUILD_DETAIL if unsupported
            else detail.get(status, "The build check could not run for this site."))
    check = _check(status, text, source_sha=source_sha, now=now, setupRunId=setup_run_id,
                   answer=str(answer.get("status") or ""), durationMs=answer.get("durationMs"), reason=reason,
                   **({"unsupported": True} if unsupported else {}))
    return check, str(_mapping(checks.get("blog_files")).get("status") or "")


def build_unsupported(check) -> bool:
    """Whether ``build_render`` did not run because Content Factory cannot build this stack in place.

    Only Content Factory's own answer marks this (``evidence.unsupported``):
    timeouts, 5xx, 404/409 and a retryable witness stay plain ``not_run``.
    """
    check = _mapping(check)
    return check.get("status") == "not_run" and _mapping(check.get("evidence")).get("unsupported") is True


def verify_outcome(checks, *, require_build=False) -> str:
    """``passed`` | ``failed`` | ``unproven`` from the run checks.

    Passed needs blog files in place and a build at the commit. Only a check of
    the already-verified commit (``require_build`` False: manual, weekly or
    refused-article checks) may stand on the live blog page answering instead,
    because the live page is still served from the previous deploy after a push.

    One exception: a stack Content Factory cannot build in place (Next, Astro,
    React Router and other non-static sites, :func:`build_unsupported`) can
    never pass ``build_render``, so there blog files in place plus the live
    blog page answering is the best proof available and passes even after a
    push. A transient ``not_run`` (timeout, 5xx, 404/409) stays unproven.
    """
    statuses = {key: _mapping(value).get("status") for key, value in checks.items()}
    if "failed" in statuses.values():
        return "failed"
    if statuses.get("blog_files") != "passed":
        return "unproven"
    if statuses.get("build_render") == "passed":
        return "passed"
    if statuses.get("live_page") == "passed" and (not require_build or build_unsupported(checks.get("build_render"))):
        return "passed"
    return "unproven"


def retry_worthy(outcome, checks) -> bool:
    """An unproven answer retries; a failure retries only when the live page alone failed (the host may lag)."""
    if outcome == "unproven":
        return True
    failed = {key for key, value in checks.items() if _mapping(value).get("status") == "failed"}
    return failed == {"live_page"}


def run_checks(website, row, op, *, source_sha, footprint, attempt=0, now=None):
    """Run the verify-only checks; raises ``WebsiteAuthorityError`` for a moved branch or lost access."""
    from .website_connections import require_unlocked_remote_call, verify_repository_head
    require_unlocked_remote_call()
    verify_repository_head(website, source_sha)
    checks = {"blog_files": check_blog_files(website, source_sha, footprint, now=now)}
    checks["live_page"] = check_live_page(website, row, source_sha=source_sha, now=now)
    if checks["blog_files"]["status"] == "failed":
        checks["build_render"] = _check("not_run", "Skipped because blog files are missing.", source_sha=source_sha, now=now)
        return checks
    build, cf_files = check_build_render(website, row, op, source_sha=source_sha, attempt=attempt, now=now)
    checks["build_render"] = build
    if cf_files == "failed" and checks["blog_files"]["status"] != "failed":
        checks["blog_files"] = _check("failed", "Content Factory found blog files missing at the latest commit.",
                                      source_sha=source_sha, now=now)
    return checks


def _promote(op, source_sha, checks, *, now):
    """Move the verified commit and the default target forward after a passed check. Returns False if superseded."""
    from .models import OrganizationContentConfig
    from .website_models import WebsiteConnection
    built = _mapping(checks.get("build_render")).get("status") == "passed"
    with transaction.atomic():
        website = WebsiteConnection.objects.select_for_update().filter(pk=op.connection_id).first()
        if website is None or website.generation != op.generation or website.state not in {"connected", "paused"}:
            return False
        config = OrganizationContentConfig.objects.filter(website_connection=website).first()
        target = _default_target(website, config) if config else None
        if target is None:
            return False
        from_sha = website.verified_sha or ""
        if target.source_sha != source_sha:
            _move_target(target, source_sha, from_sha, key="rechecked_from")
        if built:
            target.verified_at = now
        target.capabilities = {**(target.capabilities or {}), "publishingReady": True}
        target.save(update_fields=["source_sha", "contract", "capabilities", "verified_at", "updated_at"])
        website.verified_sha = source_sha
        if built:
            website.last_verified_at = now
        website.blockers = [item for item in website.blockers or [] if item.get("code") != SOURCE_CHANGED]
        website.capabilities = {**(website.capabilities or {}), "publishingReady": website.state == "connected",
                                "previewSupported": True}
        website.save(update_fields=["verified_sha", "last_verified_at", "blockers", "capabilities", "updated_at"])
    return True


def _pause_publishing(op):
    """A failed check pauses publishing (drafts stay open) until a later check passes."""
    from .website_models import WebsiteConnection
    with transaction.atomic():
        website = WebsiteConnection.objects.select_for_update().filter(pk=op.connection_id).first()
        if website is None or website.generation != op.generation:
            return
        website.capabilities = {**(website.capabilities or {}), "publishingReady": False}
        website.save(update_fields=["capabilities", "updated_at"])


def _alert(website, op, checks):
    if not getattr(settings, "ARTICLES_OPERATIONAL_ALERTS_ENABLED", False):
        return
    try:
        from .founder_journey_alerts import queue_founder_journey_alert
        failed = sorted(key for key, value in checks.items() if _mapping(value).get("status") == "failed")
        queue_founder_journey_alert(organization=website.organization, alert_reason="our_team_item",
            slot=f"readiness-recheck-{website.pk}-{op.pk}",
            details={"kind": "website_readiness_recheck_failed", "failed_checks": failed,
                     "source_sha": str(op.payload.get("source_sha") or ""), "connection_generation": website.generation})
    except Exception:  # noqa: BLE001 - the stored record stands
        logger.error("website_readiness_recheck_alert_failed connection=%s", website.pk, exc_info=True)


def _settle(op, attempt, state, receipt, *, now):
    from .website_models import WebsiteConnectionOperation
    return WebsiteConnectionOperation.objects.filter(pk=op.pk, attempts=attempt, state="pending").update(
        state=state, receipt=receipt, updated_at=timezone.now(),
        next_attempt_at=now + timedelta(seconds=min(3600, 15 * 2 ** min(attempt, 8))))


def process_verify_only(op, attempt, now):
    """Run one claimed verify-only ``source-reverify`` operation; returns its new state.

    Never dispatches a scan or a setup. A moved branch supersedes the check
    (the newer push has its own); lost access settles ``needs_user``; an
    unproven answer retries up to ``VERIFY_ONLY_MAX_ATTEMPTS`` and then
    settles ``fixing``.
    """
    from .models import OrganizationContentConfig
    from .website_contract import WebsiteAuthorityError
    from .website_models import WebsiteConnection
    from .website_readiness import record, stored
    website = WebsiteConnection.objects.select_related("organization").get(pk=op.connection_id)
    config = OrganizationContentConfig.objects.filter(website_connection=website).first()
    row = stored(website)
    source_sha = str(op.payload.get("source_sha") or "")
    footprint = list(op.payload.get("footprint") or []) or footprint_for(row, config)
    trigger = op.payload.get("trigger") if op.payload.get("trigger") in VERIFY_ONLY_TRIGGERS else "push_footprint"
    receipt = {**_mapping(op.receipt), "verify_only": True, "repository_modified": False}
    state, outcome, record_state, checks = "pending", "unproven", None, {}
    # A new commit needs a build there; the live page alone proves only the old deploy.
    require_build = trigger == "push_footprint" or source_sha != str(website.verified_sha or "")
    if sealed_for(website):
        # Sealed-CI targets keep today's CI re-verification; a verify-only check never promotes them.
        _settle(op, attempt, "cancelled", {**receipt, "status": "cancelled", "code": "source_reverification_unavailable",
                                           "reason": "sealed_workflow_required", "retryable": False}, now=now)
        return "cancelled"
    try:
        if website.generation != op.generation or website.state not in {"connected", "paused"}:
            raise WebsiteAuthorityError("website_connection_changed", "The website connection changed.")
        checks = run_checks(website, row, op, source_sha=source_sha, footprint=footprint, attempt=attempt, now=now)
        outcome = verify_outcome(checks, require_build=require_build)
    except WebsiteAuthorityError as exc:
        codes = {exc.code, exc.authority_code}
        if "website_source_changed" in codes:
            _settle(op, attempt, "cancelled", {**receipt, "status": "superseded", "code": "website_source_changed",
                                               "retryable": False}, now=now)
            from .website_source_sync import after_superseded
            after_superseded(op)
            return "cancelled"
        if codes & {"website_connection_changed", "website_operation_cancelled"}:
            _settle(op, attempt, "cancelled", {**receipt, "status": "cancelled", "code": "website_connection_changed",
                                               "retryable": False}, now=now)
            return "cancelled"
        if codes & ACCESS_CODES:
            outcome, record_state = "needs_user", "needs_user"
        receipt.update(code=exc.code, retryable=bool(exc.retryable) and not codes & ACCESS_CODES)
    except Exception:  # noqa: BLE001 - an unexpected error retries like any unavailable check
        logger.warning("website_readiness_verify_only_failed operation=%s", op.pk, exc_info=True)
        receipt.update(code="source_reverification_unavailable", retryable=True)
    receipt["checks"] = {key: {"status": value.get("status"), "detail": value.get("detail")} for key, value in checks.items()}
    if outcome == "passed" and not _promote(op, source_sha, checks, now=now):
        outcome = "unproven"
    if outcome in {"unproven", "failed"} and attempt < VERIFY_ONLY_MAX_ATTEMPTS and retry_worthy(outcome, checks):
        _settle(op, attempt, "pending", {**receipt, "status": "verification_pending"}, now=now)
        return "pending"
    if outcome == "passed":
        # The record derives from the promoted facts, so it never claims more than they prove.
        state, record_state, result = "completed", None, "passed"
        receipt.update(status="verified", method="verify_only", source_sha=source_sha)
    else:
        state, result = "needs_attention", "failed"
        record_state = record_state or "fixing"
        receipt.update(status="needs_attention", retryable=False)
    _settle(op, attempt, state, receipt, now=now)
    if result == "failed":
        _pause_publishing(op)
        _alert(website, op, checks)
    try:
        record(website, trigger=trigger, source_sha=source_sha, checks=checks, result=result, state=record_state,
               detail={"operation_id": str(op.pk), "outcome": outcome, "verify_only": True,
                    "require_build": require_build,
                    "build_unsupported": build_unsupported(checks.get("build_render"))}, now=now)
    except Exception:  # noqa: BLE001 - the operation outcome is already saved
        logger.warning("website_readiness_verify_only_record_failed operation=%s", op.pk, exc_info=True)
    return state


# --- weekly look -----------------------------------------------------------------

def look_due(row, *, now) -> bool:
    """Whether a ready record's last look is at least a week old (or missing)."""
    from django.utils.dateparse import parse_datetime
    at = parse_datetime(str(_mapping(row.last_look).get("at") or "")) if _mapping(row.last_look).get("at") else None
    stamp = at or row.verified_at or row.updated_at
    return stamp is None or now - stamp >= LOOK_INTERVAL


def look(website, row, *, now, head=None, fetch=None):
    """One branch-head read and one live page fetch; returns ``(ok, checks)``. Never raises."""
    from .website_contract import WebsiteAuthorityError
    checks = {}
    try:
        if head is None:
            from integrations.services.github_connections import read_bound_repository
            from urllib.parse import quote

            def head():
                response = read_bound_repository(website, f"https://api.github.com/repos/{website.github_repo}/commits/{quote(website.branch, safe='')}")
                response.raise_for_status()
                return str(response.json().get("sha") or "")
        sha = head()
        checks["repository"] = _check("passed", "The website repository answered.", source_sha=sha, now=now)
    except Exception as exc:  # noqa: BLE001 - a failed look is counted, never raised
        checks["repository"] = _check("failed", "The website repository could not be read.", now=now, error=type(exc).__name__)
    try:
        checks["live_page"] = check_live_page(website, row, now=now, fetch=fetch)
    except (WebsiteAuthorityError, OSError) as exc:
        checks["live_page"] = _check("failed", "The live blog page did not answer.", now=now, error=type(exc).__name__)
    ok = all(value["status"] in {"passed", "not_run"} for value in checks.values())
    return ok, checks


def _save_look(row, *, now, failures):
    from .website_models import WebsiteReadiness
    WebsiteReadiness.objects.filter(pk=row.pk).update(last_look={"at": now.isoformat(), "failures": failures})


def run_weekly_look_sweep(now=None, limit=None) -> dict:
    """Self-heal sweep: look at ready websites whose last look is a week old; two failures queue a verify-only check."""
    from .website_models import WebsiteReadiness
    if getattr(settings, RECHECK_RULES_FLAG, False) is not True:
        return {"skipped": "flag_off"}
    now = now or timezone.now()
    if not cache.add(LOOK_THROTTLE_KEY, now.isoformat(), LOOK_THROTTLE_SECONDS):
        return {"skipped": "throttled"}
    counts = {"looked": 0, "failed": 0, "queued": 0, "skipped": 0}
    rows = _look_candidates(WebsiteReadiness.objects.select_related("connection__organization"))
    for row in rows[: (limit or SWEEP_LIMIT) * 4]:
        if counts["looked"] >= (limit or SWEEP_LIMIT):
            break
        website = row.connection
        if row.generation != website.generation or not enabled_for(website) or not look_due(row, now=now):
            counts["skipped"] += 1
            continue
        try:
            counts.update(_look_once(website, row, now=now, counts=counts))
        except Exception:  # noqa: BLE001 - one site must not stop the sweep
            logger.warning("website_readiness_weekly_look_failed connection=%s", website.pk, exc_info=True)
    return counts


def _look_candidates(rows):
    """Ready, current-generation records of enabled domains only, least recently written first.

    Filtering before the window keeps rows the rules skip (other domains, an
    old generation) from holding the front of it for good.
    """
    from functools import reduce
    from operator import or_

    from django.db.models import F, Q
    from .website_readiness import _domains
    domains = sorted(_domains())
    if not domains:
        return rows.none()
    scope = reduce(or_, (Q(connection__organization__domain__iexact=domain) for domain in domains))
    return (rows.filter(scope, state="ready", connection__state="connected", generation=F("connection__generation"))
            .order_by("updated_at", "pk"))


def _look_once(website, row, *, now, counts):
    from .website_readiness import record
    ok, checks = look(website, row, now=now)
    failures = 0 if ok else int(_mapping(row.last_look).get("failures") or 0) + 1
    _save_look(row, now=now, failures=failures)
    counts = dict(counts)
    counts["looked"] += 1
    counts["failed"] += int(not ok)
    detail = {"failures": failures}
    if failures >= LOOK_FAILURES_BEFORE_RECHECK:
        op, created = queue_verify_only(website, trigger="weekly_look", now=now, bucket=now.strftime("%G-W%V"))
        counts["queued"] += int(bool(created))
        detail["operation_id"] = str(op.pk) if op else ""
    record(website, trigger="weekly_look", source_sha=website.verified_sha or "", checks=checks,
           result="passed" if ok else "failed", detail=detail, now=now)
    return counts


# --- pinned setup (step 5) --------------------------------------------------------

def setup_pin(connection, *, fresh=False):
    """``pinned_setup_run_id`` / ``strategy_pin`` for a setup request, or ``{}``.

    Only with ``ARTICLES_READINESS_PIN_ENABLED`` for the organisation, a stored
    pinned contract with a setup run, and not when the founder or staff chose
    to set up again (``fresh``).
    """
    try:
        from .website_readiness import readiness_enabled, stored
        if fresh or connection is None or not readiness_enabled("pin", connection.organization):
            return {}
        row = stored(connection)
        pin = _mapping(getattr(row, "pinned_contract", None))
        if not pin.get("setupRunId") or pin.get("generation") not in (None, connection.generation):
            # A pin from another connection generation (reset, reconnect) is never replayed.
            return {}
        values = {"pinned_setup_run_id": str(pin["setupRunId"])}
        rung = STRATEGY_FOR_MODE.get(str(pin.get("adapterMode") or ""))
        if rung:
            values["strategy_pin"] = rung
        return values
    except Exception:  # noqa: BLE001 - a setup without the pin is today's behaviour
        logger.warning("website_readiness_setup_pin_failed connection=%s", getattr(connection, "pk", ""), exc_info=True)
        return {}


# --- nightly compare (step 6) -----------------------------------------------------

def compare_row(row, derived) -> dict | None:
    """Pure: the flip between a saved record and today's derivation, or None when they agree."""
    if derived["state"] == row.state:
        return None
    return {"from": row.state, "to": derived["state"], "reasonCode": derived.get("reason_code", ""),
            "legacy": _mapping(derived.get("legacy")).get("state", "")}


def run_nightly_compare_sweep(now=None, limit=None) -> dict:
    """Report-only: re-derive every stored record once a day and log each state flip as a check event."""
    from .website_readiness import readiness_enabled
    if not readiness_enabled("nightly_compare"):
        return {"skipped": "flag_off"}
    now = now or timezone.now()
    if not cache.add(COMPARE_DAY_KEY.format(day=now.date().isoformat()), now.isoformat(), COMPARE_DAY_TTL):
        return {"skipped": "already_ran_today"}
    counts = {"compared": 0, "flipped": 0, "legacy_differs": 0, "failed": 0, "alerted": 0}
    from .website_models import WebsiteReadiness
    rows = WebsiteReadiness.objects.select_related("connection__organization").order_by("pk")
    for row in rows[: limit or 5000]:
        try:
            flip = _compare_one(row, now=now, alert=counts["alerted"] < COMPARE_ALERTS_PER_DAY)
        except Exception:  # noqa: BLE001 - one row must not hide the rest
            counts["failed"] += 1
            logger.warning("website_readiness_compare_failed readiness=%s", row.pk, exc_info=True)
            continue
        counts["compared"] += 1
        counts["flipped"] += int(bool(flip))
        counts["alerted"] += int(bool(flip and flip.get("alerted")))
        counts["legacy_differs"] += int(_mapping(row.legacy).get("state") not in ("", None, row.state))
    logger.info("website_readiness_nightly_compare compared=%s flipped=%s legacy_differs=%s failed=%s",
                counts["compared"], counts["flipped"], counts["legacy_differs"], counts["failed"])
    return counts


def _compare_one(row, *, now, alert=True):
    from .models import OrganizationContentConfig
    from .website_models import WebsiteCheckEvent
    from .website_readiness import code_version, derive
    website = row.connection
    config = OrganizationContentConfig.objects.filter(website_connection=website).first()
    if config is None or row.generation != website.generation:
        return None
    flip = compare_row(row, derive(config, website, now=now))
    if flip is None:
        return None
    WebsiteCheckEvent.objects.create(connection=website, generation=website.generation, trigger="nightly_compare",
        source_sha=str(website.verified_sha or "")[:64], changed_files=[], checks={}, result="failed",
        state_before=row.state, state_after=row.state, code_version=code_version(), detail=flip)
    if alert and getattr(settings, "ARTICLES_OPERATIONAL_ALERTS_ENABLED", False):
        try:
            from .founder_journey_alerts import queue_founder_journey_alert
            queue_founder_journey_alert(organization=website.organization, alert_reason="our_team_item",
                slot=f"readiness-compare-{website.pk}-{now.date().isoformat()}", details={"kind": "website_readiness_flip", **flip})
            return {**flip, "alerted": True}
        except Exception:  # noqa: BLE001 - report-only
            logger.error("website_readiness_compare_alert_failed connection=%s", website.pk, exc_info=True)
    return flip
