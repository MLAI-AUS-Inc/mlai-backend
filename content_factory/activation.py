"""Evidence-based Articles activation shared by bootstrap and admission gates."""
from __future__ import annotations

import json
from datetime import timedelta
from urllib.parse import urlsplit

from django.utils import timezone
from django.utils.dateparse import parse_datetime

from content_factory.article_setup_reset import article_setup_reset_marker
from content_factory.article_system import (
    PUBLISH_DISCONNECTED_KEY, is_directly_publishable_target,
)
from content_factory.billing import (
    get_content_factory_article_cost_points,
    get_content_factory_content_island_topic_cost_points,
)

VERIFICATION_MAX_AGE = timedelta(days=7)


def mapping(value):
    """Read persisted JSON mappings without trusting arbitrary strings."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return {}
        return value if isinstance(value, dict) else {}
    return {}


def github_account_state(config, *, actor_ids=None, installations=(), now=None):
    """Distinguish saved account authorization from verified repository access."""
    now = now or timezone.now()
    owner = str(getattr(config, "connected_slack_user_id", "") or "").strip()
    owned = config is not None and (actor_ids is None or owner in actor_ids)
    installation_id = str(getattr(config, "github_installation_id", "") or "") if owned else ""
    matching = [row for row in installations if not installation_id or str(row.installation_id) == installation_id]
    token = bool(owned and getattr(config, "github_token_encrypted", None))
    refresh = bool(owned and getattr(config, "github_refresh_token_encrypted", None))
    saved = bool(token or installation_id or matching)
    expires = getattr(config, "github_token_expires_at", None) if owned else None
    live_token = token and (expires is None or expires > now + timedelta(minutes=5))
    status = "connected" if live_token else "checking" if (installation_id or matching or refresh) else "needs_action" if saved else "not_connected"
    label = getattr(config, "github_user_name", "") if owned else ""
    if not label and matching:
        label = matching[0].github_user_name or matching[0].account_login
    return {"status": status, "saved": saved, "accountLabel": label or None, "owned": owned}


def integration_evidence(config, latest_runs=(), *, setup_gate=None, now=None):
    """Require a safe target and completed verification scoped to the chosen repo.

    Scaffold flags, setup merges, public listings and historical article runs are
    presentation data. A fresh default-branch scan carrying a ready verdict is the
    authority. Runtime repository probing additionally verifies the current HEAD.
    """
    now = now or timezone.now()
    connection = getattr(config, "website_connection", None)
    if connection is not None:
        return durable_integration_evidence(config, connection, now=now)
    raw = mapping(getattr(config, "article_system", None))
    pending = mapping(raw.get("pending_article_system_setup"))
    repo = str(getattr(config, "github_repo", "") or "").strip()
    gate = setup_gate or {}
    route = str(gate.get("routePath") or pending.get("routePath") or raw.get("route_path") or "").strip()
    scan = mapping(getattr(config, "scan_summary", None))
    targets = getattr(config, "publish_targets", None) or []
    default_target_id = str(getattr(config, "default_publish_target_id", "") or "").strip()
    target = next((item for item in targets if isinstance(item, dict)
                   and (not default_target_id or str(item.get("target_id") or item.get("id") or "") == default_target_id)
                   and is_directly_publishable_target(item)
                   and item.get("supported", True) is not False and not item.get("provisional") and mapping(item.get("seed_detail_proof")).get("status") != "structural"), None)
    if target and not route:
        route = str(target.get("route_path") or target.get("public_path") or mapping(target.get("registration_strategy")).get("route_path") or str(target.get("route_template") or "").split("{")[0] or "").strip()
    if not route:
        route = str(raw.get("directory_name") or "articles").strip()
    if "://" in route:
        route = urlsplit(route).path
    route = "/" + route.strip("/")
    base = {"verified": False, "routePath": route, "verifiedAt": None, "repo": repo, "branch": "", "sha": ""}
    if not repo:
        return {**base, "reasonCode": "repository_required"}
    if article_setup_reset_marker(raw) or raw.get(PUBLISH_DISCONNECTED_KEY):
        return {**base, "reasonCode": "integration_required"}
    if gate.get("setupBlocked"):
        return {**base, "reasonCode": "setup_pr_unmerged"}
    if not target:
        return {**base, "reasonCode": "integration_required"}
    selected = None
    for run in latest_runs:
        if getattr(run, "workflow", "") not in {"repo_scan", "content_factory_scan"} or str(getattr(run, "status", "")) != "completed":
            continue
        result = mapping(getattr(run, "result", None))
        nested = mapping(result.get("result"))
        request = mapping(getattr(run, "run_request", None))
        scan_repo = str(result.get("github_repo") or nested.get("github_repo") or request.get("github_repo") or getattr(run, "github_repo", "") or "")
        if scan_repo.lower() != repo.lower():
            continue
        selected = {**request, **nested, **result}
        selected.setdefault("completed_at", getattr(run, "updated_at", None))
        break
    if selected is None:
        # Never combine verdicts from two scans: a newer scoped scan without
        # readiness must not inherit a previous repository's ready flag.
        selected = mapping(raw.get("scan")) or scan
    scan_repo = str(selected.get("github_repo") or selected.get("githubRepo") or "")
    if scan_repo.lower() != repo.lower():
        return {**base, "reasonCode": "verification_required"}
    readiness = mapping(selected.get("article_system_readiness")) or mapping(mapping(selected.get("scaffold_plan")).get("article_system_readiness"))
    ready = readiness.get("ready") is True and readiness.get("safe_publish_route", True) is not False
    if not ready:
        return {**base, "reasonCode": "integration_required"}
    scan_targets = selected.get("publish_targets")
    if not isinstance(scan_targets, list) or target not in scan_targets:
        return {**base, "reasonCode": "verification_required"}
    verified_at = selected.get("scan_completed_at") or selected.get("completed_at") or selected.get("completedAt") or getattr(config, "last_scanned_at", None)
    if isinstance(verified_at, str):
        verified_at = parse_datetime(verified_at)
    if verified_at and timezone.is_naive(verified_at):
        verified_at = timezone.make_aware(verified_at)
    if not verified_at or verified_at > now + timedelta(minutes=5) or now - verified_at > VERIFICATION_MAX_AGE:
        return {**base, "reasonCode": "verification_stale"}
    if gate.get("setupMerged"):
        setup_time = gate.get("mergedAt")
        if isinstance(setup_time, str):
            setup_time = parse_datetime(setup_time)
        if setup_time and timezone.is_naive(setup_time):
            setup_time = timezone.make_aware(setup_time)
        if not gate.get("published") or (setup_time and verified_at < setup_time):
            return {**base, "reasonCode": "verification_required"}
    branch = str(selected.get("default_branch") or selected.get("defaultBranch") or "")
    sha = str(selected.get("default_branch_sha") or selected.get("defaultBranchSha") or selected.get("repo_head_sha") or getattr(config, "last_scanned_sha", "") or "")
    if not branch or not sha:
        return {**base, "reasonCode": "verification_required"}
    current_sha = str(getattr(config, "last_scanned_sha", "") or "")
    if current_sha and current_sha != sha:
        return {**base, "reasonCode": "verification_stale"}
    return {**base, "verified": True, "verifiedAt": verified_at.isoformat(), "branch": branch, "sha": sha, "reasonCode": ""}


def live_deployment_verified(connection, target, *, now=None):
    """Require the current target's exact, fresh, authenticated live receipt."""
    if target is None:
        return False
    now = now or timezone.now()
    contract = mapping(target.contract)
    marker = mapping(contract.get("live_marker"))
    if not contract.get("contract_digest") or not marker.get("value"):
        return False
    operation = connection.operations.filter(generation=connection.generation, action="deployment-verify", state="completed",
        payload__source_sha=connection.verified_sha, payload__target_id=target.target_key).order_by("-created_at").first()
    receipt = mapping(operation.receipt) if operation else {}
    stamp = parse_datetime(str(receipt.get("checked_at") or ""))
    return bool(receipt.get("status") == "passed" and receipt.get("source_sha") == connection.verified_sha
        and receipt.get("connection_generation") == connection.generation and receipt.get("target_id") == target.target_key
        and receipt.get("contract_digest") == contract["contract_digest"] and receipt.get("artifact_digest") == marker["value"]
        and receipt.get("public_url") and stamp and not timezone.is_naive(stamp)
        and now - VERIFICATION_MAX_AGE <= stamp <= now + timedelta(minutes=5))


def durable_integration_evidence(config, connection, *, now=None):
    """Use current-generation target proof, never legacy ready flags after binding."""
    from .website_contract import SHA_PATTERN
    from .website_rollout import repository_write_policy
    now = now or timezone.now()
    repo = str(getattr(config, "github_repo", "") or "")
    base = {"verified": False, "routePath": "/articles", "verifiedAt": None, "repo": repo,
            "branch": connection.branch, "sha": connection.verified_sha}
    if repo.casefold() != connection.github_repo.casefold():
        return {**base, "reasonCode": "repository_changed"}
    if connection.state != "connected":
        return {**base, "reasonCode": "website_publishing_paused" if connection.state == "paused" else "website_disconnected"}
    if not repository_write_policy(connection.organization.domain)["allowed"]:
        return {**base, "reasonCode": "website_writes_paused"}
    key = str(getattr(config, "default_publish_target_id", "") or "")
    targets = connection.targets.filter(generation=connection.generation)
    target = targets.filter(target_key=key).first() if key else None
    if target and target.adapter == "custom_contract_v1":
        return {**base, "reasonCode": "publishing_adapter_required"}
    if connection.app_root or any(item.get("code") in {"APPLICATION_ROOT_VERIFICATION_REQUIRED", "SETUP_BRANCH_VERIFICATION_REQUIRED"} for item in connection.blockers):
        return {**base, "reasonCode": "integration_required"}
    if any(item.get("code") == "repository_source_changed" for item in connection.blockers):
        return {**base, "reasonCode": "repository_source_changed"}
    if not mapping(connection.capabilities).get("publishingReady"):
        return {**base, "reasonCode": "integration_required"}
    if (target is None or not target.verified_at or not mapping(target.capabilities).get("publishingReady")
            or not SHA_PATTERN.fullmatch(connection.verified_sha or "") or target.source_sha != connection.verified_sha
            or not connection.branch):
        return {**base, "reasonCode": "verification_required"}
    verified_at = connection.last_verified_at
    if (not verified_at or verified_at > now + timedelta(minutes=5) or now - verified_at > VERIFICATION_MAX_AGE
            or target.verified_at > now + timedelta(minutes=5) or now - target.verified_at > VERIFICATION_MAX_AGE):
        return {**base, "reasonCode": "verification_stale"}
    contract = mapping(target.contract)
    route = str(contract.get("route_path") or contract.get("public_path") or str(contract.get("route_template") or "").split("{")[0] or "/articles")
    return {**base, "verified": True, "publishingVerified": live_deployment_verified(connection, target, now=now), "routePath": "/" + route.strip("/"), "verifiedAt": verified_at.isoformat(), "reasonCode": ""}


def article_capabilities(config, *, domain="", account=None, evidence=None, repository_access=None):
    """Return the versioned, action-specific client projection, failing closed."""
    account = account or github_account_state(config)
    evidence = evidence or integration_evidence(config)
    access = repository_access or {}
    repo_selected = bool(str(getattr(config, "github_repo", "") or "").strip())
    writable = access.get("writable", access.get("verified"))
    ready = bool(account.get("owned") and evidence.get("verified") and access.get("verified") and writable
                 and access.get("branch") == evidence.get("branch") and access.get("sha") == evidence.get("sha"))
    code = "" if ready else "github_required" if not account.get("saved") else "repository_required" if not repo_selected else "github_write_required" if access.get("verified") and not writable else access.get("reasonCode") or evidence.get("reasonCode") or "github_verification_required"
    reasons = {
        "website_disconnected": "Reconnect your website before repository work.", "website_publishing_paused": "Resume website publishing before repository work.",
        "website_writes_paused": "Website changes are temporarily paused.", "repository_changed": "Review the selected website repository.",
        "github_required": "Connect GitHub to get started.", "repository_required": "Choose your website repository.",
        "integration_required": "Connect your articles page to start writing.", "setup_pr_unmerged": "Review and merge your articles setup.",
        "verification_required": "Verify your articles integration.", "verification_stale": "Check your website integration again.",
        "github_verification_required": "Checking your saved GitHub access.", "github_access_required": "Review GitHub access to your website.",
        "github_unavailable": "GitHub is temporarily unavailable. Try again shortly.",
        "github_write_required": "Grant repository write access before preparing or publishing articles.",
        "repository_source_changed": "The repository source changed. Scan and verify the current source.",
        "publishing_adapter_required": "The custom build is verified. Connect an article publishing adapter or write a portable draft.",
    }
    route = evidence.get("routePath") or "/articles"
    label = route.strip("/").split("/")[0].replace("-", " ").title() or "Articles"
    unit = get_content_factory_content_island_topic_cost_points(domain)
    return {"version": 1, "canResearch": bool(domain), "canGenerateArticle": ready, "canGeneratePortableDraft": bool(domain), "canPublishArticle": bool(ready and evidence.get("publishingVerified", True)),
            "stage": "ready" if ready else "unavailable" if code == "github_unavailable" else "github" if not account.get("saved") or code == "github_access_required" else "repository" if not repo_selected else "verifying" if code in {"verification_required", "verification_stale", "github_verification_required", "github_unavailable"} else "integration",
            "reasonCode": code, "reason": "" if ready else reasons.get(code, "Complete articles setup to start writing."),
            "githubConnected": bool(account.get("saved")), "accountStatus": "connected" if account.get("verified") or access.get("verified") else account.get("status", "not_connected"),
            "accountAccessVerified": bool(account.get("verified") or access.get("verified")), "repositoryWriteVerified": bool(access.get("verified") and writable),
            "repositorySelected": repo_selected, "repositoryAccessVerified": bool(access.get("verified")),
            "repositorySourceSha": access.get("sha") if access.get("verified") else None,
            "repositoryBranch": access.get("branch") if access.get("verified") else None,
            "integrationVerified": bool(evidence.get("verified")), "routePath": route, "surfaceLabel": label,
            "nextStep": "repository" if not repo_selected or not account.get("saved") or code.startswith("github_") else "articles",
            "verifiedAt": evidence.get("verifiedAt"),
            "prices": {"article": get_content_factory_article_cost_points(domain), "researchTopic": unit, "research": unit * 4, "islandResearch": unit, "automationResearch": unit * 3}}


def founder_context_for_domain(user, domain):
    """Resolve a persisted founder company by exact organization ownership."""
    from founder_tools.models import VibeRaisingProfile
    from founder_tools.services import FounderCompanyContext
    profile = VibeRaisingProfile.objects.filter(user=user, role=VibeRaisingProfile.ROLE_FOUNDER).first()
    company = profile.companies.select_related("organization").filter(organization__domain__iexact=domain).first() if profile else None
    if company is None:
        raise PermissionError("This founder does not own the selected startup website.")
    return FounderCompanyContext(profile=profile, company=company, organization=company.organization)
