"""Durable pending hosted verification, separate from the accepted target."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path

from django.conf import settings

from . import website_goal as goals
from .website_contract import WebsiteAuthorityError
from .website_failure_contract import canonical_bytes
from .website_publishing_inputs import candidate_for_target, has_candidate

MANIFEST = Path(__file__).resolve().parents[1] / "contracts/native-verifier-v3.json"
REQUIRED_VERSION = "requiredProofVersion"


def digest(value):
    """Hash finite UTF-8 state without changing the legacy contract digest."""
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def enabled(connection):
    """Require independent enrollment and both hosted execution prerequisites."""
    from .website_hosted_verification import enabled as hosted_enabled
    return getattr(settings, "ARTICLES_PUBLISHING_INPUTS_V3_ENABLED", False) is True and hosted_enabled(connection)


def required(connection, target):
    """A persisted requirement survives rollout disablement and scan refresh."""
    caps = getattr(target, "capabilities", None) or {}
    return bool(target and (REQUIRED_VERSION in caps or has_candidate(target.contract) or enabled(connection)))


def supported_verifier():
    """Read verifier identity shipped with the immutable private producer pin."""
    try:
        value = json.loads(MANIFEST.read_text())
        if (set(value) != {"schema_version", "profile", "proof_schema_version", "runner_path", "runner_sha256", "python_version", "playwright_version"}
                or type(value["schema_version"]) is not int or value["schema_version"] != 1
                or type(value["proof_schema_version"]) is not int or value["proof_schema_version"] != 3
                or value["runner_path"] != "backend/ci_templates/articles_verification.py"):
            raise ValueError
        from .website_sealed_verification import _verifier_supported
        verifier = {key: value[key] for key in ("profile", "runner_sha256", "python_version", "playwright_version")}
        verifier["execution_schema_version"] = 1
        if not _verifier_supported(verifier, verifier):
            raise ValueError
        return verifier
    except (OSError, ValueError, TypeError, KeyError):
        raise WebsiteAuthorityError("ci_attestation_contract_mismatch", "The supported execution verifier is unavailable.") from None


def parent_binding(op, connection, source):
    """Bind proposal reads to the current prepare attempt, never its next retry."""
    from .website_connections import contract_for
    from .website_operations import deletion_epoch
    return {**contract_for(connection), "operation_id": str(op.pk),
        "operation_attempt": op.payload.get("attempt", 1), "deletion_epoch": deletion_epoch(connection),
        "configuration_revision": connection.configuration_version, "expected_source_sha": source}


def parent_snapshot(op, connection, config, target, source_run, *, source=None):
    """Validate the original goal, selection and setup ownership under authority."""
    from .website_setup_consent import valid as valid_consent
    from .website_operations import deletion_epoch
    from .website_hosted_setup_lineage import KEY as RECOVERED_SOURCE, source_reference, validate_recovered_source
    goal = goals.mapping(op.receipt).get("goal")
    scope = goals.scope_for(connection, op.payload.get("company_id"))
    latest = connection.operations.filter(generation=connection.generation, action="prepare").order_by("-created_at").first()
    if (op.action != "prepare" or op.state not in {"pending", "running", "needs_user"}
            or op.generation != connection.generation or latest is None or latest.pk != op.pk
            or not goals.valid_goal(goal, scope) or not valid_consent(goal)
            or goal["consent"]["answers"]["ci_mode"] != "hosted"
            or str(goal["goal_id"]) != str(op.pk)
            or config.default_publish_target_id != target.target_key
            or target.generation != connection.generation or target.connection_id != connection.pk):
        raise WebsiteAuthorityError("website_operation_changed", "The original hosted verification goal changed.")
    request = goals.mapping(getattr(source_run, "run_request", None))
    expected = {"website_connection_id": str(connection.pk), "connection_generation": connection.generation,
        "repository_id": connection.repository_id, "github_repo": connection.github_repo,
        "branch": connection.branch, "app_root": connection.app_root, "deletion_epoch": deletion_epoch(connection)}
    if (source_run is None or source_run.workflow != "article_system_setup"
            or source_run.organization_id != connection.organization_id
            or source_run.domain != connection.organization.domain or source_run.github_repo != connection.github_repo
            or source_run.run_id != source_reference(op)
            or any(type(request.get(key)) is not type(value) or request.get(key) != value for key, value in expected.items())
            or not isinstance(request.get("expected_source_sha"), str) or not goals.SHA.fullmatch(request["expected_source_sha"])):
        raise WebsiteAuthorityError("ci_attestation_scope_mismatch", "The original setup inputs do not belong to this website.")
    validate_recovered_source(op, connection, source_run, source=source)
    return {**({"recovered_setup_digest": digest(op.receipt[RECOVERED_SOURCE]),
        "parent_retry_epoch": op.payload.get("retry_epoch")} if RECOVERED_SOURCE in op.receipt else {}),
        "parent_operation_id": str(op.pk), "parent_attempt": op.payload.get("attempt", 1),
        "consent_digest": digest(goal["consent"]), "goal_scope_digest": digest(goal["scope"]),
        "accepted_target_digest": digest(target.contract), "source_request_digest": digest(request),
        "baseline_source_sha": request["expected_source_sha"], "target_id": target.target_key,
        "source_run_id": source_run.run_id}


def mark_required(target):
    """Withhold new article admission while preserving the prior accepted proof."""
    if REQUIRED_VERSION in target.capabilities and (type(target.capabilities[REQUIRED_VERSION]) is not int or target.capabilities[REQUIRED_VERSION] != 3):
        raise WebsiteAuthorityError("ci_attestation_contract_mismatch", "The required verification protocol is unsupported.")
    target.capabilities = {**target.capabilities, REQUIRED_VERSION: 3, "publishingReady": False}
    target.save(update_fields=["capabilities", "updated_at"])


def candidate_target(target, candidate):
    """Attach only the validated candidate; keep original legacy digest bytes."""
    value = deepcopy(target)
    value["contract"] = {**value.get("contract", {}), "publishing_inputs_v1": deepcopy(candidate)}
    if candidate_for_target(value) != candidate:
        raise WebsiteAuthorityError("ci_attestation_contract_mismatch", "The candidate differs from the accepted articles target.")
    return value


def prepare_candidate(op, context, config, target, source):
    """Read an original setup proposal outside locks, then fence its snapshot."""
    from workflow_runs.models import ContentFactoryRun
    from .website_connections import authority_guard
    from .website_sealed_candidate import fetch_candidate, validate_proposal
    from .website_hosted_setup_lineage import source_reference
    connection = config.website_connection
    if not enabled(connection):
        raise WebsiteAuthorityError("ci_attestation_required", "Compatible hosted execution verification is disabled.")
    binding = parent_binding(op, connection, source)
    with authority_guard(binding, action="read") as current:
        op.refresh_from_db(); config.refresh_from_db(); target.refresh_from_db()
        source_run = ContentFactoryRun.objects.filter(run_id=source_reference(op), organization_id=current.organization_id).first()
        snapshot = parent_snapshot(op, current, config, target, source_run, source=source)
        accepted = deepcopy(target.contract)
        mark_required(target)
    request = {key: value for key, value in binding.items() if key != "configuration_revision"}
    request.update(source_run_id=snapshot["source_run_id"], target_id=target.target_key,
        contract_digest=accepted.get("contract_digest"))
    candidate = validate_proposal(fetch_candidate(request), request=request, target=accepted,
        baseline_source_sha=snapshot["baseline_source_sha"], supported_verifier=supported_verifier())
    with authority_guard(binding, action="read") as current:
        op.refresh_from_db(); config.refresh_from_db(); target.refresh_from_db(); source_run.refresh_from_db()
        if parent_snapshot(op, current, config, target, source_run, source=source) != snapshot:
            raise WebsiteAuthorityError("website_configuration_changed", "The hosted verification inputs changed during the service read.")
    return candidate, {**snapshot, "parent_binding": binding, "accepted_target": accepted}


def fence_pending(connection, target, pending):
    """Repeat the parent and target snapshot immediately before a local write."""
    from .models import OrganizationContentConfig
    from workflow_runs.models import ContentFactoryRun
    from .website_operations import validate_operation
    if type(pending) is not dict or type(pending.get("snapshot")) is not dict:
        raise WebsiteAuthorityError("ci_attestation_contract_mismatch", "Original execution inputs are missing.")
    snapshot = pending["snapshot"]
    parent = validate_operation(connection, snapshot["parent_binding"])
    config = OrganizationContentConfig.objects.get(website_connection=connection)
    source_run = ContentFactoryRun.objects.filter(run_id=snapshot["source_run_id"], organization_id=connection.organization_id).first()
    if parent is None:
        raise WebsiteAuthorityError("website_operation_changed", "The original verification goal is unavailable.")
    actual = parent_snapshot(parent, connection, config, target, source_run, source=snapshot["parent_binding"].get("expected_source_sha"))
    if any(actual.get(key) != value for key, value in snapshot.items() if key not in {"parent_binding", "accepted_target"}):
        raise WebsiteAuthorityError("website_configuration_changed", "The original verification inputs changed.")
    if digest(snapshot["accepted_target"]) != actual["accepted_target_digest"]:
        raise WebsiteAuthorityError("ci_attestation_contract_mismatch", "The accepted target snapshot changed.")
    return candidate_target(target.contract, pending["candidate"])


def child_inputs(connection, data):
    """Load the persisted child request; the submitted proof grants no scope."""
    from .website_operations import validate_operation
    child = validate_operation(connection, data)
    binding = goals.mapping(child.payload.get("binding")) if child else {}
    pending = goals.mapping(child.payload.get("sealed_inputs")) if child else {}
    if (not child or child.state != "completed" or child.payload.get("workflow") != "hosted_native_verification"
            or any(type(binding.get(key)) is not type(data.get(key)) or binding.get(key) != data.get(key)
                for key in ("target_id", "contract_digest", "run_id", "source_run_id", "publishing_inputs_digest", "baseline_source_sha", "verifier_sha256"))
            or binding.get("expected_source_sha") != data.get("source_sha")
            or not pending):
        raise WebsiteAuthorityError("ci_attestation_scope_mismatch", "Hosted evidence differs from its original execution request.")
    return child, binding, pending


def validate_child_proof(connection, data, proof):
    """Validate service evidence against persisted inputs and a shipped verifier."""
    from .website_sealed_verification import validate_hosted_sealed_proof
    _, binding, pending = child_inputs(connection, data)
    return validate_hosted_sealed_proof(proof, origin="authenticated_hosted", binding=binding,
        candidate=pending["candidate"], supported_verifier=supported_verifier())


def saved_receipt(connection, target, receipt):
    """Revalidate saved v3 provenance without fetching or trusting worker flags."""
    from .website_sealed_verification import PROOF_FIELDS, validate_hosted_sealed_proof
    required_version = (target.capabilities or {}).get(REQUIRED_VERSION, 3)
    if type(required_version) is not int or required_version != 3:
        raise WebsiteAuthorityError("ci_attestation_contract_mismatch", "The required verification protocol is unsupported.")
    if (type(receipt) is not dict or set(receipt) != PROOF_FIELDS | {"allowed", "attestation_origin", "checked_at"}
            or receipt.get("allowed") is not True or receipt.get("attestation_origin") != "authenticated_hosted"):
        raise WebsiteAuthorityError("ci_attestation_contract_mismatch", "Authenticated execution evidence is required.")
    child = connection.operations.filter(pk=receipt["operation_id"], generation=connection.generation).first()
    binding = goals.mapping(child.payload.get("binding")) if child else {}
    data = {**receipt, "run_id": binding.get("run_id"), "source_run_id": binding.get("source_run_id")}
    _, binding, pending = child_inputs(connection, data)
    candidate = candidate_for_target(target.contract)
    if candidate is None or candidate != pending["candidate"]:
        raise WebsiteAuthorityError("ci_attestation_contract_mismatch", "The saved proof does not certify the current target.")
    proof = {key: receipt[key] for key in PROOF_FIELDS}
    validate_hosted_sealed_proof(proof, origin="authenticated_hosted", binding=binding,
        candidate=candidate, supported_verifier=supported_verifier())
    return deepcopy(receipt)


def replay_operation(connection, target, data):
    """Return an identical accepted result without renewing its proof timestamp."""
    from .website_verification import current_ci_attestation
    from .website_sealed_verification import PROOF_FIELDS
    receipt = current_ci_attestation(connection, target)
    if receipt is None or receipt.get("evidence_digest") != data.get("evidence_digest"):
        return None
    if any(type(data.get(key)) is not type(receipt[key]) or data.get(key) != receipt[key] for key in PROOF_FIELDS):
        raise WebsiteAuthorityError("ci_attestation_scope_mismatch", "The replay differs from its accepted execution proof.")
    child_inputs(connection, data)
    operation = connection.operations.filter(generation=connection.generation, action="ci-verify", state="completed",
        payload__operation_id=data["operation_id"], payload__evidence_digest=data["evidence_digest"]).order_by("-created_at").first()
    if operation is None or operation.receipt != receipt:
        raise WebsiteAuthorityError("ci_attestation_required", "The accepted execution receipt is unavailable.")
    return operation
