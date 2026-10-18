"""Durable original/current reservations and atomic native setup acceptance.

Source observers are trusted server-side read callables, never request fields.
They must authenticate their fixed provider route. The reservations cannot be
created from the native receipt, a later scan, or a selected-default fallback.
This setup-only protocol deliberately does not grant article publication.
"""
from contextlib import contextmanager
from copy import deepcopy
import json
from pathlib import Path
import re
from urllib.parse import urlsplit
from uuid import UUID

from django.conf import settings
from django.utils import timezone

from . import native_publisher_contract_v4 as protocol
from . import website_goal as goals
from . import website_sealed_workflow as sealed
from .website_contract import WebsiteAuthorityError
from .website_native_publisher_v4 import validate_native_setup
from .website_publisher_inputs_v2 import validate_candidate, wire_validate

MANIFEST = Path(__file__).resolve().parents[1] / "contracts/native-verifier-v4.json"
PROFILE = "original_python_static_marker_v1"
PENDING = "publisher_inputs_v4"


def _invalid():
    return WebsiteAuthorityError("ci_attestation_contract_mismatch", "The original publisher execution inputs are unavailable or changed.")


def _changed():
    return WebsiteAuthorityError("website_configuration_changed", "The original publisher reservation changed during verification.")


def enabled(connection):
    """Require separate rollout, hosted execution, and original-goal enrollment."""
    from .website_hosted_verification import enabled as hosted_enabled
    return getattr(settings, "ARTICLES_PUBLISHING_INPUTS_V4_ENABLED", False) is True and hosted_enabled(connection)


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def supported_profile():
    """Select only an independently shipped profile; the default list is empty."""
    try:
        raw = MANIFEST.read_bytes()
        if len(raw) > 16384:
            raise ValueError
        manifest = json.loads(raw, object_pairs_hook=_unique)
        if (type(manifest) is not dict or set(manifest) != {"schema_version", "profiles"}
                or type(manifest["schema_version"]) is not int or manifest["schema_version"] != 1
                or type(manifest["profiles"]) is not list or len(manifest["profiles"]) > 1):
            raise ValueError
        if not manifest["profiles"]:
            raise ValueError
        value = manifest["profiles"][0]
        if (type(value) is not dict or set(value) != {"profile", "source_revision", "qualification_digest", "native", "executor", "mode"}
                or value["profile"] != PROFILE or value["mode"] != "setup_only"
                or type(value["source_revision"]) is not str or not re.fullmatch(r"[a-f0-9]{40}", value["source_revision"])
                or type(value["qualification_digest"]) is not str or not re.fullmatch(r"[a-f0-9]{64}", value["qualification_digest"])):
            raise ValueError
        lock = json.loads((MANIFEST.parent / "native-publisher-v4.lock.json").read_text(), object_pairs_hook=_unique)
        if value["source_revision"] != lock["source_revision"]:
            raise ValueError
        wire_validate(value["native"], protocol.receipt_schema()["properties"]["profile"])
        from .publisher_contract_v2 import candidate_schema
        from .website_publishing_inputs import _schema
        executor_schema = candidate_schema(_schema())["properties"]["payload"]["properties"]["executor"]
        wire_validate(value["executor"], executor_schema)
        if any(value["native"][key] != value["executor"][key] for key in ("python_version", "pyyaml_version", "runtime_image")):
            raise ValueError
        return deepcopy(value)
    except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError, RecursionError):
        raise _invalid() from None


def source_facts(value, expected_sha):
    """Require independently observed exact commit, tree, and complete inventory."""
    try:
        if (type(value) is not dict or set(value) != {"source_sha", "source_tree_sha", "source_inventory_digest"}
                or value["source_sha"] != expected_sha
                or any(type(value[key]) is not str or not re.fullmatch(r"[a-f0-9]{40}(?:[a-f0-9]{24})?", value[key])
                    for key in ("source_sha", "source_tree_sha"))
                or len(value["source_sha"]) != len(value["source_tree_sha"])
                or type(value["source_inventory_digest"]) is not str or not re.fullmatch(r"[a-f0-9]{64}", value["source_inventory_digest"])):
            raise ValueError
        return deepcopy(value)
    except (ValueError, TypeError, KeyError):
        raise _invalid() from None


def _context(parent, connection, config, target, source):
    snapshot = sealed.parent_snapshot(parent, connection, config, target, source)
    request = source.run_request
    try:
        setup_id = request["operation_id"]
        attempt = request["operation_attempt"]
        if type(setup_id) is not str or str(UUID(setup_id)) != setup_id or type(attempt) is not int or attempt < 1:
            raise ValueError
        setup = connection.operations.filter(pk=setup_id, generation=connection.generation).first()
        if (setup is None or setup.action != "workflow" or setup.state not in {"pending", "running", "completed"}
                or setup.payload.get("workflow") != "article_system_setup" or setup.payload.get("run_id") != source.run_id
                or type(setup.payload.get("attempt")) is not int or setup.payload["attempt"] != attempt):
            raise ValueError
        origin = connection.site_url
        parsed = urlsplit(origin)
        if (type(origin) is not str or parsed.scheme not in {"https", "http"} or not parsed.hostname
                or parsed.username or parsed.password or parsed.path not in {"", "/"} or parsed.query or parsed.fragment):
            raise ValueError
        parsed.port
        from .website_operations import deletion_epoch
        scope = {"website_connection_id": str(connection.pk), "connection_generation": connection.generation,
            "repository_id": connection.repository_id, "github_repo": connection.github_repo, "deletion_epoch": deletion_epoch(connection),
            "branch": connection.branch, "app_root": connection.app_root, "setup_operation_id": setup_id,
            "setup_operation_attempt": attempt, "site_origin": origin.rstrip("/")}
        if scope["app_root"] != "" or target.source_sha != connection.verified_sha:
            raise ValueError
        from .website_connections import contract_for
        result = {"snapshot": snapshot, "scope": scope, "connection": contract_for(connection), "target": deepcopy(target.contract),
            "current_source_sha": connection.verified_sha, "configuration_revision": connection.configuration_version}
        if len(protocol.canonical(result)) > 192 * 1024:
            raise ValueError
        return result
    except (ValueError, TypeError, KeyError, AttributeError):
        raise _invalid() from None


def _parent(identifier):
    from .website_models import WebsiteConnectionOperation
    try:
        return WebsiteConnectionOperation.objects.select_related("connection__organization").get(pk=identifier, action="prepare")
    except (WebsiteConnectionOperation.DoesNotExist, ValueError, TypeError):
        raise _invalid() from None


@contextmanager
def _locked(parent_id, binding):
    from .models import OrganizationContentConfig
    from .website_connections import authority_guard
    from .website_models import WebsiteConnectionOperation, WebsiteConnectionTarget
    from workflow_runs.models import ContentFactoryRun
    with authority_guard(binding, action="read") as connection:
        parent = WebsiteConnectionOperation.objects.select_for_update().get(pk=parent_id, connection=connection)
        config = OrganizationContentConfig.objects.select_for_update().get(website_connection=connection)
        target = WebsiteConnectionTarget.objects.select_for_update().filter(connection=connection,
            generation=connection.generation, target_key=config.default_publish_target_id).first()
        source = ContentFactoryRun.objects.select_for_update().filter(run_id=parent.receipt.get("setup_run_id"),
            organization_id=connection.organization_id).first()
        if target is None or source is None or not enabled(connection):
            raise _invalid()
        yield parent, connection, config, target, source, _context(parent, connection, config, target, source)


def _binding(parent):
    return sealed.parent_binding(parent, parent.connection, parent.connection.verified_sha)


def _fence(original, context, profile):
    if (original.action != "publisher-original" or original.state not in {"pending", "running", "completed"}
            or type(original.payload.get("version")) is not int or original.payload["version"] != 1
            or str(original.connection_id) != context["scope"]["website_connection_id"]
            or original.generation != context["scope"]["connection_generation"] or original.payload.get("context") != context
            or protocol.canonical(original.payload.get("context")) != protocol.canonical(context)
            or original.payload.get("profile") != profile):
        raise _changed()


def _observe(observer, request):
    from .website_connections import require_unlocked_remote_call
    require_unlocked_remote_call()
    try:
        observed = observer(deepcopy(request))
    except WebsiteAuthorityError:
        raise
    except Exception:
        raise WebsiteAuthorityError("worker_unavailable", "The original publisher source observer is temporarily unavailable.", retryable=True) from None
    return source_facts(observed, request["expected_source_sha"])


def reserve_original(parent_id, *, observe_source):
    """Save original raw Git observations before a proposal or execution arrives.

    The server observer receives only its own immutable source-run request.
    No receipt data, candidate, commands or source-directory override is accepted.
    Existing reservations replay without renewing observations or timestamps.
    """
    from .website_models import WebsiteConnectionOperation
    parent = _parent(parent_id)
    binding = _binding(parent)
    profile = supported_profile()
    key = f"{parent.connection_id}:publisher-original:{parent.pk}"
    with _locked(parent.pk, binding) as (_, connection, config, target, source, context):
        existing = connection.operations.select_for_update().filter(idempotency_key=key).first()
        if existing is not None:
            if existing.state == "completed":
                child = connection.operations.select_for_update().get(pk=existing.receipt.get("child_id"))
                _replay(child, existing, connection, target, config, context, child.receipt.get("native_proof"), profile)
                return existing
            _fence(existing, context, profile)
            source_facts(existing.payload.get("facts"), context["snapshot"]["baseline_source_sha"])
            return existing
        request = {"source_run_id": source.run_id, "scope": deepcopy(context["scope"]),
            "expected_source_sha": context["snapshot"]["baseline_source_sha"]}
    facts = _observe(observe_source, request)
    with _locked(parent.pk, binding) as (_, connection, _, _, _, current):
        if current != context or supported_profile() != profile:
            raise _changed()
        original, created = WebsiteConnectionOperation.objects.get_or_create(idempotency_key=key, defaults={
            "connection": connection, "generation": connection.generation, "action": "publisher-original", "state": "pending",
            "payload": {"version": 1, "context": context, "profile": profile, "facts": facts},
            "receipt": {"status": "original_reserved"}})
        _fence(original, context, profile)
        if original.payload["facts"] != facts:
            raise _changed()
        return original


def _proposal(original, candidate, records):
    try:
        value = validate_candidate(candidate)
        payload, context, profile = value["payload"], original.payload["context"], original.payload["profile"]
        if (protocol.canonical(payload["original_scope"]) != protocol.canonical(context["scope"])
                or payload["baseline"] != original.payload["facts"] or payload["executor"] != profile["executor"]
                or payload["native_inputs_v1"]["payload"]["verification"]["verifier"]["runner_sha256"] != profile["native"]["oracle_sha256"]
                or type(records) is not list or len(records) != 2 or len(protocol.canonical(records)) > 256 * 1024
                or [protocol.digest(row) for row in records] != payload["probe_record_digests"]):
            raise ValueError
        sealed.candidate_target(context["target"], payload["native_inputs_v1"])
        binding = {**context["scope"], **original.payload["facts"], "publishing_inputs_digest": value["digest"]}
        protocol.validate_original_binding(binding, value)
        frozen = {"candidate": value, "records": deepcopy(records), "original_binding": binding}
        if "proposal" in original.payload and protocol.canonical(original.payload["proposal"]) != protocol.canonical(frozen):
            raise ValueError
        return frozen
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError, RecursionError):
        raise _invalid() from None


def reserve_execution(original_id, *, candidate, records, observe_source):
    """Freeze one proposal and current Git tuple, then allocate its durable child.

    This does not dispatch, reset the parent budget, create a charge, or grant
    Ready. The caller must dispatch the returned saved request after commit.
    """
    from .website_models import WebsiteConnectionOperation
    from workflow_runs.models import ContentFactoryRun
    original = WebsiteConnectionOperation.objects.get(pk=original_id, action="publisher-original")
    parent = _parent(original.payload["context"]["snapshot"]["parent_operation_id"])
    binding, profile = _binding(parent), supported_profile()
    with _locked(parent.pk, binding) as (_, connection, config, target, source, context):
        original.refresh_from_db()
        if original.state == "completed":
            _proposal(original, candidate, records)
            child = connection.operations.select_for_update().get(pk=original.receipt.get("child_id"))
            return _replay(child, original, connection, target, config, context, child.receipt.get("native_proof"), profile)
        _fence(original, context, profile)
        proposal = _proposal(original, candidate, records)
        key = "publisher-v4:" + protocol.digest({"original": str(original.pk), "source": context["current_source_sha"], "proposal": proposal})
        existing = connection.operations.select_for_update().filter(idempotency_key=key).first()
        if existing is not None:
            _pending(existing, original, context, profile)
            return existing
        request = {"source_run_id": source.run_id, "scope": deepcopy(context["scope"]), "expected_source_sha": context["current_source_sha"]}
    facts = _observe(observe_source, request)
    if facts["source_sha"] == original.payload["facts"]["source_sha"]:
        raise _invalid()
    with _locked(parent.pk, binding) as (_, connection, _, target, source, current):
        original.refresh_from_db()
        _fence(original, current, supported_profile())
        if current != context or original.payload["profile"] != profile or _proposal(original, candidate, records) != proposal:
            raise _changed()
        child, created = WebsiteConnectionOperation.objects.get_or_create(idempotency_key=key, defaults={
            "connection": connection, "generation": connection.generation, "action": "workflow", "state": "pending",
            "payload": {"workflow": "hosted_native_verification", "attempt": 1, "publisher_original_operation_id": str(original.pk)}})
        if not created:
            _pending(child, original, context, profile)
            return child
        native_identity = protocol.identity_for(candidate, str(child.pk), 1)
        execution_binding = _execution_binding(child, context, proposal)
        pending = {"version": 1, "original_id": str(original.pk), "context": context, "profile": profile,
            **proposal, "current_binding": facts, "identity": native_identity}
        child.payload = {**child.payload, "binding": execution_binding, "run_id": str(child.pk), "source_run_id": source.run_id,
            "deletion_epoch": context["scope"]["deletion_epoch"], PENDING: pending}
        child.save(update_fields=["payload", "updated_at"])
        ContentFactoryRun.objects.create(run_id=str(child.pk), organization=connection.organization, domain=connection.organization.domain,
            github_repo=connection.github_repo, workflow="hosted_native_verification", status="queued", run_request=execution_binding)
        original.payload = {**original.payload, "proposal": proposal}
        original.state = "running"
        original.save(update_fields=["payload", "state", "updated_at"])
        target.capabilities = {**target.capabilities, "requiredProofVersion": 4, "publishingReady": False}
        target.save(update_fields=["capabilities", "updated_at"])
        connection.capabilities = {**connection.capabilities, "publishingReady": False}
        connection.save(update_fields=["capabilities", "updated_at"])
        return child


def _execution_binding(child, context, proposal):
    return {**context["connection"], "operation_id": str(child.pk), "operation_attempt": 1,
        "deletion_epoch": context["scope"]["deletion_epoch"], "expected_source_sha": context["current_source_sha"],
        "target_id": context["snapshot"]["target_id"], "contract_digest": context["target"].get("contract_digest"),
        "run_id": str(child.pk), "source_run_id": context["snapshot"]["source_run_id"], "client_request_id": child.idempotency_key,
        "publishing_inputs_digest": proposal["candidate"]["digest"], "original_publisher_binding_v2": deepcopy(proposal["original_binding"])}


def _pending(child, original, context, profile):
    try:
        pending = child.payload[PENDING]
        if (child.action != "workflow" or child.state not in {"pending", "running", "completed"}
                or child.connection_id != original.connection_id or child.generation != original.generation
                or child.payload.get("attempt") != 1 or type(child.payload.get("attempt")) is not int
                or child.payload.get("publisher_original_operation_id") != str(original.pk)
                or child.payload.get("workflow") != "hosted_native_verification"
                or set(pending) != {"version", "original_id", "context", "profile", "candidate", "records", "original_binding", "current_binding", "identity"}
                or type(pending["version"]) is not int or pending["version"] != 1 or pending["original_id"] != str(original.pk)
                or protocol.canonical(pending["context"]) != protocol.canonical(context)
                or protocol.canonical(pending["profile"]) != protocol.canonical(profile)
                or protocol.canonical({key: pending[key] for key in ("candidate", "records", "original_binding")}) != protocol.canonical(original.payload["proposal"])
                or protocol.canonical(pending["identity"]) != protocol.canonical(protocol.identity_for(pending["candidate"], str(child.pk), 1))
                or child.payload.get("run_id") != str(child.pk)
                or child.payload.get("source_run_id") != context["snapshot"]["source_run_id"]
                or child.idempotency_key != "publisher-v4:" + protocol.digest({"original": str(original.pk), "source": context["current_source_sha"], "proposal": original.payload["proposal"]})
                or protocol.canonical(child.payload.get("binding")) != protocol.canonical(_execution_binding(child, context, original.payload["proposal"]))):
            raise ValueError
        source_facts(pending["current_binding"], context["current_source_sha"])
        return deepcopy(pending)
    except (ValueError, TypeError, KeyError, AttributeError):
        raise _invalid() from None


def _run(child, connection):
    from workflow_runs.models import ContentFactoryRun
    run = ContentFactoryRun.objects.select_for_update().filter(run_id=str(child.pk)).first()
    if (run is None or run.organization_id != connection.organization_id or run.domain != connection.organization.domain
            or run.github_repo != connection.github_repo or run.workflow != "hosted_native_verification"
            or run.status in {"cancelled", "denied"} or protocol.canonical(run.run_request) != protocol.canonical(child.payload["binding"])):
        raise _invalid()
    return run


def record_native_setup(data, *, origin):
    """Read the authenticated original child, then atomically accept setup only.

    The accepted target/config and child/original receipt commit together. An
    identical replay does not refetch the worker or renew timestamps. Neither
    this result nor its hypothetical probes are publication or refund authority.
    """
    from .website_connections import verify_repository_head
    from .website_models import WebsiteConnectionOperation
    from .website_sealed_candidate import authenticated_request
    try:
        wire_validate(data, protocol.receipt_schema())
        if origin != "authenticated_hosted":
            raise ValueError
        child = WebsiteConnectionOperation.objects.select_related("connection__organization").get(pk=data["identity"]["operation_id"])
        original = WebsiteConnectionOperation.objects.get(pk=child.payload["publisher_original_operation_id"], connection=child.connection)
        parent = _parent(original.payload["context"]["snapshot"]["parent_operation_id"])
    except (ValueError, TypeError, KeyError, WebsiteConnectionOperation.DoesNotExist):
        raise _invalid() from None
    binding, profile = _binding(parent), supported_profile()
    with _locked(parent.pk, binding) as (_, connection, config, target, _, context):
        original.refresh_from_db(); child.refresh_from_db()
        _run(child, connection)
        if original.state == "completed":
            return _replay(child, original, connection, target, config, context, data, profile)
        _fence(original, context, profile)
        pending = _pending(child, original, context, profile)
        _validate(data, pending)
        target_digest = protocol.digest(target.contract)
    verify_repository_head(connection, pending["current_binding"]["source_sha"])
    result = authenticated_request("get", f"/api/runs/{child.pk}/hosted-native-verification")
    if result.get("status") != "completed" or result.get("run_id") != str(child.pk):
        raise WebsiteAuthorityError("ci_attestation_required", "The original publisher verifier has not completed.")
    proof = _validate(result.get("verification"), pending)
    if protocol.canonical(proof) != protocol.canonical(data):
        raise _invalid()
    verify_repository_head(connection, pending["current_binding"]["source_sha"])
    with _locked(parent.pk, binding) as (_, connection, config, target, _, context):
        original.refresh_from_db(); child.refresh_from_db()
        run = _run(child, connection)
        if original.state == "completed":
            return _replay(child, original, connection, target, config, context, data, supported_profile())
        _fence(original, context, supported_profile())
        if _pending(child, original, context, supported_profile()) != pending or protocol.digest(target.contract) != target_digest:
            raise _changed()
        checked_at = timezone.now().isoformat()
        receipt = {"status": "setup_verified", "allowed": False, "attestation_origin": "authenticated_hosted",
            "checked_at": checked_at, "native_proof": proof, "original_id": str(original.pk)}
        contract = deepcopy(target.contract)
        contract["contract"] = {**contract.get("contract", {}), "publishing_inputs_v2": deepcopy(pending["candidate"])}
        contract["contract"].pop("publishing_inputs_v1", None)
        contract["verification"] = deepcopy(receipt)
        target.contract = contract
        target.capabilities = {**target.capabilities, "requiredProofVersion": 4, "nativeSetupVerified": True, "publishingReady": False}
        target.save(update_fields=["contract", "capabilities", "updated_at"])
        config.publish_targets = [row for row in config.publish_targets if row.get("target_id") != target.target_key] + [deepcopy(contract)]
        config.save(update_fields=["publish_targets", "updated_at"])
        child.state, child.receipt = "completed", deepcopy(receipt)
        child.save(update_fields=["state", "receipt", "updated_at"])
        run.status, run.result = "completed", {"verification": deepcopy(proof)}
        run.save(update_fields=["status", "result", "updated_at"])
        original.state, original.receipt = "completed", {"status": "setup_verified", "child_id": str(child.pk),
            "evidence_digest": proof["evidence_digest"], "target_digest": protocol.digest(contract), "checked_at": checked_at}
        original.save(update_fields=["state", "receipt", "updated_at"])
        return child


def _validate(proof, pending):
    try:
        return validate_native_setup(proof, candidate=pending["candidate"], records=pending["records"], identity=pending["identity"],
            supported_profile=pending["profile"]["native"], original_binding=pending["original_binding"],
            current_binding=pending["current_binding"], origin="authenticated_hosted")
    except (ValueError, KeyError, TypeError):
        raise _invalid() from None


def _replay(child, original, connection, target, config, context, proof, profile):
    _fence(original, original.payload["context"], profile)
    saved = original.payload["context"]
    actual = deepcopy(context)
    actual["target"] = saved["target"]
    actual["snapshot"]["accepted_target_digest"] = saved["snapshot"]["accepted_target_digest"]
    pending = _pending(child, original, original.payload["context"], profile)
    _validate(proof, pending)
    run = _run(child, connection)
    receipt = child.receipt
    if (actual != saved or protocol.canonical(actual) != protocol.canonical(saved)
            or child.state != "completed" or original.receipt.get("child_id") != str(child.pk)
            or run.status != "completed" or run.result.get("verification") != proof
            or receipt.get("status") != "setup_verified" or receipt.get("allowed") is not False
            or receipt.get("attestation_origin") != "authenticated_hosted" or receipt.get("native_proof") != proof
            or original.receipt.get("evidence_digest") != proof["evidence_digest"]
            or original.receipt.get("checked_at") != receipt.get("checked_at")
            or target.contract.get("verification") != receipt or protocol.digest(target.contract) != original.receipt.get("target_digest")
            or [row for row in config.publish_targets if row.get("target_id") == target.target_key] != [target.contract]
            or target.capabilities.get("requiredProofVersion") != 4 or target.capabilities.get("publishingReady") is not False):
        raise _changed()
    return child
