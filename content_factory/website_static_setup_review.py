"""Read-only discovery of an approved static setup at the selected branch's merged commit."""
import logging
import re

from django.db.models import Q

from integrations.services.github_connections import read_bound_repository
from workflow_runs.models import ContentFactoryRun
from .models import OrganizationContentConfig
from .website_content_files import enabled, static_candidate
from .website_contract import SHA_PATTERN, WebsiteAuthorityError
from .website_reconciliation import _setup_pull_merged_as, commit_tree_sha
from .website_source_sync import read_head

logger = logging.getLogger(__name__)


def _runs(site):
    return ContentFactoryRun.objects.filter(organization_id=site.organization_id, workflow="article_system_setup",
        status="completed", approval_state="approved", github_repo__iexact=site.github_repo,
        run_request__website_connection_id=str(site.pk), run_request__connection_generation=site.generation,
        run_request__repository_id=site.repository_id).filter(Q(result__setup_ladder__tier="static_dropin")
            | Q(result__article_system_setup__setup_ladder__tier="static_dropin"))


def _selected(site):
    return site.state == "connected" and OrganizationContentConfig.objects.filter(organization_id=site.organization_id,
        website_connection_id=site.pk, github_repo__iexact=site.github_repo, website_connection__state="connected",
        website_connection__generation=site.generation, website_connection__branch=site.branch,
        website_connection__app_root=site.app_root, website_connection__repository_id=site.repository_id,
        website_connection__github_repo__iexact=site.github_repo, website_connection__installation_id=site.installation_id).exists()


def _for_run(site, run, merge_sha):
    from .vibe_marketing_views import _pull_request_number_from_run
    from .website_connections import contract_for, scoped_run_contract
    saved = scoped_run_contract(run)
    if any(saved.get(k) != v for k, v in contract_for(site).items()):
        return None
    data = run.result or {}
    setup = data.get("article_system_setup") or {}
    ladder = data.get("setup_ladder") or setup.get("setup_ladder") or {}
    source, head = ladder.get("source_sha"), ladder.get("head_sha")
    number = _pull_request_number_from_run(run)
    if (not isinstance(source, str) or not SHA_PATTERN.fullmatch(source) or source != saved.get("expected_source_sha")
            or not isinstance(head, str) or not SHA_PATTERN.fullmatch(head)
            or type(number) is not int or number < 1 or not static_candidate(data)):
        return None
    hint = {"setup_run_id": run.run_id, "pr_number": number, "source_sha": source, "reviewed_head_sha": head,
            "merge_sha": merge_sha, "branch": site.branch}
    intent, receipt = data.get("publish_merge_intent") or {}, data.get("merge_response") or {}
    if (receipt.get("merged") is True and receipt.get("sha") == merge_sha and data.get("merge_status") == "merged"
            and all(intent.get(k) == v for k, v in {**contract_for(site), "run_id": run.run_id, "pr_number": number,
                    "head_sha": head, "source_sha": source, "base_branch": site.branch}.items())):
        return hint
    # A founder can merge their reviewed draft pull request in GitHub. This
    # observation may locate the candidate; it cannot complete a prepare operation.
    pull = read_bound_repository(site, f"https://api.github.com/repos/{site.github_repo}/pulls/{number}")
    pull.raise_for_status()
    if (not _setup_pull_merged_as(site, pull.json(), number=number, head_sha=head, source_sha=merge_sha)
            or commit_tree_sha(site, head) != commit_tree_sha(site, merge_sha)):
        return None
    return hint


def current_review(site, sha, *, run_id=None, candidate=None, head_verified=False):
    """Return the six discovery fields for an exact approved, merged static candidate."""
    if not enabled() or not isinstance(sha, str) or not SHA_PATTERN.fullmatch(sha) or not _selected(site):
        return None
    runs = _runs(site)
    if run_id:
        runs = runs.filter(run_id=run_id)
    # Only the newest approved attempt is relevant when the scan has no identity yet.
    run = runs.order_by("-updated_at", "-pk").first()
    if not run:
        return None
    if candidate is not None:
        original = static_candidate(run.result)
        immutable = {k: v for k, v in candidate.items() if k not in {"provisional", "verification"}}
        if immutable != {k: v for k, v in original.items() if k != "verification"}:
            return None
    try:
        if not head_verified and read_head(site) != sha:
            return None
        hint = _for_run(site, run, sha)
    except Exception as exc:
        # Provider or saved evidence is unavailable. Leave it for a later scan.
        logger.warning("static_setup_review_unavailable connection=%s run=%s error_type=%s",
                       site.pk, run.run_id, type(exc).__name__)
        return None
    if not hint or not _selected(site):
        return None
    # Event-owned updates can preserve updated_at. Re-read the two immutable
    # inputs instead of using a timestamp as evidence of continuing approval.
    fresh = runs.filter(pk=run.pk).values("run_request", "result").first()
    if fresh and fresh["run_request"] == run.run_request and fresh["result"] == run.result:
        return hint
    return None


def validate_provisional_target(site, target, expected, data):
    """Fence a provisional registration before the config write and keep existing selections."""
    proof = target.get("verification")
    proof = proof if isinstance(proof, dict) else {}
    claimed = proof.get("static_setup_merge") or {}
    if (not expected or data.get("source_sha") != expected or target.get("provisional") is not True
            or proof.get("status") != "awaiting_live_proof" or proof.get("source_sha") != expected
            or not isinstance(claimed, dict) or not isinstance(claimed.get("setup_run_id"), str)
            or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,99}", claimed["setup_run_id"])
            or not SHA_PATTERN.fullmatch(str(claimed.get("tree_sha") or ""))
            or not re.fullmatch(r"[0-9a-f]{64}", str(claimed.get("listing_sha256") or ""))):
        raise WebsiteAuthorityError("website_configuration_changed", "Recheck the reviewed directory before registering it.")
    hint = current_review(site, expected, run_id=claimed["setup_run_id"], candidate=target, head_verified=True)
    if not hint or any(claimed.get(k) != v for k, v in hint.items()):
        raise WebsiteAuthorityError("website_configuration_changed", "The reviewed directory changed before registration.")
