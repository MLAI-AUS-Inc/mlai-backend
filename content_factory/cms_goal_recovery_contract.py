"""Closed native recovery wire references; checksums never supply permission."""

from copy import deepcopy
from datetime import datetime, timedelta
import hashlib
import json
import re
from uuid import UUID, NAMESPACE_URL, uuid5

LOCATOR_FIELDS = {"schema_version", "kind", "goal_operation_id", "run_id", "envelope_digest", "run_generation", "state_version",
    "failure_occurrence_id", "failure_fingerprint", "failure_registry_sha256", "digest"}
RECEIPT_FIELDS = {"schema_version", "kind", "recovery_id", "locator_digest", "source_run_id", "target_generation", "step", "queued_at", "digest"}


class NativeRecoveryContractError(ValueError):
    """A changed native source or receipt cannot authorize another repair."""


def canonical_bytes(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _hex(value):
    return isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value) is not None


def _uuid(value):
    try:
        return isinstance(value, str) and str(UUID(value)) == value
    except (ValueError, TypeError, AttributeError):
        return False


def _time(value):
    try:
        parsed = datetime.fromisoformat(value)
        if (not isinstance(value, str) or len(value) > 40 or parsed.tzinfo is None or
                parsed.utcoffset() != timedelta(0) or parsed.isoformat() != value):
            raise ValueError("Invalid native recovery timestamp")
        return parsed
    except (ValueError, TypeError, AttributeError):
        raise NativeRecoveryContractError("The native recovery timestamp changed.") from None


def make_locator(*, goal_operation_id, run_id, envelope_digest, run_generation, state_version,
        failure_occurrence_id, failure_fingerprint, failure_registry_sha256):
    """Bind an actual original paid run and one registered causal observation."""
    if (not _uuid(goal_operation_id) or not _uuid(failure_occurrence_id) or
            not isinstance(run_id, str) or re.fullmatch(r"[A-Za-z0-9_-]{1,100}", run_id) is None or
            not _hex(envelope_digest) or not _hex(failure_registry_sha256) or
            any(type(item) is not int or not 0 <= item < 10**12 for item in (run_generation, state_version)) or
            not isinstance(failure_fingerprint, str) or re.fullmatch(r"sha256:[a-f0-9]{64}", failure_fingerprint) is None):
        raise NativeRecoveryContractError("The original native failure locator changed.")
    value = {"schema_version": 1, "kind": "cms_goal_recovery_locator", "goal_operation_id": goal_operation_id,
        "run_id": run_id, "envelope_digest": envelope_digest, "run_generation": run_generation, "state_version": state_version,
        "failure_occurrence_id": failure_occurrence_id, "failure_fingerprint": failure_fingerprint, "failure_registry_sha256": failure_registry_sha256}
    return {**value, "digest": digest(value)}


def validate_locator(value):
    """Require a closed locator; copying its digest grants no current goal lease."""
    try:
        if not isinstance(value, dict) or set(value) != LOCATOR_FIELDS:
            raise ValueError("Invalid locator fields")
        expected = make_locator(**{key: value[key] for key in LOCATOR_FIELDS - {"schema_version", "kind", "digest"}})
        if canonical_bytes(value) != canonical_bytes(expected):
            raise ValueError("Invalid locator")
        return deepcopy(expected)
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
        raise NativeRecoveryContractError("The original native failure locator changed.") from None


def recovery_id(locator):
    """One stable existing-queue intent per original failure, never per retry."""
    return uuid5(NAMESPACE_URL, "cms-goal-recovery:" + validate_locator(locator)["digest"]).hex


def make_receipt(locator, *, step, queued_at):
    """Describe one atomic generation/queue-intent reservation without model work."""
    locator = validate_locator(locator)
    if not isinstance(step, str) or re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,127}", step) is None:
        raise NativeRecoveryContractError("The native recovery step changed.")
    _time(queued_at)
    value = {"schema_version": 1, "kind": "cms_goal_recovery_receipt", "recovery_id": recovery_id(locator),
        "locator_digest": locator["digest"], "source_run_id": locator["run_id"], "target_generation": locator["run_generation"] + 1,
        "step": step, "queued_at": queued_at}
    return {**value, "digest": digest(value)}


def validate_receipt(value, locator):
    """Validate a private producer memo against its exact original failed source."""
    try:
        if not isinstance(value, dict) or set(value) != RECEIPT_FIELDS:
            raise ValueError("Invalid receipt fields")
        expected = make_receipt(locator, step=value["step"], queued_at=value["queued_at"])
        if canonical_bytes(value) != canonical_bytes(expected):
            raise ValueError("Invalid receipt")
        return deepcopy(expected)
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
        raise NativeRecoveryContractError("The original native recovery receipt changed.") from None
