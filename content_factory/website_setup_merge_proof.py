"""Carry a verified setup preview to the identical tree on the selected branch."""

from django.utils import timezone
from workflow_runs.models import ContentFactoryRun

from . import website_live_verification as live
from . import website_sealed_workflow as sealed
from . import website_source_carry
from .activation import mapping
from .incident_guards import sealed_contract
from .models import OrganizationContentConfig
from .website_connections import (
    _verified_setup_run_base, authority_guard, contract_for, observed_source_sha,
    require_unlocked_remote_call, scoped_run_contract,
)
from .website_contract import SHA_PATTERN, evidence_digest
from .website_hosted_build import enabled as hosted
from .website_models import WebsiteConnectionTarget
from .website_operations import deletion_epoch
from .website_publishing_inputs import has_candidate
from .website_reconciliation import commit_tree_sha


def _reviewed_target(run, website, selected_key):
    """Read only this setup's own default, exact render and original consent."""
    result = mapping(run.result)
    request = scoped_run_contract(run)
    key = result.get("default_publish_target_id")
    merge_receipt = mapping(result.get("merge_response"))
    head = result.get("branch_commit_sha")
    merge = result.get("merge_sha") or merge_receipt.get("sha")
    pull = mapping(merge_receipt.get("pull"))
    if (not isinstance(key, str) or not key or selected_key and selected_key != key
            or not all(isinstance(sha, str) and SHA_PATTERN.fullmatch(sha) for sha in (head, merge))
            or result.get("merge_status") != "merged" or result.get("status") != "merged"
            or merge_receipt.get("sha", merge) != merge
            or (pull and (pull.get("merge_commit_sha") != merge or pull.get("head_sha") != head))
            or request.get("website_connection_id") != str(website.pk)
            or request.get("connection_generation") != website.generation
            or request.get("repository_id") != website.repository_id
            or request.get("branch") != website.branch or request.get("app_root", "") != website.app_root
            or request.get("deletion_epoch", 0) != deletion_epoch(website)
            or run.github_repo.casefold() != website.github_repo.casefold()
            or run.domain.casefold() != website.organization.domain.casefold()):
        return None
    targets = result.get("publish_targets")
    if not isinstance(targets, list):
        return None
    matches = [row for row in targets if isinstance(row, dict) and row.get("target_id") == key]
    if len(matches) != 1:
        return None
    contract = matches[0]
    route = contract.get("route_path")
    if (not isinstance(route, str) or not route.startswith("/") or route.startswith("//")
            or route != result.get("route_path") or not sealed_contract(contract)
            or has_candidate(contract) or contract.get("supported") is False
            or contract.get("publish_capability") not in {"direct", "hook"}
            or not contract.get("delivery_adapter") or contract["delivery_adapter"] == "custom_contract_v1"
            or not _verified_setup_run_base(run, head, website, merged=True)):
        return None
    return head, merge, contract


def carry_setup_merge_proof(op, config, target):
    """Prepare a build-proven target. The live verifier owns connection readiness."""
    website = config.website_connection
    if (not website_source_carry.enabled() or not live.selected(website.organization)
            or hosted(website) or sealed.required(website, target) or website.state != "connected"
            or op.generation != website.generation or website.app_root):
        return target
    source = observed_source_sha(website)
    if not source or (target and target.source_sha == source and live.build_proof(target)):
        return target
    run_id = op.receipt.get("merged_setup_run_id")
    runs = ContentFactoryRun.objects.filter(run_id=run_id, organization_id=website.organization_id,
        workflow="article_system_setup", status="completed", approval_state="approved",
        run_request__website_connection_id=str(website.pk), run_request__connection_generation=website.generation)
    run = runs.first()
    candidate = _reviewed_target(run, website, config.default_publish_target_id) if run else None
    if not candidate or candidate[1] != source:
        return target
    head, merge, contract = candidate
    # Squash merges can have identical trees while a three-dot comparison
    # reports changes from the common ancestor. Read both exact tree objects.
    require_unlocked_remote_call()
    try:
        head_tree = commit_tree_sha(website, head)
        if head_tree != commit_tree_sha(website, merge):
            return target
    except Exception as exc:
        from .website_github_failure import github_failure
        raise github_failure(exc, legacy_code="github_source_unavailable",
            legacy_message="GitHub could not verify the merged setup files.", legacy_retryable=True, source=True) from exc

    binding = {**contract_for(website), "expected_source_sha": merge,
        "configuration_revision": website.configuration_version, "operation_id": str(op.pk),
        "operation_attempt": op.payload.get("attempt", 1), "deletion_epoch": deletion_epoch(website)}
    with authority_guard(binding, action="read", require_current_head=True) as current:
        active = current.operations.select_for_update().filter(pk=op.pk, action="prepare", state="running",
            attempts=op.attempts, receipt__merged_setup_run_id=run_id).exists()
        if not active or current.state != "connected" or observed_source_sha(current) != merge:
            return None
        run = runs.select_for_update().first()
        saved = OrganizationContentConfig.objects.select_for_update().get(website_connection=current)
        if not run or _reviewed_target(run, current, saved.default_publish_target_id) != candidate:
            return None
        key = contract["target_id"]
        locked = current.targets.select_for_update().filter(generation=current.generation, target_key=key).first()
        if sealed.required(current, locked) or (locked and live.build_proof(locked) and locked.source_sha == merge):
            return locked
        if locked is None:
            locked = WebsiteConnectionTarget(connection=current, generation=current.generation, target_key=key)
        locked.adapter = contract.get("delivery_adapter") or ""
        locked.adapter_version = contract.get("adapter_version") or ""
        locked.source_sha = merge
        locked.verified_at = timezone.now()
        locked.contract = {**contract, "verification": {**mapping(contract.get("verification")),
            "status": "passed", "source_sha": merge, "attestation_origin": "setup_merge_carry",
            "carried_from": head, "setup_run_id": run_id, "source_tree_sha": head_tree}}
        locked.capabilities = {"publishingReady": True}
        locked.save()
        return locked


def worker_publish_targets(config):
    """Project a carried setup proof from backend-owned build and deployment evidence."""
    targets = config.publish_targets
    key = config.default_publish_target_id
    matches = [row for row in targets or [] if mapping(row).get("target_id") == key]
    if not key or len(matches) != 1:
        return targets
    selected = matches[0]
    build = mapping(selected.get("verification"))
    if build.get("attestation_origin") != "setup_merge_carry":
        return targets

    website = config.website_connection
    if (website.state != "connected" or website.organization_id != config.organization_id
            or not website.verified_sha or observed_source_sha(website) != website.verified_sha):
        return targets
    target = website.targets.filter(generation=website.generation, target_key=key).first()
    if target is None or target.contract != selected or not live.accepted(website, target):
        return targets
    setup = ContentFactoryRun.objects.filter(
        run_id=build.get("setup_run_id"), organization_id=website.organization_id,
        workflow="article_system_setup", status="completed", approval_state="approved",
    ).first()
    reviewed = _reviewed_target(setup, website, key) if setup else None
    scope = contract_for(website)
    source = target.source_sha
    digest = selected.get("contract_digest")
    adapter = selected.get("delivery_adapter")
    if (not reviewed or reviewed[:2] != (build.get("carried_from"), source)
            or not SHA_PATTERN.fullmatch(str(build.get("source_tree_sha") or ""))
            or adapter != target.adapter
            or any(selected.get(field) != value for field, value in reviewed[2].items()
                   if field not in {"verification", "live_verification"})):
        return targets

    binding = {**scope, "source_sha": source, "target_id": key, "contract_digest": digest, "method": live.METHOD}
    operation = website.operations.filter(
        idempotency_key=f"{website.pk}:live-url:{evidence_digest(binding)}",
        generation=website.generation, action="deployment-verify", state="completed",
    ).first()
    receipt = mapping(operation.receipt) if operation else {}
    if (not operation or operation.payload != binding or receipt.get("status") != "passed"
            or receipt.get("attestation_origin") != live.ORIGIN
            or any(receipt.get(field) != binding[field] for field in (
                "source_sha", "target_id", "contract_digest", "connection_generation", "method",
            ))
            or receipt.get("artifact_digest") != live.artifact_digest(selected)
            or receipt.get("public_url") != live.live_proof(target).get("public_url")):
        return targets

    # This completed synchronous observation has a single execution attempt.
    proof = {**build, **scope, "target_id": key, "adapter_id": adapter,
        "operation_id": str(operation.pk), "operation_attempt": 1, "deletion_epoch": deletion_epoch(website),
        "contract_digest": digest,
        "evidence_digest": evidence_digest({"binding": binding, "build": build, "deployment": receipt})}
    return [{**row, "verification": proof} if row is selected else row for row in targets]
