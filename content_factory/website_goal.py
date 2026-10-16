"""Pure milestone evaluation for server-reserved, scoped Articles goals."""

from copy import deepcopy
from datetime import datetime, timedelta
import hashlib
import json
import re

MILESTONES = ("access", "understood", "integrated", "previewed", "approved", "merged", "live", "first_draft", "first_article_live")
CLASSES = ("transient", "waiting", "repairable", "strategy", "needs_user", "unsupported")
SCOPE_FIELDS = ("company_id", "organization_id", "connection_id", "connection_generation", "repository_id", "branch", "app_root", "configuration_revision", "deletion_epoch")
ACTION_FOR = {"access": "verify-access", "understood": "scan", "integrated": "setup", "previewed": "ci-attestation", "approved": "setup", "merged": "merge", "live": "verify",
    "first_draft":"first-article","first_article_live":"first-article"}
PROOF_KINDS = {
    "access": {"repository_access"}, "understood": {"repository_scan"},
    "integrated": {"verified_target", "setup_candidate"},
    "previewed": {"authenticated_ci", "authenticated_hosted", "setup_preview"},
    "approved": {"backend_approval", "no_mutation_required"},
    "merged": {"backend_merge", "no_mutation_required"}, "live": {"deployment_marker"},
    "first_draft": {"article_draft"}, "first_article_live": {"article_deployment_marker"},
}
SHA = re.compile(r"[a-f0-9]{40}(?:[a-f0-9]{24})?")
DIGEST = re.compile(r"[a-f0-9]{64}")
FINGERPRINT = re.compile(r"(?:sha256:)?[a-f0-9]{64}")
MAX_HISTORY = 32
MAX_OCCURRENCES = 256


def mapping(value):
    """Return only objects, without coercing untrusted scalar aliases."""
    return value if isinstance(value, dict) else {}


def digest(value):
    """Hash canonical finite JSON; no raw evidence is retained in a proof."""
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def stamp(value):
    """Read an aware timestamp without treating an invalid value as fresh."""
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
        return result if isinstance(result, datetime) and result.tzinfo else None
    except (ValueError, TypeError, OverflowError):
        return None


def scope_for(connection, company_id):
    """Bind only saved authority; worker input cannot select these fields."""
    from .website_operations import deletion_epoch
    return {"company_id": str(company_id), "organization_id": str(connection.organization_id),
        "connection_id": str(connection.pk), "connection_generation": connection.generation,
        "repository_id": str(connection.repository_id), "branch": connection.branch,
        "app_root": connection.app_root, "configuration_revision": connection.configuration_version,
        "deletion_epoch": deletion_epoch(connection)}


def scope_matches(value, expected):
    """Require exact original scope, including integer types and all fields."""
    return (isinstance(value, dict) and isinstance(expected, dict) and set(value) == set(expected) == set(SCOPE_FIELDS)
        and all(key in value and key in expected and type(value[key]) is type(expected[key])
                and value[key] == expected[key] for key in SCOPE_FIELDS))


def new_goal(identifier, scope, *, target="ready", now):
    """Create the fixed milestone ledger; unavailable usage is explicitly null."""
    if target not in {"ready", "first_article_live"}:
        raise ValueError("Unknown Articles goal target")
    return {"schema_version": 1, "goal_id": str(identifier), "revision": 0, "target": target,
        "started_at": now.isoformat(), "scope": deepcopy(scope),
        "milestones": [{"name": name, "state": "pending" if target == "first_article_live" or index < 7 else "not_required",
                        "required": target == "first_article_live" or index < 7, "proof": None}
                       for index, name in enumerate(MILESTONES)],
        "attempts_by_class": {name: 0 for name in CLASSES},
        "budget_used": {"active_seconds": None, "model_cost": None}, "strategy_pin": None,
        "fingerprints_seen": [], "failure_occurrences": [], "consent": None, "proof_history": []}


def valid_goal(goal, scope):
    """Refuse malformed or future ledgers instead of redispatching as legacy."""
    milestones = mapping(goal).get("milestones")
    return (isinstance(goal, dict) and type(goal.get("schema_version")) is int and goal["schema_version"] == 1
        and isinstance(goal.get("goal_id"), str) and 0 < len(goal["goal_id"]) <= 100
        and type(goal.get("revision")) is int and goal["revision"] >= 0
        and isinstance(goal.get("target"), str) and goal["target"] in {"ready", "first_article_live"} and scope_matches(goal.get("scope"), scope)
        and isinstance(goal.get("attempts_by_class"), dict)
        and all(type(goal["attempts_by_class"].get(name)) is int and goal["attempts_by_class"][name] >= 0 for name in CLASSES)
        and isinstance(goal.get("failure_occurrences"), list) and len(goal["failure_occurrences"]) <= MAX_OCCURRENCES
        and all(isinstance(value, str) and len(value) <= 160 for value in goal["failure_occurrences"])
        and isinstance(goal.get("fingerprints_seen"), list) and len(goal["fingerprints_seen"]) <= MAX_OCCURRENCES
        and all(isinstance(value, str) and FINGERPRINT.fullmatch(value) for value in goal["fingerprints_seen"])
        and isinstance(milestones, list) and len(milestones) == len(MILESTONES)
        and all(isinstance(item, dict) and item.get("name") == name and type(item.get("required")) is bool
                and item["required"] == (goal["target"] == "first_article_live" or index < 7)
                for index, (item, name) in enumerate(zip(milestones, MILESTONES))))


def make_proof(name, kind, *, scope, source_sha, sha, run_id, evidence, at):
    """Seal an allowlisted reference after an owning backend validator accepts it."""
    if name not in PROOF_KINDS or kind not in PROOF_KINDS[name]:
        raise ValueError("Unknown milestone proof kind")
    return {"kind": kind, "scope": deepcopy(scope), "source_sha": source_sha, "sha": sha,
        "run_id": run_id, "evidence_digest": digest(evidence), "at": at.isoformat() if isinstance(at, datetime) else at}


def valid_proof(name, proof, *, scope, source_sha, now, max_age=timedelta(days=7)):
    """Validate scope, phase identity, digest and freshness; a message proves nothing."""
    if (not isinstance(proof, dict) or set(proof) != {"kind", "scope", "source_sha", "sha", "run_id", "evidence_digest", "at"}
            or not isinstance(proof.get("kind"), str) or proof["kind"] not in PROOF_KINDS.get(name, ())):
        return False
    at = stamp(proof.get("at"))
    durable = name in {"understood", "integrated", "approved", "merged"}
    if (not scope_matches(proof.get("scope"), scope) or not at or at > now + timedelta(minutes=5)
            or not durable and at < now - max_age
            or not isinstance(proof.get("evidence_digest"), str) or not DIGEST.fullmatch(proof["evidence_digest"])):
        return False
    if name != "access" and (not isinstance(source_sha, str) or not SHA.fullmatch(source_sha)
            or proof.get("source_sha") != source_sha or not isinstance(proof.get("sha"), str) or not SHA.fullmatch(proof["sha"])):
        return False
    run_id = proof.get("run_id")
    return run_id is None or isinstance(run_id, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,100}", run_id) is not None


def refresh_goal(goal, proofs, *, scope, source_sha, now):
    """Replace proofs from current validated facts, retaining bounded invalidation history."""
    if not valid_goal(goal, scope):
        raise ValueError("The saved goal does not match current authority")
    result = deepcopy(goal)
    history = list(result.get("proof_history") or [])[-MAX_HISTORY:]
    for item in result["milestones"]:
        name, old = item["name"], item.get("proof")
        candidate = mapping(proofs).get(name)
        passed = valid_proof(name, candidate, scope=scope, source_sha=source_sha, now=now)
        if old and (not passed or any(mapping(old).get(key) != mapping(candidate).get(key)
                                     for key in ("kind", "scope", "source_sha", "sha", "run_id", "evidence_digest"))):
            history.append({"name": name, "evidence_digest": mapping(old).get("evidence_digest"), "invalidated_at": now.isoformat()})
        if passed and item.get("state") != "passed":
            item["observed_at"] = now.isoformat()
        item["proof"] = deepcopy(candidate) if passed else None
        item["state"] = "passed" if passed else "not_required" if not item["required"] else "invalidated" if old else "pending"
    result["proof_history"] = history[-MAX_HISTORY:]
    result["revision"] += 1
    return result


def next_milestone(goal):
    """Choose the first required unproved milestone in the fixed sequence."""
    return next((item["name"] for item in mapping(goal).get("milestones", [])
                 if item.get("required") is True and item.get("state") != "passed"), None)


def complete(goal):
    """Require all target milestones; the first-article goal cannot finish at live."""
    milestones = mapping(goal).get("milestones")
    return bool(valid_goal(goal, mapping(goal).get("scope")) and next_milestone(goal) is None
        and all(not item["required"] or (mapping(item.get("proof")).get("kind") in PROOF_KINDS[item["name"]]
            and DIGEST.fullmatch(str(mapping(item.get("proof")).get("evidence_digest") or ""))) for item in milestones))


def record_failure(goal, *, failure_class, fingerprint, occurrence):
    """Count a causal occurrence once; transport replays and waits never charge it."""
    result = deepcopy(goal)
    if failure_class not in CLASSES or failure_class == "waiting" or not isinstance(occurrence, str) or not 0 < len(occurrence) <= 160:
        return result
    seen = list(result.get("failure_occurrences") or [])
    if occurrence in seen:
        return result
    if len(seen) >= MAX_OCCURRENCES:
        raise ValueError("The goal occurrence journal is full")
    seen.append(occurrence)
    result["failure_occurrences"] = seen
    counters = mapping(result.get("attempts_by_class"))
    result["attempts_by_class"] = {name: counters.get(name, 0) + (name == failure_class) for name in CLASSES}
    if isinstance(fingerprint, str) and FINGERPRINT.fullmatch(fingerprint):
        fingerprints = list(result.get("fingerprints_seen") or [])
        if fingerprint not in fingerprints:
            fingerprints.append(fingerprint)
        result["fingerprints_seen"] = fingerprints[-MAX_OCCURRENCES:]
    return result


def enrolled(organization_id):
    """Require independent runner and B1 policy switches plus trusted enrollment."""
    from django.conf import settings
    from .website_failure_scope import taxonomy_enabled
    return bool(getattr(settings, "ARTICLES_WEBSITE_GOAL_RUNNER_ENABLED", False)
        and str(organization_id) in getattr(settings, "ARTICLES_WEBSITE_GOAL_ENROLLED_ORGANIZATIONS", ())
        and taxonomy_enabled(organization_id))
