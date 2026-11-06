"""Closed hosted-only schema3 receipts, bound to original frozen execution inputs.

This pure validator cannot authenticate a transport or promote a target. Its
caller supplies authenticated origin, saved operation binding and a verifier
identity from a trusted local manifest, then repeats current-scope checks before
an atomic promotion. Candidate-selected hashes alone are never authority.
"""

from copy import deepcopy
import hashlib
import re
from uuid import UUID

from .website_contract import WebsiteAuthorityError
from .website_failure_contract import canonical_bytes
from .website_publishing_inputs import validate_candidate

MAX_PROOF_BYTES = 65536
SHA = re.compile(r"[a-f0-9]{40}(?:[a-f0-9]{24})?")
DIGEST = re.compile(r"[a-f0-9]{64}")
BOOLEANS = frozenset({"content_loading", "listing", "detail", "unknown_slug_404", "canonical",
    "assets", "browser", "narrow_layout", "wide_layout"})
SCOPE_FIELDS = frozenset({"repository_id", "github_repo", "website_connection_id", "connection_generation",
    "operation_id", "operation_attempt", "deletion_epoch", "target_id", "contract_digest"})
SEALED_FIELDS = frozenset({"publishing_inputs_digest", "baseline_source_sha", "verifier_sha256"})
PROOF_FIELDS = SCOPE_FIELDS | SEALED_FIELDS | BOOLEANS | frozenset({"schema_version", "source_sha",
    "adapter_id", "adapter_version", "source_tree_sha", "environment_fingerprint", "lockfile_digests",
    "artifact_digest", "baseline_build", "patched_build", "verified_routes", "evidence_digest"})
VERIFIER_FIELDS = frozenset({"profile", "runner_sha256", "execution_schema_version", "python_version", "playwright_version"})


def _integer(value, minimum=1):
    return type(value) is int and minimum <= value < 2**63


def _text(value, maximum=4096):
    return type(value) is str and 0 < len(value) <= maximum and all(ord(char) >= 32 for char in value)


def _match(pattern, value):
    return type(value) is str and bool(pattern.fullmatch(value))


def _uuid(value):
    try:
        return type(value) is str and str(UUID(value)) == value
    except (ValueError, AttributeError):
        return False


def _exact(left, right):
    return type(left) is type(right) and left == right


def _proof_shape(proof):
    if type(proof) is not dict or set(proof) != PROOF_FIELDS:
        return False
    if type(proof["schema_version"]) is not int or proof["schema_version"] != 3:
        return False
    if any(not _integer(proof[key], 0 if key == "deletion_epoch" else 1) for key in (
            "repository_id", "connection_generation", "operation_attempt", "deletion_epoch", "adapter_version")):
        return False
    if any(not _uuid(proof[key]) for key in ("website_connection_id", "operation_id")):
        return False
    if (not _text(proof["github_repo"], 201) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}", proof["github_repo"])
            or not _text(proof["target_id"], 256) or not _text(proof["adapter_id"], 100)):
        return False
    if any(not _match(SHA, proof[key]) for key in ("source_sha", "source_tree_sha", "baseline_source_sha")):
        return False
    if any(not _match(DIGEST, proof[key]) for key in ("contract_digest", "environment_fingerprint", "artifact_digest",
            "evidence_digest", "publishing_inputs_digest", "verifier_sha256")):
        return False
    if (any(proof[key] is not True for key in BOOLEANS)
            or type(proof["baseline_build"]) is not str or proof["baseline_build"] != "passed"
            or type(proof["patched_build"]) is not str or proof["patched_build"] != "passed"):
        return False
    locks, routes = proof["lockfile_digests"], proof["verified_routes"]
    if (type(locks) is not dict or len(locks) > 20
            or any(not _text(key) or not _match(DIGEST, value) for key, value in locks.items())
            or type(routes) is not dict or set(routes) != {"listing", "detail", "unknown_slug"}
            or any(not _text(route) for route in routes.values())):
        return False
    return True


def _verifier_supported(candidate_verifier, supported):
    """The trusted manifest, not the candidate, chooses supported runner bytes."""
    if type(supported) is not dict or set(supported) != VERIFIER_FIELDS:
        return False
    if (supported["profile"] != "native_ci_v1" or not _match(DIGEST, supported["runner_sha256"])
            or type(supported["execution_schema_version"]) is not int or supported["execution_schema_version"] != 1
            or not _text(supported["python_version"], 20) or not re.fullmatch(r"\d+\.\d+(?:\.\d+)?", supported["python_version"])
            or supported["playwright_version"] != "1.58.0"):
        return False
    return canonical_bytes(candidate_verifier) == canonical_bytes(supported)


def sealed_evidence_digest(proof):
    """Return finite UTF-8 schema3 evidence bytes without changing legacy v2."""
    return hashlib.sha256(canonical_bytes({key: value for key, value in proof.items() if key != "evidence_digest"})).hexdigest()


def validate_hosted_sealed_proof(proof, *, origin, binding, candidate, supported_verifier):
    """Validate authenticated hosted evidence against a saved exact-input request.

    ``binding`` must be the trusted original child request with current authority
    rechecked by the caller. ``supported_verifier`` comes from a local pinned
    manifest. Neither value may be populated from the receipt being validated.
    A returned receipt grants no publication authority by itself.
    """
    try:
        if type(origin) is not str or origin != "authenticated_hosted" or not _proof_shape(proof) or type(binding) is not dict:
            raise ValueError
        # Closed shape and scalar bounds precede canonicalization; NaN, huge
        # integers, nested data and arbitrary provider fields cannot enter it.
        if len(canonical_bytes(proof)) > MAX_PROOF_BYTES or sealed_evidence_digest(proof) != proof["evidence_digest"]:
            raise ValueError
        sealed = validate_candidate(candidate)
        verification = sealed["payload"]["verification"]
        execution, oracle = verification["execution"], verification["oracle"]
        target = sealed["payload"]["publishing"]["target"]
        baseline = verification["baseline_policy"]
        if (type(baseline) is not dict or set(baseline) != {"kind", "source_sha"}
                or baseline["kind"] != "fixed_setup_source" or not _match(SHA, baseline["source_sha"])
                or not _verifier_supported(verification["verifier"], supported_verifier)):
            raise ValueError
        expected = {**{key: binding.get(key) for key in SCOPE_FIELDS | SEALED_FIELDS},
            "source_sha": binding.get("expected_source_sha")}
        if any(not _exact(proof[key], value) for key, value in expected.items()):
            raise ValueError
        if "source_sha" in binding and not _exact(binding["source_sha"], proof["source_sha"]):
            raise ValueError
        selection = sealed["payload"]["publishing"]["source_selection"]
        if any(not _exact(binding.get(key), value) for key, value in selection.items()):
            raise ValueError
        frozen = {"publishing_inputs_digest": sealed["digest"], "baseline_source_sha": baseline["source_sha"],
            "verifier_sha256": supported_verifier["runner_sha256"], "target_id": target["target_id"],
            "artifact_digest": oracle["artifact_digest"], "adapter_id": target["delivery_adapter"], "adapter_version": 1}
        if any(not _exact(proof[key], value) for key, value in frozen.items()):
            raise ValueError
        routes = {"listing": execution["listing_route"],
            "detail": execution["route_template"].replace("{slug}", oracle["seed_slug"]),
            "unknown_slug": execution["route_template"].replace("{slug}", "mlai-nonexistent-" + oracle["artifact_digest"][:12])}
        if (proof["verified_routes"] != routes or len(set(routes.values())) != 3
                or any("{" in route or "}" in route for route in routes.values())
                or set(proof["lockfile_digests"]) != set(execution["lockfiles"])):
            raise ValueError
        return deepcopy(proof)
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError, RecursionError, UnicodeError):
        raise WebsiteAuthorityError("ci_attestation_contract_mismatch",
            "Hosted evidence does not match the original sealed execution inputs.") from None
