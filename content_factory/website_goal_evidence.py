"""Collect milestone proofs from owning backend records and validators only."""

from datetime import timedelta

from .website_goal import DIGEST, SHA, make_proof, mapping, scope_for, stamp
from .website_contract import WebsiteAuthorityError


def _child(op, connection):
    from workflow_runs.models import ContentFactoryRun
    from .website_operations import validate_operation, deletion_epoch
    identifier = op.receipt.get("setup_run_id") or op.receipt.get("child_run_id")
    if not identifier:
        return None
    run = ContentFactoryRun.objects.filter(run_id=identifier, organization_id=connection.organization_id,
        workflow="article_system_setup").first()
    if not run:
        return None
    saved = mapping(run.run_request)
    if (saved.get("website_connection_id") != str(connection.pk)
            or saved.get("connection_generation") != connection.generation
            or saved.get("repository_id") != connection.repository_id
            or saved.get("deletion_epoch") != deletion_epoch(connection)
            or run.github_repo.casefold() != connection.github_repo.casefold()
            or run.domain.casefold() != connection.organization.domain.casefold()):
        return None
    try:
        original = validate_operation(connection, {**saved, "run_id": run.run_id})
        if not original or original.payload.get("run_id") != run.run_id:
            return None
    except (WebsiteAuthorityError, ValueError, TypeError, AttributeError):
        return None
    return run


def source_for(connection, journey):
    """Prefer the authenticated selected-branch observation over a stale baseline."""
    repository = mapping(journey.get("repository"))
    value = repository.get("observedSourceSha") or repository.get("sourceSha") or getattr(connection, "verified_sha", "")
    return value if isinstance(value, str) and SHA.fullmatch(value) else None


def collect_proofs(op, context, config, journey, *, now):
    """Read bounded trusted facts without writing, dispatching or granting consent."""
    from .activation import VERIFICATION_MAX_AGE
    from .website_discovery import discovery_snapshot
    from .website_journey import build_proof_fresh
    from .website_verification import current_ci_attestation
    from .website_prepare import _setup_preview_proven_for_review
    from .website_connections import _verified_setup_run_base

    connection = config.website_connection
    scope, source = scope_for(connection, context.company.pk), source_for(connection, journey)
    proofs = {}

    def put(name, kind, *, sha=None, run_id=None, evidence=None, at=now):
        if stamp(at):
            proofs[name] = make_proof(name, kind, scope=scope, source_sha=source,
                sha=sha, run_id=run_id, evidence=evidence or {}, at=at)

    prerequisites = mapping(journey.get("prerequisites"))
    if (connection.state == "connected" and journey.get("connectionId") == str(connection.pk)
            and journey.get("connectionGeneration") == connection.generation
            and all(mapping(prerequisites.get(key)).get("status") == "complete" for key in ("account", "repository"))):
        put("access", "repository_access", evidence={"installation_id": connection.installation_id,
            "repository_id": connection.repository_id, "branch": connection.branch})

    if not source:
        return proofs, source
    snapshot = connection.scan_snapshots.filter(generation=connection.generation, source_sha=source).exclude(
        detector_version="github_head").order_by("-created_at", "-pk").first()
    if snapshot:
        raw = mapping(snapshot.evidence)
        discovery = discovery_snapshot({**mapping(raw.get("scan_summary")), **raw})
        if (discovery.get("complete") is True and isinstance(snapshot.fingerprint, str)
                and DIGEST.fullmatch(snapshot.fingerprint)):
            put("understood", "repository_scan", sha=source, run_id=snapshot.run_id,
                evidence={"snapshot_id": str(snapshot.pk), "fingerprint": snapshot.fingerprint}, at=snapshot.created_at)

    target = connection.targets.filter(generation=connection.generation, target_key=config.default_publish_target_id,
        source_sha=source).first() if config.default_publish_target_id else None
    contract = mapping(getattr(target, "contract", None))
    contract_digest = contract.get("contract_digest")
    ci = current_ci_attestation(connection, target, capabilities={"repositorySourceSha": source,
        "repositoryBranch": connection.branch}) if target and source == connection.verified_sha else None
    deployment = None
    if target and isinstance(contract_digest, str) and DIGEST.fullmatch(contract_digest):
        verification = mapping(contract.get("verification"))
        if (verification.get("status") in {"passed", "verified", "preview_verified"}
                and target.connection_id == connection.pk and target.generation == connection.generation):
            put("integrated", "verified_target", sha=source, evidence={"target_id": target.target_key,
                "contract_digest": contract_digest}, at=target.updated_at)
        if ci and build_proof_fresh(connection, target, {"repositorySourceSha": source, "repositoryBranch": connection.branch}, now=now):
            put("previewed", "authenticated_hosted" if ci.get("attestation_origin") == "authenticated_hosted" else "authenticated_ci", sha=source, run_id=ci.get("run_id"), evidence=ci,
                at=ci.get("checked_at") or ci.get("verified_at"))
            deployment = connection.operations.filter(generation=connection.generation, action="deployment-verify", state="completed",
                payload__source_sha=source, payload__target_id=target.target_key).order_by("-created_at", "-pk").first()
            receipt = mapping(getattr(deployment, "receipt", None))
            marker = mapping(contract.get("live_marker"))
            checked = stamp(receipt.get("checked_at"))
            if (receipt.get("status") == "passed" and receipt.get("provider_verified") is True
                    and mapping(journey.get("capabilities")).get("canGenerateArticle") is True
                    and receipt.get("source_sha") == source and receipt.get("connection_generation") == connection.generation
                    and receipt.get("target_id") == target.target_key and receipt.get("contract_digest") == contract_digest
                    and receipt.get("ci_evidence_digest") == ci.get("evidence_digest")
                    and isinstance(ci.get("evidence_digest"), str) and DIGEST.fullmatch(ci["evidence_digest"])
                    and receipt.get("artifact_digest") == marker.get("value") == ci.get("artifact_digest")
                    and isinstance(marker.get("value"), str) and DIGEST.fullmatch(marker["value"])
                    and marker.get("kind") == "artifact_digest" and checked
                    and now - VERIFICATION_MAX_AGE <= checked <= now + timedelta(minutes=5)):
                put("live", "deployment_marker", sha=source, run_id=ci.get("run_id"), evidence=receipt, at=checked)

    child = _child(op, connection)
    if child:
        result, saved = mapping(child.result), mapping(child.run_request)
        head = result.get("branch_commit_sha")
        preview = mapping(result.get("directory_quality_verification"))
        exact = (isinstance(head, str) and SHA.fullmatch(head)
            and saved.get("expected_source_sha") == source
            and (_verified_setup_run_base(child, head, connection, automatic_approval=True)
                 or _setup_preview_proven_for_review(child, head)))
        if exact:
            put("integrated", "setup_candidate", sha=head, run_id=child.run_id,
                evidence={"branch": result.get("branch"), "source_sha": source, "head_sha": head}, at=preview.get("completed_at"))
            put("previewed", "setup_preview", sha=head, run_id=child.run_id,
                evidence=preview, at=preview.get("completed_at"))
        intent, response = mapping(result.get("publish_merge_intent")), mapping(result.get("merge_response"))
        # These fields are Django-owned: the receiver refuses worker introduction
        # or replacement. They are written by the existing guarded merge path.
        bound_intent = (intent.get("website_connection_id") == str(connection.pk)
            and intent.get("connection_generation") == connection.generation and intent.get("repository_id") == connection.repository_id
            and intent.get("run_id") == child.run_id and intent.get("base_branch") == connection.branch
            and intent.get("source_sha") == saved.get("expected_source_sha")
            and isinstance(intent.get("head_sha"), str) and SHA.fullmatch(intent["head_sha"]))
        merged = (bound_intent and response.get("merged") is True and response.get("sha") == source
            and result.get("merge_status") == "merged" and stamp(result.get("merged_at")))
        if bound_intent and (intent.get("source_sha") == source or merged):
            put("approved", "backend_approval", sha=intent["head_sha"], run_id=child.run_id, evidence=intent,
                at=result.get("merged_at") or intent.get("recorded_at"))
        if merged:
            put("merged", "backend_merge", sha=source, run_id=child.run_id,
                evidence={"intent": intent, "response": response}, at=result["merged_at"])
    elif "integrated" in proofs and "previewed" in proofs:
        # A currently certified selected-branch surface needs no new mutation.
        # This is an explicit no-change proof, never an invented owner approval.
        for name in ("approved", "merged"):
            put(name, "no_mutation_required", sha=source,
                evidence={"target_id": target.target_key, "contract_digest": contract_digest,
                          "ci_evidence_digest": ci.get("evidence_digest")},
                at=proofs["previewed"]["at"])
    return proofs, source
