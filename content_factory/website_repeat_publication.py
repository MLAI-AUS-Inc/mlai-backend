"""Bounded live-content observation for an ordinary article after its merge.

This read/persistence helper never starts work, merges, deploys, charges, adopts
consent or reuses a first-article goal. The existing refresher owns body/PR proof
and cooldowns; provider timeouts remain its established best-effort contract.
"""
import hashlib
import math
import re
from urllib.parse import urlsplit
from uuid import UUID

from django.core.cache import cache
from django.db import connection as database

from .run_state import ARTICLE_WORKFLOWS

GOAL_FIELDS = frozenset({"first_article_goal_operation_id", "first_article_goal_id", "strategy_goal_id", "goal_budget",
    "firstArticleGoalOperationId", "firstArticleGoalId", "strategyGoalId", "goalBudget"})
_SCOPE = ("website_connection_id", "connection_generation", "repository_id", "deletion_epoch", "expected_source_sha", "branch", "app_root")
SHA = re.compile(r"[a-f0-9]{40}(?:[a-f0-9]{24})?\Z")
THROTTLE_SECONDS = 30
MAX_SECONDS = 3.0


def _mapping(value):
    return value if isinstance(value, dict) else {}


def eligible_request(run, context):
    """Skip legacy, portable, pending, foreign and goal work without DB reads."""
    request = _mapping(getattr(run, "run_request", None))
    organization = getattr(context, "organization", None)
    company = getattr(context, "company", None)
    organization_id = getattr(organization, "pk", None)
    if (run is None or organization_id is None or company is None
            or getattr(company, "organization_id", None) != organization_id
            or getattr(run, "organization_id", None) != organization_id
            or not isinstance(getattr(run, "workflow", None), str) or run.workflow not in ARTICLE_WORKFLOWS
            or getattr(run, "status", None) != "completed"
            or GOAL_FIELDS & request.keys() or request.get("delivery_mode") == "content_only"
            or request.get("resolved_delivery_mode") == "content_only"
            or any(key not in request for key in _SCOPE)
            or any(type(request[key]) is not int or request[key] < minimum for key, minimum in
                (("connection_generation", 1), ("repository_id", 1), ("deletion_epoch", 0)))
            or type(request.get("operation_attempt")) is not int or request["operation_attempt"] < 1
            or not isinstance(request["expected_source_sha"], str) or not SHA.fullmatch(request["expected_source_sha"])
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", str(getattr(run, "run_id", "")))
            or not isinstance(request["branch"], str) or not request["branch"]
            or not isinstance(request["app_root"], str)
            or not isinstance(getattr(run, "github_repo", None), str) or not run.github_repo
            or str(request.get("github_repo", "")).casefold() != run.github_repo.casefold()
            or not isinstance(getattr(run, "domain", None), str) or not run.domain
            or not isinstance(getattr(organization, "domain", None), str)
            or run.domain.casefold() != organization.domain.casefold()
            or not isinstance(request.get("domain", run.domain), str)
            or request.get("domain", run.domain).casefold() != run.domain.casefold()):
        return False
    try:
        return (str(UUID(request["website_connection_id"])) == request["website_connection_id"]
            and str(UUID(request["operation_id"])) == request["operation_id"])
    except (KeyError, ValueError, TypeError, AttributeError):
        return False


def _merged(child):
    result = _mapping(child.result)
    response = _mapping(result.get("merge_response"))
    sha = result.get("merge_sha")
    if (result.get("merge_status") != "merged" or response.get("merged") is not True
            or not isinstance(sha, str) or not SHA.fullmatch(sha) or response.get("sha") != sha):
        return None
    parsed = urlsplit(str(result.get("pr_url") or result.get("prUrl") or ""))
    if (parsed.scheme != "https" or parsed.hostname != "github.com" or parsed.port not in {None, 443}
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or not re.fullmatch(r"/" + re.escape(child.github_repo) + r"/pull/[1-9][0-9]*", parsed.path, flags=re.IGNORECASE)):
        return None
    return sha


def refresh_owned_publication(*, run, context, config=None, child=None, max_seconds=MAX_SECONDS):
    """Observe only the exact current-owned ordinary writer and merged child.

    Network runs outside database transactions. Cooldowns and a target-bound
    throttle prevent status polling from continually fetching origin/GitHub.
    The per-call budget is capped; existing individual provider timeouts apply.
    """
    if (not eligible_request(run, context) or database.in_atomic_block
            or type(max_seconds) not in {int, float} or not math.isfinite(max_seconds) or max_seconds <= 0):
        return []
    try:
        from . import vibe_marketing_views as views
        if child is None:
            child = views._publish_child_for_run_state(run, context=context)
        if not eligible_request(child, context):
            return []
        writing = views._publish_source_run_for_child(child, context) if child.run_id == run.run_id else run
        if writing is None or not eligible_request(writing, context) or writing.run_id == child.run_id:
            return []
        relationships = {surface[key] for surface in (_mapping(child.run_request), _mapping(child.result))
            for key in ("source_run_id", "sourceRunId") if surface.get(key)}
        pointers = {run.result[key] for key in ("publish_child_run_id", "promoted_publish_job_id")
            if _mapping(run.result).get(key)} if run.run_id != child.run_id else {child.run_id}
        if (relationships != {writing.run_id} or pointers != {child.run_id}
                or any(writing.run_request[key] != child.run_request[key]
                    or type(writing.run_request[key]) is not type(child.run_request[key]) for key in _SCOPE)
                or writing.github_repo.casefold() != child.github_repo.casefold()):
            return []
        merge_sha = _merged(child)
        if merge_sha is None:
            return []
        from .models import OrganizationContentConfig
        selected = OrganizationContentConfig.objects.select_related("website_connection__organization").filter(
            organization_id=context.organization.pk).first()
        connection = getattr(selected, "website_connection", None)
        if (connection is None or config is not None and getattr(config, "pk", None) != selected.pk
                or connection.organization_id != context.organization.pk
                or str(connection.pk) != writing.run_request["website_connection_id"]
                or type(connection.generation) is not int or connection.generation != writing.run_request["connection_generation"]
                or type(connection.repository_id) is not int or connection.repository_id != writing.run_request["repository_id"]
                or connection.github_repo.casefold() != writing.github_repo.casefold()
                or selected.github_repo.casefold() != connection.github_repo.casefold()
                or str(selected.github_installation_id) != str(connection.installation_id)
                or not str(connection.installation_id).isdecimal()
                or connection.branch != writing.run_request["branch"] or connection.app_root != writing.run_request["app_root"]):
            return []
        from .website_contract import validate_authority
        from .website_operations import validate_operation, deletion_epoch
        if deletion_epoch(connection) != writing.run_request["deletion_epoch"]:
            return []
        for owned in (writing, child):
            payload = {**owned.run_request, "run_id": owned.run_id}
            validate_authority(connection, payload, action="read", domain=owned.domain, github_repo=owned.github_repo)
            validate_operation(connection, payload)
        identity = {"connection": str(connection.pk), "generation": connection.generation,
            "writer": writing.run_id, "child": child.run_id, "merge": merge_sha}
        key = "articles:repeat-live:" + hashlib.sha256(repr(sorted(identity.items())).encode()).hexdigest()
        if not cache.add(key, 1, THROTTLE_SECONDS):
            return []
        from .article_publish_status import refresh_publish_statuses
        refreshed = refresh_publish_statuses(context.organization, selected, limit=2, force=False,
            max_seconds=min(float(max_seconds), MAX_SECONDS), source_run_ids=(writing.run_id, child.run_id))
        if run.run_id == writing.run_id:
            # Preserve presentation-only child annotations already on this poll.
            # These are the exact receipts the existing refresher just saved.
            receipts = dict(_mapping(_mapping(run.result).get("release_observations")))
            for article in refreshed:
                observation = getattr(article, "_live_observation", None)
                if article.source_run_id == writing.run_id and isinstance(observation, dict):
                    receipts[str(article.id)] = observation
            if receipts:
                run.result = {**_mapping(run.result), "release_observations": receipts}
        return refreshed
    except Exception:
        # A read failure cannot reopen a goal or change payment/consent/PR state.
        return []
