"""The canonical, company-scoped, resumable website/articles journey (v2)."""

from .activation import mapping
from .website_contract import evidence_digest
from .website_discovery import discovery_snapshot
from .website_operations import deletion_epoch, operation_summary
from .activation import VERIFICATION_MAX_AGE
from django.utils import timezone
from datetime import timedelta


def fact(complete=False, *, code="", reason="", status=None, verified_at=None, operation=None):
    """Expose a finite state; a saved credential is never an active check."""
    return {"status": status or ("complete" if complete else "needs_action"), "reasonCode": "" if complete else code,
        "reason": "" if complete else reason, "verifiedAt": verified_at, "operationId": operation}


def project_journey(*, company_id, domain, website=None, capabilities=None, discovery=None, target=None, operation=None, epoch=0, proof=None, source_runs=None, ci_source_runs=None):
    """Project verified facts without interpreting legacy completion ticks."""
    website, capabilities, target = mapping(website), mapping(capabilities), mapping(target)
    discovery = discovery or discovery_snapshot({})
    target_contract = mapping(target.get("contract"))
    proof = mapping(proof)
    authoring_supported = target.get("adapter") != "custom_contract_v1" and target_contract.get("delivery_adapter") != "custom_contract_v1"
    verified = bool(capabilities.get("canGenerateArticle") and authoring_supported)
    access = bool(capabilities.get("repositoryAccessVerified"))
    account_access = bool(capabilities.get("accountAccessVerified", access))
    write_access = bool(capabilities.get("repositoryWriteVerified", access))
    connected = website.get("status") == "connected"
    policy = mapping(website.get("writePolicy"))
    write_allowed = connected and policy.get("allowed") is True
    deployment = mapping(target.get("deployment_receipt"))
    deployed = bool(deployment.get("status") == "passed" and deployment.get("source_sha") == website.get("verifiedSha")
        and deployment.get("connection_generation") == website.get("connectionGeneration") and deployment.get("target_id") == target.get("key")
        and deployment.get("public_url") and deployment.get("checked_at"))
    live_ready = verified and deployed
    reason_code = capabilities.get("reasonCode") or "integration_required"
    reason = capabilities.get("reason") or "Prepare and verify your articles integration."
    if not authoring_supported:
        reason_code = "publishing_adapter_required"
        reason = "The custom build is verified. Connect an article publishing adapter or write a portable draft."
    running = operation and operation.get("state") in {"pending", "running", "verifying", "applying"}
    op_id = operation.get("id") if operation else None
    adapter = target.get("adapter") or target_contract.get("delivery_adapter") or proof.get("verifiedAdapter")
    certified = bool(proof.get("buildVerified", verified))
    custom_certified = bool(certified and adapter == "custom_contract_v1")
    native_certified = bool(certified and adapter in {"react_article_system", "next_pages_router", "next_app_router", "react_router", "astro_content", "static_markdown", "hook_materialized", "react_component", "react_json_collection", "mdx_file", "markdown_file", "hook_bundle", "document_file", "stack_native", "registry_entry", "native_article_composer"})
    native_path = next((path for path in discovery.get("supportPaths", []) if isinstance(path, dict) and path.get("id") == "native"), {})
    current_source = capabilities.get("repositorySourceSha") or website.get("verifiedSha")
    inventory_stale = bool(discovery.get("complete") and current_source and discovery.get("sourceSha") != current_source)
    verification_stale = proof.get("verificationStale") is True
    prerequisites = {
        "account": fact(account_access, code="github_access_required", reason="Check saved GitHub access."),
        "repository": fact(access and bool(website.get("repositoryId")), code="repository_required", reason="Select and verify your website repository."),
        "inventory": fact(bool(discovery.get("complete")) and not inventory_stale,
            code="scan_stale" if inventory_stale else "scan_required", reason="Scan the current repository.", status="stale" if inventory_stale else None),
        "integration": fact(proof.get("integrationVerified", verified), code=reason_code, reason=reason,
            status="blocked" if not write_allowed and not proof.get("integrationVerified", verified) else None),
        "verification": fact(proof.get("buildVerified", verified), code="verification_stale" if verification_stale else reason_code,
            reason="Rerun native CI verification and attest the current source." if verification_stale else reason,
            status="stale" if verification_stale else None, verified_at=capabilities.get("verifiedAt")),
        "deployment": fact(deployed, code="deployment_verification_required", reason="Verify the deployed public articles route.", verified_at=deployment.get("checked_at")),
    }
    step_facts = [("startup", "Startup details", fact(bool(company_id))), ("github", "GitHub access", prerequisites["account"]),
        ("repository", "Website repository", prerequisites["repository"]), ("scan", "Repository inventory", prerequisites["inventory"]),
        ("integration", "Articles integration", prerequisites["integration"]),
        ("preview", "Build and render verification", prerequisites["verification"]),
        ("activation", "Live articles route", prerequisites["deployment"]), ("articles", "Write articles", fact(verified, code=reason_code, reason=reason))]
    steps = [{"id": key, "label": label, **value, "runId": operation.get("runId") if operation and key in {"scan", "integration", "preview"} and operation.get("action") == "workflow" else None} for key, label, value in step_facts]
    allowed = ["verify-access"] if website else []
    if account_access:
        allowed.append("github-revoke")
    if website:
        allowed += ["disconnect", "reset", "purge", "reconcile", "github-revoke"]
        if access:
            allowed += ["custom-contract", "ci-attestation"]
        allowed += ["scan", "pause"] if connected else ["reconnect"]
        if write_allowed and write_access:
            allowed.append("setup")
            if not verification_stale:
                allowed.append("verify")
        if website.get("hasMutationHistory"):
            allowed.append("cleanup")
        if proof.get("cleanupAwaitingDeployment") or operation and operation.get("action") == "cleanup" and operation.get("state") == "awaiting_deployment":
            allowed.append("verify-cleanup")
        if running and operation.get("action") == "workflow":
            allowed.append("cancel-operation")
    actions = [{"id": action, "label": action.replace("-", " ").capitalize(), "method": "POST",
        "path": "/api/v1/my-startup/vibe-marketing/" + ("scan" if action == "scan" else "article-system-setup" if action == "setup" else "website-connection/" + action),
        "enabled": True, "reasonCode": "", "requiresConfirmation": action in {"disconnect", "reset", "purge", "cleanup", "github-revoke"}} for action in dict.fromkeys(allowed)]
    for action in actions:
        if action["id"] == "verify-cleanup":
            action["operationId"] = proof.get("cleanupOperationId") or op_id
            action["params"] = {"operation_id": action["operationId"]}
    next_step = next((step for step in steps if step["status"] != "complete"), None)
    action_id = {"github": "verify-access", "repository": "verify-access", "scan": "scan", "integration": "setup",
        "preview": "ci-attestation" if verification_stale else "verify", "activation": "verify"}.get(next_step["id"] if next_step else "")
    next_action = {"id": action_id, "label": next_step["reason"] or next_step["label"], "step": next_step["id"]} if next_step and action_id in allowed else None
    repository = {"id": website.get("repositoryId"), "name": website.get("githubRepo", ""), "branch": website.get("branch", ""),
        "appRoot": website.get("appRoot", ""), "sourceSha": website.get("verifiedSha") or discovery.get("sourceSha"), "targetId": target.get("key"),
        "observedSourceSha": capabilities.get("repositorySourceSha"),
        "publicRoute": target_contract.get("route_path") or target_contract.get("public_path") or None,
        "contentPath": target_contract.get("content_path") or target_contract.get("content_dir") or None}
    return {"version": 2, "companyId": str(company_id), "domain": domain,
        "revision": evidence_digest({"website": website, "capabilities": capabilities, "discovery": discovery, "operation": operation}),
        "connectionId": website.get("connectionId"), "connectionGeneration": website.get("connectionGeneration"),
        "configurationRevision": website.get("configurationVersion", 0), "deletionEpoch": epoch, "repository": repository,
        "policy": {"allowed": write_allowed, "reasonCode": "" if write_allowed else reason_code, "reason": "" if write_allowed else reason},
        "reasonCode": "" if verified else reason_code, "reason": "" if verified else reason,
        "prerequisites": prerequisites, "steps": steps,
        "capabilities": {"canRead": access, "canScan": bool(access and connected), "canPrepare": "setup" in allowed,
            "canPreview": bool(mapping(website.get("capabilities")).get("previewSupported") and write_allowed),
            "canGeneratePortableDraft": bool(capabilities.get("canGeneratePortableDraft", company_id)), "canGenerateArticle": verified, "canOpenPublicationPr": verified,
            "canPublishArticle": live_ready, "canMerge": bool(write_allowed and proof.get("reviewedMergeReady")), "canDisconnect": bool(website), "canPurge": bool(website), "canCleanup": "cleanup" in allowed},
        "allowedActions": list(dict.fromkeys(allowed)), "actions": actions, "nextAction": next_action,
        "discovery": discovery, "operation": operation, "sourceRuns": source_runs or [], "ciSourceRuns": ci_source_runs or source_runs or [],
        "support": {"native": {"available": native_certified or native_path.get("status") == "available", "certified": native_certified,
                "reasonCode": "" if native_certified else "native_verification_required" if native_path.get("status") == "available" else "adapter_required",
                "reason": native_path.get("reason") or "A verified framework adapter is required."},
            "portable": {"available": bool(capabilities.get("canGeneratePortableDraft", company_id))},
            "customContract": {"available": bool(website and access), "certified": custom_certified, "reasonCode": "" if custom_certified else "reviewed_contract_required",
                "generationRequirements": ([] if mapping(website.get("capabilities")).get("generationReady") else ["reviewed_article_template", "live_artifact_marker"]) + ([] if authoring_supported else ["registered_publishing_adapter"]),
                "action": "custom-contract", "path": "/api/v1/my-startup/vibe-marketing/website-connection/custom-contract"},
            "customerCi": {"available": bool(website and access), "certified": False, "reasonCode": "ci_attestation_required",
                "action": "ci-attestation", "path": "/api/v1/my-startup/vibe-marketing/website-connection/ci-attestation"},
            "cms": {"available": False, "reasonCode": "adapter_required"}}}


def ci_source_run(row, connection, operation):
    """Project only an actual fenced custom verification child, not articles."""
    request = mapping(row.run_request)
    if (not operation or operation.payload.get("workflow") != "native_verification"
            or operation.generation != connection.generation or operation.connection_id != connection.pk
            or operation.state in {"cancelled", "denied", "deleted"}
            or request.get("operation_id") != str(operation.pk) or operation.payload.get("run_id") != row.run_id
            or operation.payload.get("source_run_id") != request.get("source_run_id")
            or not request.get("source_run_id")
            or request.get("operation_attempt") != operation.payload.get("attempt", 1)
            or request.get("deletion_epoch") != deletion_epoch(connection)):
        return None
    return {"runId": row.run_id, "workflow": "native_verification", "status": row.status,
        "operationId": request["operation_id"], "operationAttempt": request["operation_attempt"],
        "deletionEpoch": request["deletion_epoch"], "baselineSha": request.get("expected_source_sha"),
        "targetId": request.get("target_id"), "contractDigest": request.get("contract_digest"),
        "sourceRunId": request["source_run_id"], "connectionId": str(connection.pk),
        "connectionGeneration": connection.generation, "repositoryId": connection.repository_id}


def build_proof_fresh(connection, target, capabilities, *, now=None):
    """Check proof age and live source independently from publication policy."""
    now = now or timezone.now()
    if (target is None or target.source_sha != connection.verified_sha
            or capabilities.get("repositorySourceSha") not in (None, "", connection.verified_sha)
            or capabilities.get("repositoryBranch") not in (None, "", connection.branch)):
        return False
    target_stamp = target.verified_at
    if not target_stamp and mapping(mapping(target.contract).get("verification")).get("status") == "preview_verified":
        # A reviewed PR preview has build/browser proof but intentionally has
        # no publication verified_at until that source reaches the main branch.
        target_stamp = target.updated_at
    return all(stamp and now - VERIFICATION_MAX_AGE <= stamp <= now + timedelta(minutes=5)
        for stamp in (target_stamp, connection.last_verified_at))


def journey_for_context(context, config, *, capabilities=None):
    """Load current-generation facts only; authenticated account probes stay live."""
    from .website_connections import summary_for
    if capabilities is None:
        from .vibe_marketing_views import _article_capabilities_for_context
        capabilities = _article_capabilities_for_context(context, config)
    website = summary_for(config, company_id=context.company.pk)
    connection = config.website_connection
    discovery, target, operation, epoch, proof, source_runs, ci_source_runs = None, None, None, 0, {}, [], []
    if connection:
        website["hasMutationHistory"] = connection.repository_mutations.exists()
        from django.db.models import Q
        inventory_rows = connection.scan_snapshots.filter(generation=connection.generation).exclude(detector_version="github_head").filter(
            Q(evidence__repository_inventory__discovery_complete=True) | Q(evidence__repository_discovery__discovery_complete=True)
            | Q(evidence__scan_summary__repository_inventory__discovery_complete=True) | Q(evidence__scan_summary__repository_discovery__discovery_complete=True)
            | Q(evidence__scan_complete=True))
        snapshot = inventory_rows.order_by("-created_at").first()
        evidence = mapping(snapshot.evidence) if snapshot else {}
        discovery = discovery_snapshot({**mapping(evidence.get("scan_summary")), **evidence})
        if snapshot:
            discovery["sourceSha"] = snapshot.source_sha
        selected = connection.targets.filter(generation=connection.generation, target_key=config.default_publish_target_id).first() if config.default_publish_target_id else None
        preview_target = selected or connection.targets.filter(generation=connection.generation, source_sha=connection.verified_sha).order_by("-updated_at").first()
        deployment = connection.operations.filter(generation=connection.generation, action="deployment-verify", state="completed",
            payload__source_sha=connection.verified_sha, payload__target_id=config.default_publish_target_id).order_by("-created_at").first()
        if deployment and selected and (deployment.receipt.get("artifact_digest") != mapping(selected.contract.get("live_marker")).get("value")
                or deployment.receipt.get("contract_digest") != selected.contract.get("contract_digest")):
            deployment = None
        target = {"key": selected.target_key, "adapter": selected.adapter, "contract": selected.contract, "deployment_receipt": deployment.receipt if deployment else {}} if selected else None
        preview_proof = mapping(mapping(preview_target.contract).get("verification")) if preview_target else {}
        prepared = bool(preview_target and preview_target.source_sha == connection.verified_sha and preview_proof.get("status") in {"passed", "verified", "preview_verified"})
        fresh = prepared and build_proof_fresh(connection, preview_target, capabilities)
        proof = {"integrationVerified": prepared, "buildVerified": fresh, "verificationStale": prepared and not fresh,
            "verifiedAdapter": preview_target.adapter if fresh else None}
        cleanup = connection.operations.filter(generation=connection.generation, action="cleanup", state="awaiting_deployment").order_by("-created_at").first()
        if cleanup:
            proof.update(cleanupAwaitingDeployment=True, cleanupOperationId=str(cleanup.pk))
        proof["reviewedMergeReady"] = connection.repository_mutations.filter(generation=connection.generation, status="applied").exclude(head_sha="").exists() and bool(connection.capabilities.get("previewSupported"))
        latest = connection.operations.filter(generation=connection.generation).order_by("-created_at").first()
        operation = operation_summary(latest) if latest else None
        epoch = deletion_epoch(connection)
        from workflow_runs.models import ContentFactoryRun
        source_runs = [{"runId": row.run_id, "workflow": row.workflow, "status": row.status,
            "operationId": row.run_request.get("operation_id"), "operationAttempt": row.run_request.get("operation_attempt"),
            "deletionEpoch": row.run_request.get("deletion_epoch"), "baselineSha": row.run_request.get("expected_source_sha"),
            "connectionId": str(connection.pk), "connectionGeneration": connection.generation, "repositoryId": connection.repository_id}
            for row in ContentFactoryRun.objects.filter(organization=context.organization,
                workflow__in=["repo_scan", "content_factory_scan", "article_system_setup"],
                run_request__website_connection_id=str(connection.pk), run_request__connection_generation=connection.generation,
                run_request__operation_id__isnull=False, run_request__operation_attempt__isnull=False, run_request__deletion_epoch__isnull=False)
                .exclude(status__in=["cancelled", "denied"]).exclude(run_request__source_run_id__isnull=False).order_by("-updated_at")[:12]]
        verifier_operations = {str(row.pk): row for row in connection.operations.filter(generation=connection.generation,
            action="workflow", payload__workflow="native_verification").exclude(state__in=["cancelled", "denied", "deleted"])}
        verifier_runs = ContentFactoryRun.objects.filter(organization=context.organization,
                run_request__website_connection_id=str(connection.pk), run_request__connection_generation=connection.generation,
                run_request__source_run_id__isnull=False, run_request__operation_id__isnull=False,
                run_request__operation_id__in=list(verifier_operations),
                run_request__operation_attempt__isnull=False, run_request__deletion_epoch__isnull=False)
        ci_source_runs = [projection for row in verifier_runs.exclude(status__in=["cancelled", "denied"]).order_by("-updated_at")[:12]
            if (projection := ci_source_run(row, connection, verifier_operations.get(row.run_request.get("operation_id"))))] + source_runs
    return project_journey(company_id=context.company.pk, domain=context.organization.domain, website=website,
        capabilities=capabilities, discovery=discovery, target=target, operation=operation, epoch=epoch, proof=proof, source_runs=source_runs, ci_source_runs=ci_source_runs)
