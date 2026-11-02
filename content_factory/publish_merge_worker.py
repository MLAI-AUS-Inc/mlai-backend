"""Consent-bound, exact-head publication merges and post-merge deployment watch.

Database transactions reserve work and protect receipts. Provider requests are
always outside them; a fresh authority/consent check precedes the merge CAS.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta
import importlib
import logging
import re
from urllib.parse import quote
from uuid import uuid4

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from integrations import http_client
from integrations.services.github_app import create_installation_access_token
from . import p2_foundation as foundation
from .website_connections import authority_guard, require_unlocked_remote_call, scoped_run_contract
from .website_contract import WebsiteAuthorityError

logger = logging.getLogger(__name__)
SHA = re.compile(r"[0-9a-f]{40}")
HASH = re.compile(r"[0-9a-f]{64}")
KINDS = {"article", "setup", "scaffold", "proxy"}
POLL_MINUTES = (1, 2, 5, 10, 20, 30)


class ProviderError(ValueError):
    """A bounded provider error retaining status, never credentials."""

    def __init__(self, status, message="GitHub request could not be completed"):
        self.status = status
        super().__init__(message)


def _mapping(value):
    return value if isinstance(value, dict) else {}


def _moment(value):
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return result if timezone.is_aware(result) else None
    except (ValueError, TypeError):
        return None


def _state(run):
    return _mapping(_mapping(_mapping(run.result).get("self_heal_v1")).get("merge_worker"))


def _update_locked(run, result):
    # Backend bookkeeping never changes CF's status or progress timestamp.
    from workflow_runs.models import ContentFactoryRun
    ContentFactoryRun.objects.filter(pk=run.pk).update(result=result)
    run.result = result


def _claim(identifier, *, now, watch=False):
    from organizations.models import Organization
    from workflow_runs.models import ContentFactoryRun
    snapshot = ContentFactoryRun.objects.filter(pk=identifier).first()
    if not snapshot or not snapshot.organization_id:
        return None
    with transaction.atomic():
        org = Organization.objects.select_for_update().get(pk=snapshot.organization_id)
        run = ContentFactoryRun.objects.select_for_update().get(pk=identifier)
        if (run.organization_id != org.pk or run.status in {"cancelled", "denied"} or not foundation.p2_applies(org)
                or foundation.run_merge_policy(run) != foundation.BACKEND_WORKER):
            return None
        result, state = deepcopy(_mapping(run.result)), deepcopy(_state(run))
        merged = _mapping(result.get("merge_v1"))
        if bool(merged) != watch or watch and result.get("deploy_v1"):
            return None
        if state.get("capped") and not watch or watch and state.get("watch_reason") == "deadline":
            return None
        due = _moment(state.get("watch_next_at" if watch else "next_at"))
        lease = _moment(state.get("lease_until"))
        if due and due > now or lease and lease > now:
            return None
        token = uuid4().hex
        state.update(claim=token, lease_until=(now + timedelta(minutes=10)).isoformat())
        result["self_heal_v1"] = {**_mapping(result.get("self_heal_v1")), "merge_worker": state}
        _update_locked(run, result)
        run.organization = org
        return run, token


def _finish(run, token, *, state=None, fields=None):
    """Apply only the current claim's result, retaining concurrent CF updates."""
    from workflow_runs.models import ContentFactoryRun
    with transaction.atomic():
        current = ContentFactoryRun.objects.select_for_update().get(pk=run.pk)
        worker = deepcopy(_state(current))
        if worker.get("claim") != token:
            return False
        worker.update(state or {})
        worker.pop("claim", None)
        worker.pop("lease_until", None)
        result = {**_mapping(current.result), **(fields or {})}
        if fields and (fields.get("merge_v1") or fields.get("deploy_v1")):
            if result.get("error_code") in {"BRANCH_PROTECTION_APPROVAL_REQUIRED", "DEPLOYMENT_NOT_DETECTED"}:
                result.pop("error_code", None)
                result.pop("failure_class", None)
            if _mapping(result.get("user_action")).get("id") == "approve_on_github":
                result.pop("user_action", None)
        result["self_heal_v1"] = {**_mapping(result.get("self_heal_v1")), "merge_worker": worker}
        _update_locked(current, result)
        run.result = result
        return True


def _api(connection, token, method, path, *, body=None, accepted=(200,)):
    require_unlocked_remote_call()
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", connection.github_repo):
        raise ProviderError(0)
    response = http_client.request(method, "https://api.github.com/repos/" + connection.github_repo + path,
        headers={"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28"}, json=body, timeout=(3, 15))
    if response.status_code not in accepted:
        # Provider text can include arbitrary source/log data. Keep only a
        # bounded classification for the review-required case.
        text = str(getattr(response, "text", ""))[:2000].lower()
        review = any(word in text for word in ("review", "protected branch", "pull request is not mergeable: required"))
        raise ProviderError(response.status_code, "review_required" if review else "GitHub request could not be completed")
    value = response.json()
    if not isinstance(value, (dict, list)):
        raise ProviderError(0)
    return value


def _token(connection, *, checks=False):
    require_unlocked_remote_call()
    return create_installation_access_token(installation_id=connection.installation_id,
        repository=connection.github_repo, repository_id=connection.repository_id,
        permission_mode="read" if checks else "write",
        permission_profile="ci_evidence" if checks else "repository", use_cache=False).token


def _owned(receipt, connection, pull=None):
    """Refuse foreign/forked/non-CF heads before any write credential is used."""
    if (receipt.get("kind") not in KINDS or type(receipt.get("number")) is not int or receipt["number"] <= 0
            or receipt.get("repository_id") != connection.repository_id
            or type(receipt.get("repository_id")) is not int
            or receipt.get("head_repository_id") != connection.repository_id
            or not str(receipt.get("branch") or "").startswith("mlai/")
            or receipt.get("base") != connection.branch
            or not SHA.fullmatch(str(receipt.get("head_sha") or ""))
            or receipt.get("pr_url") != f"https://github.com/{connection.github_repo}/pull/{receipt['number']}"):
        return False
    if pull is None:
        return True
    head, base = _mapping(pull.get("head")), _mapping(pull.get("base"))
    return (pull.get("number") == receipt["number"] and head.get("ref") == receipt["branch"]
        and base.get("ref") == connection.branch
        and all(_mapping(side.get("repo")).get("id") == connection.repository_id
            and str(_mapping(side.get("repo")).get("full_name") or "").casefold() == connection.github_repo.casefold()
            for side in (head, base)))


def _approver(organization, identifier, *, owner_only=False):
    from founder_tools.services import organization_owner_user_id
    from core.models import User
    if not identifier or isinstance(identifier, bool):
        return False
    try:
        identifier = int(identifier)
    except (TypeError, ValueError):
        return False
    user = User.objects.filter(pk=identifier, is_active=True).first()
    if user is None:
        return False
    if organization_owner_user_id(organization) == identifier:
        return True
    if owner_only:
        return False
    try:
        provider = getattr(importlib.import_module("founder_tools.startup_team"), "member_may_publish")
    except (ImportError, AttributeError):
        return False
    return bool(provider(user, organization))


def _claims(run, identity):
    """Read canonical current findings through the authenticated review facade."""
    from .article_review_views import remote_review
    review = remote_review(run)
    if not isinstance(review, dict):
        return None
    bound = _mapping(review.get("reviewFindingsIdentity"))
    if not bound.get("revision") or {key: value for key, value in bound.items() if key != "revision"} != identity:
        return None
    findings = review.get("reviewFindings")
    if not isinstance(findings, list) or any(not isinstance(item, dict) or item.get("kind") not in {"style", "claim"} for item in findings):
        return None
    return sum(item["kind"] == "claim" for item in findings)


def _copy_article_approval(run):
    from .article_publish_approval import article_publish_approval_receipt_matches, article_review_identity
    if not article_publish_approval_receipt_matches(run):
        return None
    source = _mapping(run.run_request).get("article_publish_approval_receipt") or {}
    identity = article_review_identity(run)
    claims = _claims(run, identity)
    # Copy the P1 receipt fields verbatim; never reconstruct a consent from
    # readiness or an inferred approver.
    return {"version": 1, "source": "receipt", "approved_by_user_id": source["actor_id"],
        "approved_at": source["approved_at"], "review_identity": {key: source[key] for key in identity},
        "approved_body_sha256": None, "claims_open": claims}


def _consent(run, receipt):
    from .founder_journey_cohort import read_marker
    result = _mapping(run.result)
    if receipt["kind"] != "article":
        approval = _mapping(result.get("owner_merge_approval_v1"))
        valid = (approval.get("version") == 1 and approval.get("kind") == receipt["kind"]
            and approval.get("head_sha") == receipt["head_sha"] and approval.get("pr_url") == receipt["pr_url"]
            and bool(approval.get("approved_at"))
            and _approver(run.organization, approval.get("approved_by_user_id"), owner_only=True))
        return ("owner_merge_approval_v1", approval) if valid else ("merge_waiting_approval", None)
    approval = _mapping(result.get("article_approval_v1"))
    if not approval or approval.get("version") != 1 or not approval.get("approved_at"):
        return "merge_waiting_approval", None
    if type(approval.get("claims_open")) is not int or approval["claims_open"] != 0:
        return "merge_waiting_claims", None
    if not _approver(run.organization, approval.get("approved_by_user_id")):
        return "merge_waiting_approval", None
    if approval.get("source") == "receipt":
        from .article_publish_approval import article_publish_approval_receipt_matches, article_review_identity
        identity = _mapping(approval.get("review_identity"))
        if (not article_publish_approval_receipt_matches(run) or identity != article_review_identity(run)
                or not HASH.fullmatch(str(identity.get("quality_inputs_sha256") or ""))
                or identity["quality_inputs_sha256"] != receipt.get("review_quality_inputs_sha256")
                or not _mapping(_mapping(read_marker(run.organization)).get("publishAutomatically")).get("enabledAt")):
            return "merge_waiting_approval", None
    elif approval.get("source") == "publish_reviewed":
        digest = approval.get("approved_body_sha256")
        if not HASH.fullmatch(str(digest or "")) or digest != receipt.get("article_sha256"):
            return "merge_waiting_approval", None
    else:
        return "merge_waiting_approval", None
    return "article_approval_v1", approval


def _policy(connection, token):
    required, unknown, reviews = set(), False, False
    branch = quote(connection.branch, safe="")
    for page in range(1, 11):
        try:
            rules = _api(connection, token, "GET", f"/rules/branches/{branch}?per_page=100&page={page}")
        except ProviderError as exc:
            if exc.status not in {403, 404}:
                raise
            unknown = True
            break
        if not isinstance(rules, list):
            raise ProviderError(0)
        for rule in rules:
            if not isinstance(rule, dict):
                raise ProviderError(0)
            parameters = _mapping(rule.get("parameters"))
            if rule.get("type") == "required_status_checks":
                checks = parameters.get("required_status_checks")
                if not isinstance(checks, list):
                    raise ProviderError(0)
                for item in checks:
                    if not isinstance(item, dict) or not isinstance(item.get("context"), str):
                        raise ProviderError(0)
                    required.add((item["context"], item.get("integration_id")))
            if rule.get("type") == "pull_request":
                reviews |= any(parameters.get(key) for key in ("required_approving_review_count", "require_code_owner_review", "require_last_push_approval"))
        if len(rules) < 100:
            break
    else:
        raise ProviderError(0)
    try:
        classic = _api(connection, token, "GET", f"/branches/{branch}/protection")
        if not isinstance(classic, dict):
            raise ProviderError(0)
        status = _mapping(classic.get("required_status_checks"))
        for item in status.get("checks") or []:
            required.add((item["context"], item.get("app_id")))
        required.update((context, None) for context in status.get("contexts") or [])
        review = _mapping(classic.get("required_pull_request_reviews"))
        reviews |= any(review.get(key) for key in ("required_approving_review_count", "require_code_owner_reviews", "require_last_push_approval"))
    except ProviderError as exc:
        if exc.status not in {403, 404}:
            raise
        unknown = True
    return required, unknown, bool(reviews)


def _checks(connection, token, sha, required):
    statuses = _api(connection, token, "GET", f"/commits/{sha}/status?per_page=100")
    runs = _api(connection, token, "GET", f"/commits/{sha}/check-runs?per_page=100")
    if not isinstance(statuses, dict) or not isinstance(runs, dict):
        raise ProviderError(0)
    if statuses.get("sha") != sha:
        raise ProviderError(0)
    if runs.get("total_count", 0) > 100 or statuses.get("total_count", 0) > 100:
        raise ProviderError(0)
    values = {}
    for row in statuses.get("statuses") or []:
        values.setdefault((row.get("context"), None), row.get("state"))
    for row in runs.get("check_runs") or []:
        if row.get("head_sha") != sha:
            continue
        state = row.get("conclusion") if row.get("status") == "completed" else "pending"
        values.setdefault((row.get("name"), _mapping(row.get("app")).get("id")), state)
        values.setdefault((row.get("name"), None), state)
    observed = [values.get(key) for key in required]
    if any(value in {"failure", "error", "cancelled", "timed_out", "action_required"} for value in observed):
        return "failed"
    return "passed" if all(value == "success" for value in observed) else "pending"


def _ask(run, token, receipt, reason, *, now, code=None):
    key = str(receipt["number"]) + ":" + reason
    state = {"reason": reason, "ask_key": key, "next_at": (now + timedelta(minutes=1)).isoformat()}
    fields = {"merge_status": "manual_merge_required", "user_action": {"id": "approve_on_github", "url": receipt["pr_url"]}}
    if code == "BRANCH_PROTECTION_APPROVAL_REQUIRED":
        fields.update(error_code="BRANCH_PROTECTION_APPROVAL_REQUIRED", failure_class="needs_user")
    if _state(run).get("ask_key") != key:
        logger.info("%s run_id=%s pr_number=%s", reason, run.run_id, receipt["number"])
    _finish(run, token, state=state, fields=fields)
    return reason


def _wait(run, token, reason, *, now, rejected=False):
    from .registry_policy import limits
    state = _state(run)
    attempts = int(state.get("attempts") or 0) + 1
    rejects = int(state.get("rejections") or 0) + int(rejected)
    delays = limits()["backoff_minutes"]
    start = _moment(state.get("started_at")) or now
    capped = now - start >= timedelta(hours=24)
    _finish(run, token, state={"reason": reason, "attempts": attempts, "rejections": rejects,
        "started_at": start.isoformat(), "next_at": (now + timedelta(minutes=delays[min(attempts - 1, len(delays) - 1)])).isoformat(),
        "capped": capped})
    if rejected and rejects == 2:
        from .founder_journey_alerts import queue_founder_journey_alert
        queue_founder_journey_alert(organization=run.organization, run=run, alert_reason="merge_rejected_twice", slot=run.run_id)
    logger.info("%s run_id=%s", reason, run.run_id)
    return reason


def _authority(run, *, watch=False):
    payload = scoped_run_contract(run)
    if watch:
        payload = {key: value for key, value in payload.items() if key not in {"expected_source_sha", "source_sha", "repo_head_sha", "last_scanned_sha", "configuration_revision"}}
    kind = _mapping(_mapping(run.result).get("publish_pr_v1")).get("kind")
    with authority_guard(payload, action="read" if watch else "setup" if kind in {"setup", "scaffold", "proxy"} else "merge") as connection:
        if (connection.organization_id != run.organization_id
                or connection.github_repo.casefold() != run.github_repo.casefold()
                or connection.organization.domain.casefold() != run.domain.casefold()):
            raise WebsiteAuthorityError("website_connection_changed", "The publication belongs to a different website.")
        return connection


def _recheck(run, token, receipt, *, now, observed=False):
    """Reload current authority and approver rights before mutation or attestation."""
    from organizations.models import Organization
    from .models import OrganizationContentConfig
    from .website_models import WebsiteConnection
    from workflow_runs.models import ContentFactoryRun
    connection = _authority(run, watch=observed)
    with transaction.atomic():
        org = Organization.objects.select_for_update().get(pk=run.organization_id)
        locked_connection = WebsiteConnection.objects.select_for_update().get(pk=connection.pk)
        if any(getattr(locked_connection, key) != getattr(connection, key) for key in (
                "generation", "configuration_version", "state", "installation_id", "repository_id", "authorized_by_id")):
            raise WebsiteAuthorityError("website_connection_changed", "The website changed before merging.")
        OrganizationContentConfig.objects.select_for_update().filter(organization=org).first()
        current = ContentFactoryRun.objects.select_for_update().get(pk=run.pk)
        current.organization = org
        state = _state(current)
        if (not getattr(settings, "ARTICLES_MERGE_WORKER_ENABLED", False) or not foundation.p2_applies(org)
                or current.organization_id != org.pk or current.status in {"cancelled", "denied"} or current.result.get("merge_v1")
                or (state.get("manual") is True and not observed)
                or state.get("claim") != token or (_moment(state.get("lease_until")) or now) <= now
                or _mapping(current.result).get("publish_pr_v1") != receipt
                or foundation.run_merge_policy(current) != foundation.BACKEND_WORKER):
            raise WebsiteAuthorityError("website_run_changed", "The publication changed before merging.")
        consent, approval = _consent(current, receipt)
        if approval is None:
            raise WebsiteAuthorityError("website_run_changed", "The publication consent changed before merging.")
    require_unlocked_remote_call()
    return connection, consent, approval


def merge_run(run, token, *, now):
    """Evaluate one claimed stamped run; GitHub's exact-head CAS is the only merge."""
    receipt = deepcopy(_mapping(_mapping(run.result).get("publish_pr_v1")))
    try:
        # A merge (including a manual GitHub merge) moves the base head. Read
        # that existing effect under current identity, without granting a new
        # mutation lease on a replacement source. _recheck stays strict below.
        connection = _authority(run, watch=True)
        if not _owned(receipt, connection):
            return _wait(run, token, "merge_unowned_pr", now=now)
        from .website_setup_merge_policy import p2_setup_example_candidate_allowed
        if not p2_setup_example_candidate_allowed(run):
            return _wait(run, token, "merge_setup_example_owner_required", now=now)
        if _state(run).get("capped"):
            return _wait(run, token, "merge_capped", now=now)
        if receipt["kind"] == "article" and _mapping(run.result).get("article_approval_v1", {}).get("source") != "publish_reviewed":
            approval = _copy_article_approval(run)
            if approval is not None:
                # Keep claim ownership while recording the immutable approval.
                from workflow_runs.models import ContentFactoryRun
                with transaction.atomic():
                    current = ContentFactoryRun.objects.select_for_update().get(pk=run.pk)
                    from .article_publish_approval import article_publish_approval_receipt_matches
                    if _state(current).get("claim") != token or not article_publish_approval_receipt_matches(current):
                        return _wait(run, token, "merge_waiting_approval", now=now)
                    _update_locked(current, {**_mapping(current.result), "article_approval_v1": approval})
                    run.result = current.result
        consent, approval = _consent(run, receipt)
        if approval is None:
            return _wait(run, token, consent, now=now)
        read_token = _token(connection, checks=True)
        pull = _api(connection, read_token, "GET", f"/pulls/{receipt['number']}")
        if not isinstance(pull, dict) or not _owned(receipt, connection, pull):
            return _wait(run, token, "merge_unowned_pr", now=now)
        if _mapping(pull.get("head")).get("sha") != receipt["head_sha"]:
            return _ask(run, token, receipt, "merge_head_moved", now=now)
        if pull.get("merged") is True:
            _, consent, approval = _recheck(run, token, receipt, now=timezone.now(), observed=True)
            merged = {"sha": pull.get("merge_commit_sha"), "merged": True}
            method = "observed"
        else:
            if _state(run).get("manual") is True:
                return _ask(run, token, receipt, "merge_manual_owner_required", now=now)
            if pull.get("state") != "open" or pull.get("mergeable") is False:
                return _wait(run, token, "merge_rejected", now=now, rejected=True)
            required, unknown, reviews = _policy(connection, read_token)
            if reviews:
                return _ask(run, token, receipt, "merge_review_required", now=now, code="BRANCH_PROTECTION_APPROVAL_REQUIRED")
            verification = _mapping(_mapping(run.result).get("build_verification_v1"))
            if (unknown or not required) and not (verification.get("sha") == receipt["head_sha"] and verification.get("status") == "passed" and verification.get("at")):
                from .vibe_marketing_views import _call_content_factory_run_action
                _call_content_factory_run_action(run_id=run.run_id, action="verify-head", workflow=run.workflow,
                    payload={"sha": receipt["head_sha"], "pr_number": receipt["number"]},
                    transport_errors_are_pending=True)
                return _wait(run, token, "merge_waiting_verification", now=now)
            checks = _checks(connection, read_token, receipt["head_sha"], required)
            if checks != "passed":
                return _wait(run, token, "merge_checks_" + checks, now=now, rejected=checks == "failed")
            metadata = _api(connection, read_token, "GET", "")
            if not isinstance(metadata, dict) or metadata.get("id") != connection.repository_id:
                return _wait(run, token, "merge_unowned_pr", now=now)
            method = next((name for name in ("squash", "merge", "rebase") if metadata.get("allow_" + name + "_merge") is True), None)
            if method is None:
                return _wait(run, token, "merge_method_unavailable", now=now)
            connection, consent, approval = _recheck(run, token, receipt, now=timezone.now())
            write_token = _token(connection)
            # Token mint can take time or observe a changed grant. Recheck all
            # local authority and rights after minting and before the mutation.
            _recheck(run, token, receipt, now=timezone.now())
            merged = _api(connection, write_token, "PUT", f"/pulls/{receipt['number']}/merge",
                body={"sha": receipt["head_sha"], "merge_method": method})
            if not isinstance(merged, dict) or merged.get("merged") is not True:
                return _wait(run, token, "merge_rejected", now=now, rejected=True)
        if not SHA.fullmatch(str(merged.get("sha") or "")):
            raise ProviderError(0)
        record = {"merged_at": pull.get("merged_at") or now.isoformat(), "merge_sha": merged["sha"],
            "head_sha": receipt["head_sha"], "method": method, "consent": consent,
            "approved_by_user_id": approval["approved_by_user_id"]}
        _finish(run, token, state={"reason": "merged", "watch_next_at": (now + timedelta(minutes=1)).isoformat()},
            fields={"merge_v1": record, "merge_status": "merged", "merge_sha": merged["sha"]})
        return "merged"
    except ProviderError as exc:
        if str(exc) == "review_required" and receipt.get("number"):
            return _ask(run, token, receipt, "merge_review_required", now=now, code="BRANCH_PROTECTION_APPROVAL_REQUIRED")
        return _wait(run, token, "merge_provider_wait", now=now, rejected=exc.status in {405, 409, 422})
    except Exception as exc:
        logger.warning("merge_worker_wait run_id=%s error_type=%s", run.run_id, type(exc).__name__)
        return _wait(run, token, "merge_authority_wait", now=now)


def run_merge_sweep(now=None, limit=None):
    """Merge at most the configured number of claimed P2 pull requests per tick."""
    if not getattr(settings, "ARTICLES_MERGE_WORKER_ENABLED", False):
        return {"checked": 0, "merged": 0}
    from workflow_runs.models import ContentFactoryRun
    from django.db.models import Q
    now = now or timezone.now()
    ceiling = max(0, min(int(limit if limit is not None else getattr(settings, "ARTICLES_MERGE_WORKER_LIMIT", 20)), 100))
    checked = merged = 0
    candidates = ContentFactoryRun.objects.filter(result__has_key="publish_pr_v1", organization__isnull=False).exclude(
        result__has_key="merge_v1").exclude(status__in=["cancelled", "denied"])
    candidates = candidates.filter(Q(result__self_heal_v1__merge_worker__next_at__isnull=True)
        | Q(result__self_heal_v1__merge_worker__next_at__lte=now.isoformat()))
    for identifier in candidates.order_by("pk").values_list("pk", flat=True)[:max(ceiling * 10, ceiling)]:
        if checked >= ceiling:
            break
        claim = _claim(identifier, now=now)
        if claim:
            checked += 1
            merged += merge_run(*claim, now=now) == "merged"
    return {"checked": checked, "merged": merged}


def deployment_evidence(connection, token, sha):
    """Recognize provider evidence only from this exact production commit."""
    statuses = _api(connection, token, "GET", f"/commits/{sha}/status?per_page=100")
    for row in _mapping(statuses).get("statuses") or []:
        if row.get("state") != "success":
            continue
        context = str(row.get("context") or "").lower()
        provider = "vercel" if "vercel" in context else "netlify" if "netlify" in context else "cloudflare" if "cloudflare pages" in context else None
        if provider:
            return provider
    deployments = _api(connection, token, "GET", f"/deployments?sha={sha}&per_page=30")
    if not isinstance(deployments, list):
        raise ProviderError(0)
    for deployment in deployments[:30]:
        if (not isinstance(deployment, dict) or deployment.get("sha") != sha or deployment.get("production_environment") is not True
                or type(deployment.get("id")) is not int):
            continue
        label = str(deployment.get("environment") or "").lower()
        host = str(_mapping(deployment.get("creator")).get("login") or "").lower()
        provider = "vercel" if "vercel" in label + host else "netlify" if "netlify" in label + host else "cloudflare" if "cloudflare" in label + host else None
        if provider:
            states = _api(connection, token, "GET", f"/deployments/{deployment['id']}/statuses?per_page=1")
            if isinstance(states, list) and states and states[0].get("state") == "success":
                return provider
    return None


def watch_run(run, token, *, now):
    """Check a safe live route after exact-commit deploy proof, bounded to 48 h."""
    merge = _mapping(run.result.get("merge_v1"))
    start = _moment(merge.get("merged_at"))
    if not start or not SHA.fullmatch(str(merge.get("merge_sha") or "")):
        _finish(run, token, state={"watch_reason": "invalid_merge_receipt"})
        return "invalid"
    elapsed = (now - start).total_seconds() / 60
    if elapsed >= 48 * 60:
        _finish(run, token, state={"watch_reason": "deadline", "watch_next_at": None})
        return "deadline"
    provider = _state(run).get("deploy_provider")
    try:
        connection = _authority(run, watch=True)
        provider = provider or _mapping(_mapping(connection.capabilities).get("deploymentFingerprint")).get("provider")
        provider = deployment_evidence(connection, _token(connection, checks=True), merge["merge_sha"]) or provider
        route = str(run.result.get("article_url") or run.result.get("url") or "")
        if not route:
            path = str(run.result.get("route_path") or run.result.get("path") or "")
            if path.startswith("/") and not path.startswith("//"):
                route = "https://" + run.domain + path
        if provider and route:
            from .website_live_fetch import fetch_live_route
            require_unlocked_remote_call()
            body, _ = fetch_live_route(route, run.domain)
            if body:
                _finish(run, token, state={"watch_reason": "live", "watch_next_at": None},
                    fields={"deploy_v1": {"live_at": now.isoformat(), "url": route, "provider": provider}})
                return "live"
    except Exception as exc:
        logger.info("deployment_watch_wait run_id=%s error_type=%s", run.run_id, type(exc).__name__)
    next_minute = next((minute for minute in POLL_MINUTES if minute > elapsed), None)
    state = {"watch_reason": "waiting", "deploy_provider": provider,
        "watch_next_at": (start + timedelta(minutes=next_minute) if next_minute else now + timedelta(hours=1)).isoformat()}
    fields = {}
    if elapsed >= 30:
        fields = {"error_code": "DEPLOYMENT_NOT_DETECTED", "failure_class": "waiting"}
        if not _state(run).get("deploy_alerted"):
            from .founder_journey_alerts import queue_founder_journey_alert
            queue_founder_journey_alert(organization=run.organization, run=run, alert_reason="deploy_not_detected", slot=run.run_id)
        state["deploy_alerted"] = True
    _finish(run, token, state=state, fields=fields)
    return "waiting"


def run_deployment_watch_sweep(now=None, limit=None):
    """Poll worker merges independently of the legacy deployment-detection flag."""
    if not getattr(settings, "ARTICLES_DEPLOYMENT_WATCH_SWEEP_ENABLED", False):
        return {"checked": 0, "live": 0}
    from workflow_runs.models import ContentFactoryRun
    from django.db.models import Q
    now = now or timezone.now()
    ceiling = max(0, min(int(limit if limit is not None else 20), 100))
    checked = live = 0
    candidates = ContentFactoryRun.objects.filter(result__has_key="merge_v1", organization__isnull=False).exclude(result__has_key="deploy_v1")
    candidates = candidates.filter(Q(result__self_heal_v1__merge_worker__watch_next_at__isnull=True)
        | Q(result__self_heal_v1__merge_worker__watch_next_at__lte=now.isoformat())).filter(
        Q(result__self_heal_v1__merge_worker__watch_reason__isnull=True) | ~Q(result__self_heal_v1__merge_worker__watch_reason="deadline"))
    for identifier in candidates.order_by("pk").values_list("pk", flat=True)[:max(ceiling * 10, ceiling)]:
        if checked >= ceiling:
            break
        claim = _claim(identifier, now=now, watch=True)
        if claim:
            checked += 1
            live += watch_run(*claim, now=now) == "live"
    return {"checked": checked, "live": live}


def record_owner_approval(*, run, organization, user, choice):
    """Persist head-bound setup/scaffold/proxy consent only for the current owner."""
    if foundation.run_merge_policy(run) != foundation.BACKEND_WORKER:
        return True
    from workflow_runs.models import ContentFactoryRun
    from organizations.models import Organization
    with transaction.atomic():
        org = Organization.objects.select_for_update().get(pk=organization.pk)
        current = ContentFactoryRun.objects.select_for_update().get(pk=run.pk)
        receipt = _mapping(_mapping(current.result).get("publish_pr_v1"))
        if (current.organization_id != org.pk
                or receipt.get("kind") not in {"setup", "scaffold", "proxy"}
                or not SHA.fullmatch(str(receipt.get("head_sha") or ""))
                or not _approver(org, getattr(user, "pk", None), owner_only=True)):
            return False
        state = deepcopy(_state(current))
        state.update(next_at=timezone.now().isoformat(), manual=choice == "manual")
        state.pop("claim", None)
        state.pop("lease_until", None)
        result = {**_mapping(current.result), "self_heal_v1": {
            **_mapping(_mapping(current.result).get("self_heal_v1")), "merge_worker": state}}
        record = {"version": 1, "kind": receipt["kind"], "pr_url": receipt.get("pr_url"),
            "head_sha": receipt["head_sha"], "approved_by_user_id": user.pk,
            "approved_at": timezone.now().isoformat()}
        _update_locked(current, {**result, "owner_merge_approval_v1": record})
        run.result = current.result
        return True


def run_fields(run, *, organization):
    """Add P2 copy/outcome fields without changing a legacy run's payload."""
    if not foundation.p2_applies(organization):
        return {}
    from .registry_policy import copy_id_for, effective_entry, wire_action_id
    result = _mapping(run.result)
    code = result.get("error_code")
    fields = {}
    copy = copy_id_for(code, organization=organization, terminal=run.status in {"failed", "blocked", "cancelled", "denied"},
        self_heal=_mapping(result.get("self_heal_v1")))
    if copy:
        fields["copyId"] = copy
    entry = effective_entry(code, organization)
    action = _mapping(result.get("user_action"))
    if entry and action and entry.get("action_id"):
        fields["userAction"] = {**action, "id": wire_action_id(entry["action_id"])}
    if copy and _state(run).get("reason") == "merge_waiting_claims":
        fields["copyId"] = "ask_check_highlighted"
        fields["userAction"] = {"id": wire_action_id("review_draft")}
    if _state(run).get("deploy_alerted"):
        from .website_deployment import PLAIN_HOST_LINES
        provider = _state(run).get("deploy_provider")
        if provider in PLAIN_HOST_LINES:
            fields["deployHost"] = {"vercel": "Vercel", "netlify": "Netlify", "cloudflare": "Cloudflare Pages"}[provider]
    try:
        outcomes = importlib.import_module("content_factory.no_delivery_refunds")
    except ImportError:
        return fields
    for key, name in (("pointsOutcome", "points_outcome"), ("cancelOutcome", "cancel_outcome")):
        provider = getattr(outcomes, name, None)
        if provider:
            value = provider(run)
            if value is not None:
                fields[key] = value
    return fields
