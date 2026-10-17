"""Closed private inspection wire format; checksums never grant authority."""
from copy import deepcopy
from datetime import datetime
import hashlib
import json
import re
from uuid import UUID

MAX_REQUEST_BYTES = 32768
MAX_RECEIPT_BYTES = 65536
SCOPE_FIELDS = {"company_id", "organization_id", "connection_id", "connection_generation", "repository_id",
    "branch", "app_root", "configuration_revision", "deletion_epoch"}
SHA = re.compile(r"[a-f0-9]{40}(?:[a-f0-9]{24})?\Z")
HASH = re.compile(r"[a-f0-9]{64}\Z")
RUN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}\Z")
INSPECTION_WORKFLOW = "article_source_refresh_inspect"


class RefreshContractError(ValueError):
    """Fixed diagnostics excluding private request, provider and article data."""


def canonical(value):
    """Return finite canonical JSON shared with the backend goal journal."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()


def digest(value):
    """Hash exact metadata for comparison, without authorizing any action."""
    return hashlib.sha256(canonical(value)).hexdigest()


def _uuid(value):
    return isinstance(value, str) and str(UUID(value)) == value


def _integer(value, minimum=0):
    return type(value) is int and minimum <= value <= 10**12


def _decimal(value):
    return isinstance(value, str) and re.fullmatch(r"[1-9][0-9]{0,19}", value) is not None


def _stamp(value):
    return isinstance(value, str) and len(value) <= 128 and datetime.fromisoformat(value.replace("Z", "+00:00")).tzinfo is not None


def _path(value, empty=False):
    return (isinstance(value, str) and len(value) <= 1024 and (bool(value) or empty)
        and not value.startswith("/") and "\\" not in value and not any(ord(char) < 32 for char in value)
        and re.match(r"^[A-Za-z]:", value) is None
        and (not value or all(part not in {"", ".", "..", ".git"} for part in value.split("/"))))


def _domain(value):
    if (not isinstance(value, str) or not 0 < len(value) <= 253 or value != value.casefold()
            or any(ord(char) < 33 or char in "/\\:@?#[]%" for char in value)):
        return False
    labels = value.rstrip(".").encode("idna").decode("ascii").split(".")
    return len(labels) >= 2 and all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) is not None for label in labels)


def validate_request(value):
    """Inspect only one current backend reservation and its original paid source."""
    try:
        if (not isinstance(value, dict) or set(value) != {"schema_version", "intent", "operation_id",
                "operation_attempt", "client_request_id", "domain", "github_repo", "digest"}
                or type(value["schema_version"]) is not int or value["schema_version"] != 1
                or len(canonical(value)) > MAX_REQUEST_BYTES or not _uuid(value["operation_id"])
                or not _integer(value["operation_attempt"], 1)
                or not _domain(value["domain"])
                or not isinstance(value["github_repo"], str)
                or re.fullmatch(r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}", value["github_repo"]) is None):
            raise ValueError
        intent = value["intent"]
        if (not isinstance(intent, dict) or set(intent) != {"version", "state", "id", "proposed_run_id", "binding", "created_at"}
                or type(intent["version"]) is not int or intent["version"] != 1
                or intent["state"] != "pending_reconciliation" or not _uuid(intent["proposed_run_id"])
                or not _stamp(intent["created_at"]) or not isinstance(intent["binding"], dict)
                or set(intent["binding"]) != {"goal", "current_scope", "source_sha", "paid_root", "billing"}):
            raise ValueError
        binding = intent["binding"]
        scope, root, goal, billing = (binding[key] for key in ("current_scope", "paid_root", "goal", "billing"))
        if (not all(isinstance(item, dict) for item in (scope, root, goal, billing))
                or set(scope) != SCOPE_FIELDS or not _uuid(scope["connection_id"])
                or any(not _decimal(scope[key]) for key in ("company_id", "organization_id", "repository_id"))
                or any(not _integer(scope[key], minimum) for key, minimum in
                    (("connection_generation", 1), ("configuration_revision", 1), ("deletion_epoch", 0)))
                or not _path(scope["app_root"], empty=True)
                or not isinstance(scope["branch"], str) or not 0 < len(scope["branch"]) <= 255
                or any(ord(char) < 33 for char in scope["branch"])
                or set(goal) != {"goal_id", "authority_digest", "answers_digest"} or not _uuid(goal["goal_id"])
                or not isinstance(goal["answers_digest"], str) or not HASH.fullmatch(goal["answers_digest"])
                or goal["authority_digest"] != digest({key: item for key, item in scope.items() if key != "configuration_revision"})
                or set(root) != {"run_id", "source_sha", "configuration_revision", "operation_id", "operation_attempt", "generation", "state_version"}
                or not isinstance(root["run_id"], str) or not RUN.fullmatch(root["run_id"]) or not _uuid(root["operation_id"])
                or root["run_id"] == intent["proposed_run_id"] or root["operation_id"] == value["operation_id"]
                or any(not _integer(root[key], minimum) for key, minimum in
                    (("configuration_revision", 1), ("operation_attempt", 1), ("generation", 0), ("state_version", 0)))
                or root["configuration_revision"] >= scope["configuration_revision"]
                or any(not isinstance(sha, str) or not SHA.fullmatch(sha) for sha in (root["source_sha"], binding["source_sha"]))
                or root["source_sha"] == binding["source_sha"] or len(root["source_sha"]) != len(binding["source_sha"])
                or set(billing) != {"client_request_id", "payer_user_id", "spend_ledger_id", "cost_points"}
                or billing["client_request_id"] != "articles-first:" + digest({"goal_id": goal["goal_id"],
                    "scope": {key: item for key, item in scope.items() if key != "configuration_revision"}})
                or not _decimal(billing["payer_user_id"]) or not _integer(billing["cost_points"])
                or (billing["spend_ledger_id"] is not None if billing["cost_points"] == 0 else not _decimal(billing["spend_ledger_id"]))
                or intent["id"] != digest(binding) or value["client_request_id"] != "articles-refresh-inspect:" + intent["id"]
                or value["digest"] != digest({key: item for key, item in value.items() if key != "digest"})):
            raise ValueError
        return deepcopy(value)
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError, OverflowError):
        raise RefreshContractError("The original article inspection request is invalid") from None


def make_request(*, intent, operation_id, operation_attempt, domain, github_repo):
    """Construct a closed request only after the backend reserves its read operation."""
    value = {"schema_version": 1, "intent": deepcopy(intent), "operation_id": operation_id,
        "operation_attempt": operation_attempt, "domain": domain.casefold(), "github_repo": github_repo,
        "client_request_id": "articles-refresh-inspect:" + intent["id"]}
    value["digest"] = digest(value)
    return validate_request(value)


def validate_inspection(value, request):
    """Validate content-free observations; no result authorizes a replacement write."""
    try:
        request = validate_request(request)
        binding = request["intent"]["binding"]
        root, scope = binding["paid_root"], binding["current_scope"]
        if (not isinstance(value, dict) or len(canonical(value)) > MAX_RECEIPT_BYTES
                or set(value) != {"schema_version", "kind", "request_digest", "intent_id", "source_run_id",
                    "source_generation", "source_state_version", "source_status", "inputs_sha256", "review_inputs_sha256",
                    "content_state", "superseded_by_run_id", "publications", "publication_refs", "publication_children", "observed_at", "digest"}
                or type(value["schema_version"]) is not int or value["schema_version"] != 1
                or value["kind"] != "article_source_refresh_inspection"
                or value["request_digest"] != request["digest"] or value["intent_id"] != request["intent"]["id"]
                or value["source_run_id"] != root["run_id"]
                or type(value["source_generation"]) is not int or value["source_generation"] != root["generation"]
                or not _integer(value["source_state_version"]) or value["source_state_version"] < root["state_version"]
                or value["source_status"] not in ("completed", "failed", "blocked", "awaiting_approval")
                or not isinstance(value["inputs_sha256"], str) or not HASH.fullmatch(value["inputs_sha256"])
                or value["review_inputs_sha256"] is not None and (not isinstance(value["review_inputs_sha256"], str)
                    or not HASH.fullmatch(value["review_inputs_sha256"]))
                or value["content_state"] not in ("partial", "draft_available", "review_pending", "superseded") or not _stamp(value["observed_at"])
                or not isinstance(value["publications"], list) or len(value["publications"]) > 32):
            raise ValueError
        superseded = value["superseded_by_run_id"]
        if ((superseded is not None) != (value["content_state"] == "superseded")
                or superseded is not None and (not isinstance(superseded, str) or not RUN.fullmatch(superseded)
                    or superseded in (root["run_id"], request["intent"]["proposed_run_id"]))):
            raise ValueError
        identifiers = set()
        for row in value["publications"]:
            if (not isinstance(row, dict) or set(row) != {"mutation_id", "run_id", "base_sha", "head_sha", "branch", "pr_url", "status", "files_sha256"}
                    or not isinstance(row["mutation_id"], str) or not HASH.fullmatch(row["mutation_id"])
                    or row["mutation_id"] in identifiers or not isinstance(row["run_id"], str) or not RUN.fullmatch(row["run_id"])
                    or not isinstance(row["base_sha"], str) or not SHA.fullmatch(row["base_sha"])
                    or not isinstance(row["head_sha"], str) or row["head_sha"] and not SHA.fullmatch(row["head_sha"])
                    or row["status"] == "applied" and not row["head_sha"]
                    or not isinstance(row["branch"], str) or not 0 < len(row["branch"]) <= 255
                    or any(ord(char) < 33 for char in row["branch"])
                    or row["status"] not in ("proposed", "applied")
                    or not isinstance(row["files_sha256"], str) or not HASH.fullmatch(row["files_sha256"])
                    or not isinstance(row["pr_url"], str) or row["pr_url"] and re.fullmatch(
                        r"https://github\.com/" + re.escape(request["github_repo"]) + r"/pull/[1-9][0-9]*", row["pr_url"], re.IGNORECASE) is None):
                raise ValueError
            identifiers.add(row["mutation_id"])
        if (not isinstance(value["publication_refs"], list) or len(value["publication_refs"]) > 16
                or not isinstance(value["publication_children"], list) or len(value["publication_children"]) > 1):
            raise ValueError
        children = set()
        for child in value["publication_children"]:
            if (not isinstance(child, dict) or set(child) != {"run_id", "status", "generation", "state_version"}
                    or not isinstance(child["run_id"], str) or not RUN.fullmatch(child["run_id"])
                    or child["run_id"] == root["run_id"] or child["run_id"] in children
                    or child["status"] not in ("queued", "running", "awaiting_approval", "awaiting_confirmation", "awaiting_delivery_mode", "completed", "failed", "blocked", "cancelled", "denied")
                    or not _integer(child["generation"]) or not _integer(child["state_version"])):
                raise ValueError
            children.add(child["run_id"])
        references = set()
        for row in value["publication_refs"]:
            if (not isinstance(row, dict) or set(row) != {"run_id", "number", "url"}
                    or row["run_id"] not in {root["run_id"], *children} or not _integer(row["number"], 1)
                    or row["url"] != f'https://github.com/{request["github_repo"]}/pull/{row["number"]}'
                    or (row["run_id"], row["number"]) in references):
                raise ValueError
            references.add((row["run_id"], row["number"]))
        if value["digest"] != digest({key: item for key, item in value.items() if key != "digest"}):
            raise ValueError
        return deepcopy(value)
    except (RefreshContractError, ValueError, TypeError, KeyError, AttributeError, RecursionError, OverflowError):
        raise RefreshContractError("The original article inspection receipt is invalid") from None
