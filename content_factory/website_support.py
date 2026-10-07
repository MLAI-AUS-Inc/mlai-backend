"""Owner-scoped executable custom-contract and customer CI support paths."""

import re
import hashlib
from django.utils import timezone

from .website_contract import WebsiteAuthorityError, connection_contract, safe_repository_path, sanitized_evidence


def validate_custom_contract(contract):
    """Reject source secrets and unreviewed/unbounded execution declarations."""
    if not isinstance(contract, dict) or contract.get("schema_version") != 1 or contract.get("adapter_id", "custom_contract_v1") != "custom_contract_v1" or contract.get("reviewed") is not True:
        raise WebsiteAuthorityError("reviewed_contract_required", "Supply a reviewed version 1 custom integration contract.", status=422)
    if contract != sanitized_evidence(contract):
        raise WebsiteAuthorityError("contract_secret_values_denied", "Declare environment variable names rather than secret values.", status=422)
    if contract.get("adapter_version", 1) != 1 or contract.get("runtime_family") not in {"node", "python", "ruby", "php", "go", "static", "other"}:
        raise WebsiteAuthorityError("contract_schema_invalid", "Choose a version 1 supported runtime declaration.", status=422)
    for key in ("runtime_version", "package_manager"):
        value = contract.get(key, "")
        if not isinstance(value, str) or len(value) > 100 or key == "runtime_version" and not value:
            raise WebsiteAuthorityError("contract_schema_invalid", "Use bounded runtime and package manager declarations.", status=422)
    lockfiles = contract.get("lockfiles", [])
    if not isinstance(lockfiles, list) or len(lockfiles) > 20 or any(not isinstance(path, str) for path in lockfiles):
        raise WebsiteAuthorityError("contract_path_invalid", "Lockfiles must be a bounded list of repository paths.", status=422)
    policy = contract.get("dependency_policy", {})
    if (not isinstance(policy, dict) or set(policy) - {"reviewed", "allow_lockfile_regeneration", "allow_legacy_peer_dependencies"}
            or any(not isinstance(value, bool) for value in policy.values())):
        raise WebsiteAuthorityError("contract_dependency_policy_invalid", "Use the reviewed boolean dependency policy fields.", status=422)
    root = safe_repository_path(contract.get("app_root", "."), allow_empty=True)
    if len(root) > 500:
        raise WebsiteAuthorityError("contract_path_invalid", "Application root is too long.", status=422)
    for path in [str(contract.get("content_path_pattern") or "").replace("{slug}", "example"), *(contract.get("lockfiles") or [])]:
        safe_repository_path(path)
    for key in ("route_template", "listing_route"):
        route = str(contract.get(key) or "")
        if not route.startswith("/") or route.startswith("//") or ".." in route.split("/"):
            raise WebsiteAuthorityError("contract_route_invalid", "Declare local absolute article routes.", status=422)
    if "{slug}" not in str(contract.get("route_template")) or "{slug}" not in str(contract.get("content_path_pattern")):
        raise WebsiteAuthorityError("contract_slug_required", "Declare an explicit article slug mapping.", status=422)
    for key in ("install_command", "build_command", "preview_command"):
        command = contract.get(key, [])
        if not isinstance(command, list) or len(command) > 100 or any(not isinstance(value, str) or not value or len(value) > 1000 or any(ord(char) < 32 for char in value) for value in command):
            raise WebsiteAuthorityError("contract_command_invalid", "Commands must be bounded argument arrays.", status=422)
    if not contract.get("build_command"):
        raise WebsiteAuthorityError("contract_build_required", "Declare the integration build command.", status=422)
    names = contract.get("environment_names", [])
    if not isinstance(names, list) or len(names) > 100 or any(not re.fullmatch(r"[A-Z_][A-Z0-9_]{0,99}", str(name)) for name in names):
        raise WebsiteAuthorityError("contract_environment_invalid", "Declare bounded environment variable names only.", status=422)
    if any(name in {"PATH", "HOME", "CI", "PORT", "HOST", "MLAI_SEAL_TOKEN"} or name.startswith(("GITHUB_", "ACTIONS_")) for name in names):
        raise WebsiteAuthorityError("contract_environment_invalid", "Provider credentials and verifier environment names are reserved.", status=422)
    defaults = {"schema_version": 1, "adapter_id": "custom_contract_v1", "adapter_version": 1, "app_root": ".", "package_manager": "",
        "lockfiles": [], "install_command": [], "preview_command": [], "environment_names": [], "dependency_policy": {}, "artifact_digest": None}
    if contract.get("artifact_digest") is not None and not re.fullmatch(r"[a-f0-9]{64}", str(contract["artifact_digest"])):
        raise WebsiteAuthorityError("contract_marker_invalid", "Artifact digest must be a lowercase SHA256 digest.", status=422)
    allowed = set(defaults) | {"runtime_family", "runtime_version", "build_command", "content_path_pattern", "route_template", "listing_route", "reviewed"}
    if set(contract) - allowed or not contract.get("runtime_family") or not contract.get("runtime_version"):
        raise WebsiteAuthorityError("contract_schema_invalid", "Use the versioned runtime, command and route contract fields.", status=422)
    return {**defaults, **contract}


def owner_support_operation(context, config, *, action, data):
    """Preserve an existing run's authority through worker support transport."""
    from integrations import http_client
    from workflow_runs.models import ContentFactoryRun
    from .website_connections import authority_guard, scoped_run_contract, require_unlocked_remote_call
    from .vibe_marketing_views import _content_factory_headers, _content_factory_remote_config
    if config.website_connection is None:
        raise WebsiteAuthorityError("website_connection_required", "Select a current website connection for this integration.")
    run = ContentFactoryRun.objects.filter(run_id=data.get("run_id"), organization=context.organization).first()
    if run is None:
        raise WebsiteAuthorityError("website_source_run_required", "Select the current scan or integration operation.", status=404)
    binding = scoped_run_contract(run)
    if any(binding.get(key) is None for key in ("operation_id", "operation_attempt", "deletion_epoch")):
        raise WebsiteAuthorityError("website_operation_required", "This historical run has no current operation fence. Start a fresh scan before integration verification.")
    reviewed = connection_contract(data)
    if not reviewed or any(connection_contract(binding).get(key) != value for key, value in reviewed.items()):
        raise WebsiteAuthorityError("website_connection_changed", "The reviewed run belongs to another website generation.")
    payload = {**binding, "run_id": run.run_id}
    if data.get("configuration_revision") is not None:
        payload["configuration_revision"] = data["configuration_revision"]
    if action == "custom-contract":
        if binding.get("source_run_id"):
            raise WebsiteAuthorityError("website_source_run_required", "Select the original scan or setup for a new custom verification.")
        if data.get("verification") is not None and not isinstance(data["verification"], dict):
            raise WebsiteAuthorityError("contract_sources_invalid", "Verification must be an object.", status=422)
        if data.get("phase", "configure") not in {"configure", "verify"}:
            raise WebsiteAuthorityError("contract_phase_invalid", "Choose configure or verify.", status=422)
        if data.get("generation") is not None and data.get("phase", "configure") != "configure":
            raise WebsiteAuthorityError("generation_configuration_after_verification", "Verify the committed custom contract first, then configure its generation templates and marker.", status=422)
        payload["contract"] = validate_custom_contract(data.get("contract"))
        if data.get("generation") is not None:
            return configure_owner_generation(config, binding=payload, contract=payload["contract"],
                generation=data["generation"], request_key=data.get("idempotency_key"))
        if data.get("phase"):
            payload["phase"] = data["phase"]
        if data.get("verification"):
            if not isinstance(data["verification"], dict):
                raise WebsiteAuthorityError("contract_sources_invalid", "Verification must be an object.", status=422)
            verification = dict(data["verification"])
            overrides = verification.get("file_overrides", {})
            if not isinstance(overrides, dict) or len(overrides) > 100 or sum(len(str(value)) for value in overrides.values()) > 1_000_000:
                raise WebsiteAuthorityError("contract_sources_invalid", "Verification sources exceed the isolated build limits.", status=422)
            for path in overrides:
                safe_repository_path(path)
            payload["verification"] = verification
        if data.get("phase") == "verify":
            from .website_contract import evidence_digest
            from .website_connections import contract_for
            from .website_operations import reserve_workflow_operation
            website = config.website_connection
            source = str((payload.get("verification") or {}).get("source_sha") or "")
            if not source or source != website.verified_sha:
                raise WebsiteAuthorityError("website_source_changed", "Verify the current scanned source for this custom contract.")
            target_id = (payload.get("verification") or {}).get("target_id")
            if not isinstance(target_id, str) or not target_id or len(target_id) > 200:
                raise WebsiteAuthorityError("verification_target_required", "Name the reviewed custom verification target.", status=422)
            reservation = {**contract_for(website), "expected_source_sha": source,
                "source_run_id": run.run_id,
                "target_id": target_id,
                "contract_digest": evidence_digest(payload["contract"]), "verification_input_digest": evidence_digest(payload.get("verification")),
                "client_request_id": str(data.get("idempotency_key") or f"native-verify:{evidence_digest(payload.get('verification'))}")}
            child = reserve_workflow_operation(website, workflow="native_verification", payload=reservation)
            if child.receipt.get("support"):
                return child
            child_run_id = str(child.pk)
            reservation["run_id"] = child_run_id
            payload["idempotency_key"] = child.idempotency_key
            with authority_guard(reservation, action="custom_contract"):
                child.payload = {**child.payload, "run_id": child_run_id, "source_run_id": run.run_id,
                    "binding": reservation, "contract": payload["contract"], "verification_input_digest": reservation["verification_input_digest"]}
                child.save(update_fields=["payload", "updated_at"])
                ContentFactoryRun.objects.get_or_create(run_id=child_run_id, defaults={"organization": context.organization,
                    "domain": context.organization.domain, "github_repo": config.github_repo, "workflow": "article_system_setup", "status": "queued", "run_request": reservation})
            payload = {**payload, **reservation, "source_run_id": run.run_id}
            binding = reservation
    else:
        evidence = data.get("evidence")
        if not isinstance(evidence, dict):
            raise WebsiteAuthorityError("ci_evidence_required", "Supply exact native CI evidence from the repository check.", status=422)
        from .website_verification import record_ci_attestation
        # A required-environment child can be attested using the owner's
        # selected original setup. The sealed operation still names the child.
        from .website_models import WebsiteConnectionOperation
        from uuid import UUID
        try:
            evidence_operation = UUID(str(evidence.get("operation_id")))
        except (ValueError, TypeError, AttributeError) as exc:
            raise WebsiteAuthorityError("invalid_operation_id", "CI evidence needs its exact verification operation.", status=422) from exc
        child = WebsiteConnectionOperation.objects.filter(pk=evidence_operation, connection_id=config.website_connection_id,
            generation=config.website_connection.generation, payload__source_run_id=run.run_id).first()
        if child and child.payload.get("workflow") == "native_verification":
            binding = child.payload["binding"]
            payload = {**binding, "run_id": child.payload["run_id"]}
        proof = {**evidence, **{key: binding[key] for key in ("website_connection_id", "connection_generation", "repository_id", "operation_id", "operation_attempt", "deletion_epoch") if key in binding}, "run_id": run.run_id}
        if child:
            proof["run_id"] = child.payload["run_id"]
        if any(key in evidence and evidence[key] != proof[key] for key in proof if key in evidence):
            raise WebsiteAuthorityError("ci_attestation_scope_mismatch", "CI evidence differs from the selected operation.")
        proof["expected_source_sha"] = evidence["source_sha"]
        if data.get("configuration_revision") is not None:
            proof["configuration_revision"] = data["configuration_revision"]
        record_ci_attestation(proof, owner_review=True)
        config.refresh_from_db()
        binding = {**binding, "expected_source_sha": evidence["source_sha"], "configuration_revision": config.website_connection.configuration_version}
        payload = {**payload, **binding}
        payload["evidence"] = evidence
    with authority_guard(payload, action="custom_contract" if action == "custom-contract" else "read") as website:
        pass
    from .website_contract import evidence_digest
    request_digest = evidence_digest({"action": action, "phase": data.get("phase", "configure"), "payload": payload, "generation": data.get("generation")})
    from .website_models import WebsiteConnectionOperation
    existing = WebsiteConnectionOperation.objects.get(pk=binding["operation_id"])
    requests = existing.payload.get("support_requests", {})
    request_key = str(data.get("idempotency_key") or request_digest)
    if request_key in requests:
        if requests[request_key]["digest"] != request_digest:
            raise WebsiteAuthorityError("operation_key_conflict", "This request identity was used for different support inputs.")
        if requests[request_key].get("completed"):
            if action == "custom-contract" and data.get("generation") is not None and not existing.receipt.get("generation_configured"):
                configure_custom_generation(config, binding={**binding, "configuration_revision": data.get("configuration_revision")}, contract=payload["contract"], generation=data["generation"])
                existing.receipt = {**existing.receipt, "generation_configured": True}
                existing.save(update_fields=["receipt", "updated_at"])
            return existing
    require_unlocked_remote_call()
    remote = _content_factory_remote_config()
    if not remote["enabled"]:
        raise WebsiteAuthorityError("support_worker_unavailable", "Integration verification is temporarily unavailable.", status=503, retryable=True)
    response = http_client.post(f"{remote['base_url']}/api/connections/{website.pk}/{action}", headers=_content_factory_headers(), json=payload, timeout=(3, 30))
    if response.status_code >= 400:
        raise WebsiteAuthorityError("support_verification_unavailable", "The integration worker could not accept this contract. Review its runtime and required environment names.", status=409, retryable=response.status_code >= 500)
    outcome = sanitized_evidence(response.json())
    with authority_guard(binding, action="read"):
        op = WebsiteConnectionOperation.objects.get(pk=binding["operation_id"])
        op.payload = {**op.payload, "support_path": action, "custom_contract": payload.get("contract") if action == "custom-contract" else None,
            "support_requests": {**op.payload.get("support_requests", {}), request_key: {"digest": request_digest, "completed": True}}}
        op.receipt = {**op.receipt, "support": outcome}
        op.save(update_fields=["payload", "receipt", "updated_at"])
    if action == "custom-contract" and data.get("generation") is not None:
        configure_custom_generation(config, binding={**binding, "configuration_revision": data.get("configuration_revision")},
            contract=payload["contract"], generation=data["generation"])
        op.receipt = {**op.receipt, "generation_configured": True}
        op.save(update_fields=["receipt", "updated_at"])
    return op


def configure_owner_generation(config, *, binding, contract, generation, request_key=None):
    """Configure a current certified target without rewriting its original scan."""
    from .website_connections import authority_guard
    from .website_contract import evidence_digest
    from .website_models import WebsiteConnectionOperation
    website = config.website_connection
    # CI may attest a reviewed commit newer than the immutable scan baseline.
    # Only local template configuration uses that already certified source;
    # the original run request and its mutation authority remain unchanged.
    current = {**binding, "expected_source_sha": website.verified_sha}
    digest = evidence_digest({"action": "custom-contract", "phase": "configure-generation",
        "binding": {key: current.get(key) for key in ("website_connection_id", "connection_generation", "repository_id", "operation_id", "operation_attempt", "deletion_epoch", "expected_source_sha")},
        "contract": contract, "generation": generation})
    key = str(request_key or digest)
    original = WebsiteConnectionOperation.objects.get(pk=binding["operation_id"])
    prior = original.payload.get("support_requests", {}).get(key)
    if prior and prior.get("digest") != digest:
        raise WebsiteAuthorityError("operation_key_conflict", "This request identity was used for different generation inputs.")
    if prior and prior.get("completed"):
        # A retry may carry the pre-success configuration revision. Check the
        # still-current consent and operation without repeating configuration.
        with authority_guard({key: value for key, value in current.items() if key != "configuration_revision"}, action="custom_contract"):
            return original
    return configure_custom_generation(config, binding=current, contract=contract, generation=generation,
        operation=original, request_key=key, request_digest=digest)


def configure_custom_generation(config, *, binding, contract, generation, operation=None, request_key=None, request_digest=None):
    """Persist reviewed seeds only against an already verified exact custom target."""
    from .website_connections import authority_guard, validate_template_update
    from .website_contract import evidence_digest, template_validation
    from .website_models import WebsiteTemplateRevision, WebsiteConnectionOperation
    if not isinstance(generation, dict) or generation.get("reviewed") is not True or set(generation) - {"reviewed", "article_template", "design_guide", "resource_prompt", "live_marker"}:
        raise WebsiteAuthorityError("generation_configuration_invalid", "Review the article template, design guide and live marker fields.", status=422)
    bodies = {key: generation[key] for key in ("article_template", "design_guide", "resource_prompt") if key in generation}
    if not {"article_template", "design_guide"}.issubset(bodies) or any(not isinstance(body, str) or len(body) > 500_000 for body in bodies.values()):
        raise WebsiteAuthorityError("generation_templates_required", "Supply bounded article_template and design_guide source bodies.", status=422)
    validate_template_update(bodies)
    marker = generation.get("live_marker")
    if (not isinstance(marker, dict) or marker.get("kind") != "artifact_digest" or marker.get("schema_version") != 1
            or marker.get("meta_name") != "mlai-artifact-digest" or not re.fullmatch(r"[a-f0-9]{64}", str(marker.get("value") or ""))):
        raise WebsiteAuthorityError("generation_marker_required", "Supply the reviewed version 1 mlai-artifact-digest marker.", status=422)
    if generation != sanitized_evidence(generation):
        raise WebsiteAuthorityError("generation_secret_values_denied", "Generation configuration must not contain credential values.", status=422)
    with authority_guard(binding, action="custom_contract") as website:
        target = website.targets.filter(generation=website.generation, target_key=config.default_publish_target_id,
            source_sha=website.verified_sha, adapter="custom_contract_v1", capabilities__adapterCertified=True, verified_at__isnull=False).first()
        if target is None or target.contract.get("contract_digest") != evidence_digest(contract):
            raise WebsiteAuthorityError("custom_contract_verification_required", "Verify this exact committed custom contract before configuring article generation.")
        proof = target.contract.get("verification") or {}
        if proof.get("artifact_digest") != marker["value"]:
            raise WebsiteAuthorityError("generation_marker_unverified", "The exact build/browser or CI proof must test this reviewed artifact marker before generation configuration.")
        if operation is not None:
            operation = WebsiteConnectionOperation.objects.select_for_update().get(pk=operation.pk)
            prior = operation.payload.get("support_requests", {}).get(request_key)
            if prior and prior.get("digest") != request_digest:
                raise WebsiteAuthorityError("operation_key_conflict", "This request identity was used for different generation inputs.")
            if prior and prior.get("completed"):
                return operation
        for purpose, body in bodies.items():
            setattr(config, purpose, body)
            WebsiteTemplateRevision.objects.get_or_create(connection=website, purpose=purpose,
                digest=hashlib.sha256(body.encode()).hexdigest(), defaults={"generation": website.generation, "source_sha": website.verified_sha,
                    "provenance": "owner_reviewed_custom_contract", "status": "validated", "body": body, "validation": template_validation(body)})
        config.save(update_fields=[*bodies, "updated_at"])
        target.contract = {**target.contract, "live_marker": marker}
        target.save(update_fields=["contract", "updated_at"])
        website.capabilities = {**website.capabilities, "templatesValid": True, "generationReady": False}
        website.configuration_version += 1
        website.save(update_fields=["capabilities", "configuration_version", "updated_at"])
        if operation is not None:
            operation.payload = {**operation.payload, "custom_contract": contract,
                "support_requests": {**operation.payload.get("support_requests", {}), request_key: {"digest": request_digest, "completed": True}}}
            operation.receipt = {**operation.receipt, "generation_configured": True,
                "support": {"status": "configured", "source_sha": website.verified_sha, "contract_digest": evidence_digest(contract)}}
            operation.save(update_fields=["payload", "receipt", "updated_at"])
        return operation


def custom_target_contract(*, contract, proof, previous=None):
    """Retain a configured marker only when the newer exact proof tested it."""
    previous = previous or {}
    target = {"target_id": proof["target_id"], "delivery_adapter": "custom_contract_v1", "publish_capability": "direct",
        "content_path": contract["content_path_pattern"], "content_path_pattern": contract["content_path_pattern"],
        "route_path": contract["listing_route"], "route_template": contract["route_template"], "custom_contract": contract,
        "contract_digest": proof["contract_digest"], "verification": {**proof, "status": "verified"}}
    marker = previous.get("live_marker")
    if (previous.get("contract_digest") == proof["contract_digest"] and isinstance(marker, dict)
            and marker.get("kind") == "artifact_digest" and marker.get("schema_version") == 1
            and marker.get("meta_name") == "mlai-artifact-digest" and re.fullmatch(r"[a-f0-9]{64}", str(marker.get("value") or ""))
            and marker["value"] == proof.get("artifact_digest")):
        target["live_marker"] = marker
    return target


def promote_custom_target(website, *, contract, proof):
    """Promote sealed CI or isolated proof, keeping generation configuration separate."""
    from .website_models import WebsiteConnectionTarget
    from .models import OrganizationContentConfig
    target_id = proof["target_id"]
    previous = WebsiteConnectionTarget.objects.filter(connection=website, target_key=target_id, generation=website.generation).first()
    target_contract = custom_target_contract(contract=contract, proof=proof, previous=previous.contract if previous else None)
    from .incident_guards import target_update_allowed, proof_stamp
    if not target_update_allowed(previous, target_contract, generation=website.generation, sha=proof["source_sha"]):
        raise WebsiteAuthorityError("website_target_verification_required", "A weaker custom contract cannot replace the accepted target proof.")
    accepted_at = previous.verified_at if previous and previous.contract == target_contract else proof_stamp(proof) or timezone.now()
    WebsiteConnectionTarget.objects.update_or_create(connection=website, target_key=target_id, generation=website.generation, defaults={
        "adapter": "custom_contract_v1", "adapter_version": "1", "source_sha": proof["source_sha"], "contract": target_contract,
        "capabilities": {"publishingReady": False, "adapterCertified": True}, "verified_at": accepted_at})
    config = OrganizationContentConfig.objects.get(website_connection=website)
    config.publish_targets, config.default_publish_target_id = [target_contract], target_id
    config.save(update_fields=["publish_targets", "default_publish_target_id", "updated_at"])
    website.capabilities = {**website.capabilities, "publishingReady": False, "previewSupported": True, "generationReady": False}
    website.last_verified_at = accepted_at
    website.save(update_fields=["capabilities", "last_verified_at", "updated_at"])


def reconcile_support_verifications(*, connection_id=None, limit=20):
    """Poll typed isolated proof and conditionally promote the reviewed contract."""
    from integrations import http_client
    from .website_connections import authority_guard, require_unlocked_remote_call
    from .website_models import WebsiteConnectionOperation, WebsiteConnectionTarget
    from .website_verification import validated_ci_identity
    from .vibe_marketing_views import _content_factory_headers, _content_factory_remote_config
    from .models import OrganizationContentConfig
    from workflow_runs.models import ContentFactoryRun
    rows = WebsiteConnectionOperation.objects.filter(action="workflow", state="running", payload__workflow="native_verification")
    if connection_id:
        rows = rows.filter(connection_id=connection_id)
    remote = _content_factory_remote_config()
    if not remote["enabled"]:
        return
    require_unlocked_remote_call()
    for op in rows.select_related("connection__organization").order_by("updated_at")[:limit]:
        binding = op.payload["binding"]
        try:
            with authority_guard(binding, action="custom_contract"):
                pass
            response = http_client.get(f"{remote['base_url']}/api/runs/{op.payload['run_id']}/native-verification", headers=_content_factory_headers(), timeout=(3, 20))
            response.raise_for_status()
            outcome = sanitized_evidence(response.json())
            state = outcome.get("status")
            proof = outcome.get("verification") if isinstance(outcome.get("verification"), dict) else {}
            contract = op.payload["contract"]
            if state == "verified":
                expected = {**proof, **binding, "source_sha": binding["expected_source_sha"], "contract_digest": binding["contract_digest"]}
                # The authenticated service owns isolated build/browser execution;
                # this is a native proof, distinct from GitHub CI origin.
                normalized = {key: value for key, value in proof.items() if key not in {"status", "proof_kind", "deployment_status"}}
                validated_ci_identity(expected, normalized)
            with authority_guard(binding, action="custom_contract") as website:
                op.refresh_from_db()
                if op.state != "running":
                    continue
                op.receipt = {"status": state, "support": outcome, "repository_modified": False}
                if state == "verified":
                    promote_custom_target(website, contract=contract, proof=normalized)
                    op.state = "completed"
                    ContentFactoryRun.objects.filter(run_id=op.payload["run_id"]).update(status="completed", result=outcome)
                elif state not in {"queued", "running"}:
                    op.state = "blocked" if state in {"requires_ci", "requires_configuration", "requires_github"} else "failed"
                    ContentFactoryRun.objects.filter(run_id=op.payload["run_id"]).update(status="blocked", result=outcome)
                op.save(update_fields=["state", "receipt", "updated_at"])
        except WebsiteAuthorityError:
            continue
        except Exception:
            WebsiteConnectionOperation.objects.filter(pk=op.pk, state="running").update(receipt={**op.receipt, "last_error": "native_verification_unavailable"})


def current_verification_data(config, reviewed):
    """Use retained sealed CI identity; never turn an empty verify into a no-op."""
    website = config.website_connection
    receipt = website.operations.filter(generation=website.generation, action="ci-verify", state="completed",
        payload__target_id=config.default_publish_target_id, payload__source_sha=website.verified_sha).order_by("-created_at").first()
    if receipt is None:
        raise WebsiteAuthorityError("ci_attestation_required", "Verify your current build and browser evidence with the repository CI check, then verify the live route.")
    from .website_connections import contract_for
    supplied = connection_contract(reviewed)
    current = {**contract_for(website), "connection_target_id": config.default_publish_target_id}
    if not supplied or any(connection_contract(current).get(key) != value for key, value in supplied.items()):
        raise WebsiteAuthorityError("website_connection_changed", "Review the current website before verifying its deployed articles.")
    # A generic client may submit the latest lifecycle receipt operation ID.
    # That receipt is not the original workflow identity sealed by CI. Keep
    # every proof identity from the retained attestation, not request overlays.
    owner_fields = {key: reviewed[key] for key in ("configuration_revision", "idempotency_key", "client_request_id") if key in reviewed}
    return {**receipt.payload, **owner_fields, "target_id": config.default_publish_target_id, "source_sha": website.verified_sha}
