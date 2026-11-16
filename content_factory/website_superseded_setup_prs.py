"""Close MLAI's own older setup pull requests once preparation has a newer one.

La Vie, 16 Nov 2026: preparation opened setup PR #38 while MLAI's PR #37 for
the same connection generation stayed open, so the founder saw two competing
setup pull requests. When preparation holds a setup run with its own pull
request, each older open setup pull request of the same connection generation
is closed with a short comment that links the newer one.

Only pull requests MLAI opened are touched. A candidate must be an
``article_system_setup`` run of this organisation, connection and generation,
created before the newer run, with its pull request number recorded on the run.
GitHub must then confirm the pull request is still open and unmerged, was
opened by a GitHub App (a bot account, never a person), comes from a branch in
the same repository (never a fork) and is not the website's own branch. A
founder's pull request carries no MLAI run, so it can never be selected.

Closing never fails preparation: every error is logged and the pass carries on.
A pass that hit an error is tried again on a later pass, at most
``MAX_PASSES`` times per newer run. ``ARTICLES_CLOSE_SUPERSEDED_SETUP_PRS_ENABLED``
is a kill switch that defaults on.
"""
from __future__ import annotations

import copy
import logging

from django.conf import settings

logger = logging.getLogger(__name__)

SETUP_WORKFLOW = "article_system_setup"
RECEIPT_KEY = "superseded_setup_prs"
MAX_PASSES = 3
MAX_CANDIDATES = 10
DONE_MERGE_STATUSES = frozenset({"merged", "pr_closed"})


def enabled() -> bool:
    return bool(getattr(settings, "ARTICLES_CLOSE_SUPERSEDED_SETUP_PRS_ENABLED", True))


def _mapping(value):
    return value if isinstance(value, dict) else {}


def _merge_statuses(run):
    result = _mapping(run.result)
    return {result.get("merge_status"), _mapping(result.get("article_system_setup")).get("merge_status")}


def _same_generation(run, connection) -> bool:
    from .website_connections import scoped_run_contract
    saved = scoped_run_contract(run)
    return (str(saved.get("website_connection_id") or "") == str(connection.pk)
            and str(saved.get("connection_generation") or "") == str(connection.generation)
            and str(getattr(run, "github_repo", "") or "").casefold() == str(getattr(connection, "github_repo", "") or "").casefold())


def _recorded_branch(run) -> str:
    result = _mapping(run.result)
    setup = _mapping(result.get("article_system_setup"))
    for mapping in (result, setup):
        for key in ("head_branch", "branch_name", "pr_branch"):
            if isinstance(mapping.get(key), str) and mapping[key].strip():
                return mapping[key].strip()
    return ""


def close_superseded_setup_prs(op, child) -> dict:
    """Close older open MLAI setup pull requests that ``child``'s pull request replaces.

    Returns a small summary; never raises.
    """
    try:
        return _close(op, child)
    except Exception:
        logger.exception("website_prepare superseded_setup_prs_failed operation=%s run=%s",
            getattr(op, "pk", None), getattr(child, "run_id", None))
        return {"status": "error"}


def _close(op, child) -> dict:
    if not enabled() or child is None or child.workflow != SETUP_WORKFLOW:
        return {"status": "skipped"}
    connection = op.connection
    if (op.generation != connection.generation or getattr(connection, "state", "") != "connected"
            or not getattr(connection, "github_repo", "") or not _same_generation(child, connection)):
        return {"status": "skipped"}
    if _merge_statuses(child) & {"pr_closed"}:
        return {"status": "skipped"}
    from .vibe_marketing_views import _pull_request_number_from_run
    new_number = _pull_request_number_from_run(child)
    if not new_number:
        return {"status": "skipped"}
    saved = _mapping(op.receipt.get(RECEIPT_KEY))
    if saved.get("run_id") == child.run_id and (saved.get("done") or int(saved.get("passes") or 0) >= MAX_PASSES):
        return {"status": "done"}
    passes = int(saved.get("passes") or 0) + 1 if saved.get("run_id") == child.run_id else 1
    closed = list(saved.get("closed") or []) if saved.get("run_id") == child.run_id else []
    errors = 0
    for run in _candidates(connection, child):
        number = _pull_request_number_from_run(run)
        if not number or number == new_number or number in closed:
            continue
        try:
            if _close_one(connection, run, number, child=child, new_number=new_number):
                closed.append(number)
        except Exception as exc:
            errors += 1
            logger.warning("website_prepare superseded_setup_pr_close_failed operation=%s run=%s pr=%s error_type=%s",
                op.pk, run.run_id, number, type(exc).__name__)
    op.receipt[RECEIPT_KEY] = {"run_id": child.run_id, "pr_number": new_number, "closed": closed,
        "passes": passes, "done": errors == 0}
    return {"status": "completed" if errors == 0 else "partial", "closed": closed}


def _candidates(connection, child):
    from workflow_runs.models import ContentFactoryRun
    from .website_connections import _current_run_scope_filter
    created = getattr(child, "created_at", None)
    if created is None:
        return []
    rows = ContentFactoryRun.objects.filter(_current_run_scope_filter(connection),
        organization_id=connection.organization_id, workflow=SETUP_WORKFLOW,
        created_at__lt=created).exclude(run_id=child.run_id).order_by("-created_at")[:MAX_CANDIDATES]
    return [row for row in rows if not (_merge_statuses(row) & DONE_MERGE_STATUSES) and _same_generation(row, connection)]


def _close_one(connection, run, number, *, child, new_number) -> bool:
    """Close one pull request once GitHub confirms it is MLAI's own and still open."""
    from .vibe_marketing_views import _github_api_request, _github_token_for_repo_operation, _pull_request_url_from_run
    repo = connection.github_repo
    token, _ = _github_token_for_repo_operation(domain=connection.organization.domain, github_repo=repo,
        permission_mode="write")
    pull = _github_api_request("GET", f"/repos/{repo}/pulls/{number}", token=token)
    head = _mapping(pull.get("head"))
    head_repo = str(_mapping(head.get("repo")).get("full_name") or "")
    branch = _recorded_branch(run)
    if (pull.get("state") != "open" or pull.get("merged") or pull.get("merged_at")
            or _mapping(pull.get("user")).get("type") != "Bot"
            or head_repo.casefold() != str(repo).casefold()
            or str(head.get("ref") or "") == str(connection.branch or "")
            or (branch and str(head.get("ref") or "") != branch)):
        logger.info("website_prepare superseded_setup_pr_left_open run=%s pr=%s", run.run_id, number)
        return False
    new_url = _pull_request_url_from_run(child) or f"https://github.com/{repo}/pull/{new_number}"
    _github_api_request("POST", f"/repos/{repo}/issues/{number}/comments", token=token, expected=(201,), body={
        "body": (f"MLAI opened a newer articles setup pull request for this website: {new_url}\n\n"
                 "This one is superseded, so MLAI is closing it. Nothing on your site changes.")})
    _github_api_request("PATCH", f"/repos/{repo}/pulls/{number}", token=token, body={"state": "closed"})
    _mark_closed(run, superseded_by=child.run_id)
    logger.info("website_prepare superseded_setup_pr_closed run=%s pr=%s superseded_by=%s pr=%s",
        run.run_id, number, child.run_id, new_number)
    return True


def _mark_closed(run, *, superseded_by) -> None:
    """Record the close on the older run only; the pending setup keeps pointing at the newer one."""
    from .run_provider_facts import save_provider_result
    baseline = copy.deepcopy(run.result or {})
    result = dict(run.result or {})
    setup = dict(_mapping(result.get("article_system_setup")))
    for item in (result, setup):
        # The same fields the merge path writes for a closed setup pull request,
        # so no lookup resumes this run as an open pull request.
        item.update(status="pr_closed", setup_status="pr_closed", setupStatus="pr_closed", merge_status="pr_closed",
            mergeStatus="pr_closed", superseded_by_run_id=superseded_by)
    result["article_system_setup"] = setup
    save_provider_result(run, result, baseline=baseline, legacy_fields=["result", "updated_at"])
