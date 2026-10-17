"""Native per-site goal ledger; provider proofs never masquerade as Git facts."""

from copy import deepcopy
from datetime import datetime, timedelta
import re
from uuid import UUID

from .channel_artifact_contract import canonical_bytes, digest
from .cms_connection_contract import validate_binding, CmsContractError
from .website_goal import MILESTONES

KINDS = {"access": "cms_access", "understood": "cms_inventory", "integrated": "cms_native_policy",
    "previewed": "cms_staged_article", "approved": "cms_native_approval", "live": "cms_public_article",
    "first_draft": "cms_native_handoff", "first_article_live": "cms_public_article"}
SOURCE_MILESTONES = {"previewed", "approved", "live", "first_draft", "first_article_live"}
SCOPE_FIELDS = {"binding", "policy_operation_id", "policy_digest"}
SOURCE_FIELDS = {"run_id", "run_generation", "envelope_digest"}
PROOF_FIELDS = {"kind", "scope_digest", "source", "subject_digest", "at", "digest"}
GOAL_FIELDS = {"schema_version", "kind", "goal_id", "scope", "source", "revision", "started_at", "milestones", "proof_history"}
HEX = re.compile(r"[a-f0-9]{64}")


class CmsGoalError(ValueError):
    """A malformed or superseded native ledger cannot authorize continuation."""


def _time(value):
    if not isinstance(value, str) or len(value) > 40:
        raise CmsGoalError("The native goal timestamp changed.")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0) or parsed.isoformat() != value:
        raise CmsGoalError("The native goal timestamp changed.")
    return parsed


def _hex(value):
    return isinstance(value, str) and HEX.fullmatch(value) is not None


def scope(binding, *, policy_operation_id, policy_digest):
    """Bind actual provider/site/owner/policy identities, with no repository SHA."""
    try:
        if not isinstance(policy_operation_id, str) or str(UUID(policy_operation_id)) != policy_operation_id or not _hex(policy_digest):
            raise CmsGoalError("The native policy reference changed.")
        return {"binding": deepcopy(validate_binding(binding)), "policy_operation_id": policy_operation_id, "policy_digest": policy_digest}
    except (CmsContractError, ValueError, TypeError, AttributeError):
        raise CmsGoalError("The native goal scope changed.") from None


def validate_scope(value):
    if not isinstance(value, dict) or set(value) != SCOPE_FIELDS:
        raise CmsGoalError("The native goal scope changed.")
    expected = scope(**value)
    if canonical_bytes(value) != canonical_bytes(expected):
        raise CmsGoalError("The native goal scope changed.")
    return expected


def source(value):
    """Identify a real original CF run generation and its private envelope."""
    if value is None:
        return None
    if (not isinstance(value, dict) or set(value) != SOURCE_FIELDS or
            not isinstance(value["run_id"], str) or re.fullmatch(r"[A-Za-z0-9_-]{1,100}", value["run_id"]) is None or
            type(value["run_generation"]) is not int or not 0 <= value["run_generation"] < 10**12 or not _hex(value["envelope_digest"])):
        raise CmsGoalError("The original native article changed.")
    return deepcopy(value)


def new_goal(identifier, expected_scope, *, now):
    """Create one native ledger; a Git merge is explicitly not applicable."""
    if not isinstance(identifier, str) or str(UUID(identifier)) != identifier:
        raise CmsGoalError("The native goal identifier changed.")
    return {"schema_version": 1, "kind": "cms_site_goal", "goal_id": identifier, "scope": validate_scope(expected_scope),
        "source": None, "revision": 0, "started_at": _time(now.isoformat()).isoformat(),
        "milestones": [{"name": name, "required": name != "merged", "state": "not_required" if name == "merged" else "pending", "proof": None}
            for name in MILESTONES], "proof_history": []}


def validate_goal(goal, expected_scope):
    """Validate the closed ledger independently of its currently fresh proofs."""
    try:
        if (not isinstance(goal, dict) or set(goal) != GOAL_FIELDS or type(goal["schema_version"]) is not int or goal["schema_version"] != 1 or
                goal["kind"] != "cms_site_goal" or not isinstance(goal["goal_id"], str) or str(UUID(goal["goal_id"])) != goal["goal_id"] or
                type(goal["revision"]) is not int or goal["revision"] < 0 or
                canonical_bytes(validate_scope(goal["scope"])) != canonical_bytes(validate_scope(expected_scope)) or
                not isinstance(goal["proof_history"], list) or len(goal["proof_history"]) > 32):
            raise CmsGoalError("The native goal changed.")
        _time(goal["started_at"]); source(goal["source"])
        rows = goal["milestones"]
        if not isinstance(rows, list) or len(rows) != len(MILESTONES):
            raise CmsGoalError("The native milestone ledger changed.")
        for name, row in zip(MILESTONES, rows):
            if (not isinstance(row, dict) or set(row) != {"name", "required", "state", "proof"} or row["name"] != name or
                    type(row["required"]) is not bool or row["required"] != (name != "merged") or
                    row["state"] not in {"pending", "passed", "not_required", "invalidated"} or
                    name == "merged" and (row["state"] != "not_required" or row["proof"] is not None) or
                    name != "merged" and row["state"] == "not_required" or
                    (row["state"] == "passed") != (row["proof"] is not None) or
                    row["proof"] is not None and (not isinstance(row["proof"], dict) or not valid_proof(name, row["proof"],
                        expected_scope=expected_scope, original_source=goal["source"], now=_time(row["proof"]["at"])))):
                raise CmsGoalError("The native milestone ledger changed.")
        for row in goal["proof_history"]:
            if (not isinstance(row, dict) or set(row) != {"name", "proof_digest", "invalidated_at"} or
                    row["name"] not in KINDS or not _hex(row["proof_digest"])):
                raise CmsGoalError("The native proof history changed.")
            _time(row["invalidated_at"])
        return deepcopy(goal)
    except CmsGoalError:
        raise
    except (CmsContractError, ValueError, TypeError, KeyError, AttributeError, RecursionError):
        raise CmsGoalError("The native goal changed.") from None


def make_proof(name, *, expected_scope, original_source, subject_digest, at):
    """Seal references accepted by the owning private validator, never JSON consent."""
    if name not in KINDS or not _hex(subject_digest):
        raise CmsGoalError("The native proof kind changed.")
    bound = source(original_source) if name in SOURCE_MILESTONES else None
    if name in SOURCE_MILESTONES and bound is None:
        raise CmsGoalError("The native proof has no original article.")
    value = {"kind": KINDS[name], "scope_digest": digest(validate_scope(expected_scope)), "source": bound,
        "subject_digest": subject_digest, "at": _time(at.isoformat()).isoformat()}
    return {**value, "digest": digest(value)}


def valid_proof(name, proof, *, expected_scope, original_source, now):
    """Public proof is fresh; durable native policy/consent stays bound to scope."""
    try:
        if name not in KINDS or not isinstance(proof, dict) or set(proof) != PROOF_FIELDS:
            return False
        at = _time(proof["at"])
        expected = make_proof(name, expected_scope=expected_scope, original_source=original_source,
            subject_digest=proof["subject_digest"], at=at)
        if canonical_bytes(proof) != canonical_bytes(expected) or at > now + timedelta(seconds=30):
            return False
        max_age = timedelta(minutes=15) if name in {"live", "first_article_live"} else timedelta(days=7) if name in {"access", "understood"} else None
        return max_age is None or at >= now - max_age
    except (CmsGoalError, ValueError, TypeError, KeyError, AttributeError, RecursionError):
        return False


def bind_source(goal, value, *, expected_scope, now):
    """Bind the real paid run; a newer generation invalidates dependent proofs."""
    result = validate_goal(goal, expected_scope)
    selected = source(value)
    if selected is None:
        raise CmsGoalError("The original native article is required.")
    prior = result["source"]
    if prior is not None and (prior["run_id"] != selected["run_id"] or prior["envelope_digest"] != selected["envelope_digest"] or
            selected["run_generation"] < prior["run_generation"]):
        raise CmsGoalError("The original native article cannot be replaced.")
    if prior == selected:
        return result
    result["source"] = selected
    for row in result["milestones"]:
        if row["name"] in SOURCE_MILESTONES and row["proof"] is not None:
            result["proof_history"].append({"name": row["name"], "proof_digest": row["proof"]["digest"], "invalidated_at": _time(now.isoformat()).isoformat()})
            row.update(state="invalidated", proof=None)
    result["proof_history"] = result["proof_history"][-32:]
    result["revision"] += 1
    return result


def milestone_passed(name, proof, *, proofs, expected_scope, original_source, now):
    """Keep completed preparation at its real draft time after owner review.

    Only a freshly verified publication of that exact source, with its original
    draft and subsequent approval, can establish this past milestone. Ordinary
    access freshness and every new-work admission remain unchanged.
    """
    clock = now
    if name in {"access", "understood"} and isinstance(proofs, dict):
        try:
            draft, approval, live, first = (proofs[key] for key in ("first_draft", "approved", "live", "first_article_live"))
            if (all(valid_proof(key, proofs[key], expected_scope=expected_scope, original_source=original_source, now=now)
                    for key in ("first_draft", "approved", "live", "first_article_live")) and
                    canonical_bytes(live) == canonical_bytes(first) and
                    _time(draft["at"]) <= _time(approval["at"]) <= _time(first["at"])):
                clock = _time(draft["at"])
        except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
            pass
    return valid_proof(name, proof, expected_scope=expected_scope, original_source=original_source, now=clock)


def refresh(goal, proofs, *, expected_scope, now):
    """Rebuild milestones from current private facts, with bounded invalidations."""
    result = validate_goal(goal, expected_scope)
    if not isinstance(proofs, dict) or set(proofs) - set(KINDS):
        raise CmsGoalError("The native proof set changed.")
    for row in result["milestones"]:
        name = row["name"]
        if name == "merged":
            continue
        candidate, old = proofs.get(name), row["proof"]
        passed = milestone_passed(name, candidate, proofs=proofs, expected_scope=expected_scope, original_source=result["source"], now=now)
        if old and (not passed or old["digest"] != candidate["digest"]):
            result["proof_history"].append({"name": name, "proof_digest": old["digest"], "invalidated_at": _time(now.isoformat()).isoformat()})
        row.update(state="passed" if passed else "invalidated" if old else "pending", proof=deepcopy(candidate) if passed else None)
    result["proof_history"] = result["proof_history"][-32:]
    result["revision"] += 1
    return result


def complete(goal, *, expected_scope, now):
    """A native goal cannot finish at consent, draft or provider acknowledgement."""
    try:
        value = validate_goal(goal, expected_scope)
        proofs = {row["name"]: row["proof"] for row in value["milestones"]}
        return all(not row["required"] or row["state"] == "passed" and milestone_passed(row["name"], row["proof"], proofs=proofs,
            expected_scope=expected_scope, original_source=value["source"], now=now) for row in value["milestones"])
    except CmsGoalError:
        return False
