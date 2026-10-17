"""Private source-stop observations; no receipt grants replacement effects."""
from copy import deepcopy
from datetime import datetime
import hashlib
import json
import re
from uuid import UUID

STOP_ACTION = "article_source_stop"
MAX_REQUEST_BYTES = 32768
MAX_RECEIPT_BYTES = 65536
RUN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}\Z")
HASH = re.compile(r"[a-f0-9]{64}\Z")
SHA = re.compile(r"[a-f0-9]{40}(?:[a-f0-9]{24})?\Z")
SCOPE_FIELDS = {"company_id", "organization_id", "connection_id", "connection_generation", "repository_id",
    "branch", "app_root", "configuration_revision", "deletion_epoch"}
ORIGIN_STATES = {"queued", "running", "awaiting_approval", "awaiting_confirmation", "awaiting_delivery_mode",
    "completed", "failed", "blocked"}


class StopContractError(ValueError):
    """Fixed diagnostics that omit private article, provider and request data."""


def canonical(value):
    """Encode finite JSON identically in both services."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()


def digest(value):
    """Compare exact metadata; a hash never authorizes a stop or read."""
    return hashlib.sha256(canonical(value)).hexdigest()


def _uuid(value):
    return isinstance(value, str) and str(UUID(value)) == value


def _integer(value, minimum=0):
    return type(value) is int and minimum <= value <= 10**12


def _decimal(value):
    return isinstance(value, str) and re.fullmatch(r"[1-9][0-9]{0,19}", value) is not None


def _stamp(value):
    return isinstance(value, str) and len(value) <= 128 and datetime.fromisoformat(value.replace("Z", "+00:00")).tzinfo is not None


def _domain(value):
    if (not isinstance(value, str) or not 0 < len(value) <= 253 or value != value.casefold()
            or any(ord(char) < 33 or char in "/\\:@?#[]%" for char in value)):
        return False
    labels = value.rstrip(".").encode("idna").decode("ascii").split(".")
    return len(labels) >= 2 and all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) is not None for label in labels)


def _scope(value):
    return (isinstance(value, dict) and set(value) == SCOPE_FIELDS and _uuid(value["connection_id"])
        and all(_decimal(value[key]) for key in ("company_id", "organization_id", "repository_id"))
        and all(_integer(value[key], minimum) for key, minimum in
            (("connection_generation", 1), ("configuration_revision", 1), ("deletion_epoch", 0)))
        and isinstance(value["branch"], str) and 0 < len(value["branch"]) <= 255
        and not any(ord(char) < 33 for char in value["branch"])
        and isinstance(value["app_root"], str) and len(value["app_root"]) <= 1024
        and (not value["app_root"] or not value["app_root"].startswith("/")
            and "\\" not in value["app_root"] and not any(ord(char) < 32 for char in value["app_root"])
            and all(part not in {"", ".", "..", ".git"} for part in value["app_root"].split("/"))))


def validate_request(value):
    """Bind one stop reservation to the immutable backend source-fence cause."""
    try:
        if (not isinstance(value, dict) or set(value) != {"schema_version", "operation_id", "operation_attempt",
                "source_fence_id", "source_fence", "domain", "github_repo", "digest"}
                or type(value["schema_version"]) is not int or value["schema_version"] != 1
                or len(canonical(value)) > MAX_REQUEST_BYTES or not _uuid(value["operation_id"])
                or not _uuid(value["source_fence_id"]) or value["operation_id"] == value["source_fence_id"]
                or not _integer(value["operation_attempt"], 1)
                or not _domain(value["domain"])
                or not isinstance(value["github_repo"], str)
                or re.fullmatch(r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}", value["github_repo"]) is None):
            raise ValueError
        fence = value["source_fence"]
        if (not isinstance(fence, dict) or set(fence) != {"schema_version", "binding", "recorded_at", "digest"}
                or type(fence["schema_version"]) is not int or fence["schema_version"] != 1
                or not _stamp(fence["recorded_at"]) or fence["digest"] != digest({k: v for k, v in fence.items() if k != "digest"})):
            raise ValueError
        binding = fence["binding"]
        if (not isinstance(binding, dict) or set(binding) != {"goal", "root_run_id", "source_scope", "source_sha",
                "fence_scope", "fence_source_sha", "billing", "runs"}
                or not _scope(binding["source_scope"]) or not _scope(binding["fence_scope"])
                or any(binding["source_scope"][key] != binding["fence_scope"][key] for key in SCOPE_FIELDS if key != "configuration_revision")
                or binding["source_scope"]["configuration_revision"] >= binding["fence_scope"]["configuration_revision"]
                or not all(isinstance(binding[key], str) and SHA.fullmatch(binding[key]) for key in ("source_sha", "fence_source_sha"))
                or binding["source_sha"] == binding["fence_source_sha"]):
            raise ValueError
        goal, billing = binding["goal"], binding["billing"]
        if (not isinstance(goal, dict) or set(goal) != {"goal_id", "authority_digest", "answers_digest", "owner_user_id"}
                or not _uuid(goal["goal_id"]) or not _decimal(goal["owner_user_id"])
                or not isinstance(goal["answers_digest"], str) or not HASH.fullmatch(goal["answers_digest"])
                or goal["authority_digest"] != digest({k: v for k, v in binding["source_scope"].items() if k != "configuration_revision"})
                or not isinstance(billing, dict) or set(billing) != {"client_request_id", "payer_user_id", "spend_ledger_id", "cost_points"}
                or not _decimal(billing["payer_user_id"]) or not _integer(billing["cost_points"])
                or (billing["spend_ledger_id"] is not None if billing["cost_points"] == 0 else not _decimal(billing["spend_ledger_id"]))
                or billing["client_request_id"] != "articles-first:" + digest({"goal_id": goal["goal_id"],
                    "scope": {k: v for k, v in binding["source_scope"].items() if k != "configuration_revision"}})
                or not isinstance(binding["runs"], list) or not 1 <= len(binding["runs"]) <= 2):
            raise ValueError
        identifiers = set()
        for node in binding["runs"]:
            if (not isinstance(node, dict) or set(node) != {"run_id", "operation_id", "operation_attempt", "run_request_digest",
                    "status", "operation_state", "execution_version"}
                    or not isinstance(node["run_id"], str) or not RUN.fullmatch(node["run_id"]) or node["run_id"] in identifiers
                    or not _uuid(node["operation_id"]) or node["operation_id"] == value["operation_id"]
                    or not _integer(node["operation_attempt"], 1) or node["status"] not in ORIGIN_STATES
                    or node["operation_state"] not in {"pending", "running", "completed", "failed", "blocked"}
                    or not isinstance(node["run_request_digest"], str) or not HASH.fullmatch(node["run_request_digest"])
                    or node["execution_version"] is not None and (not isinstance(node["execution_version"], list)
                        or len(node["execution_version"]) != 2 or not all(_integer(item) for item in node["execution_version"]))):
                raise ValueError
            identifiers.add(node["run_id"])
        if binding["root_run_id"] not in identifiers or value["digest"] != digest({k: v for k, v in value.items() if k != "digest"}):
            raise ValueError
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError, RecursionError):
        raise StopContractError("The article source stop request is invalid") from None
    return deepcopy(value)


def make_request(*, operation_id, source_fence_id, source_fence, domain, github_repo):
    """Create an exact single-attempt private stop request."""
    value = {"schema_version": 1, "operation_id": operation_id, "operation_attempt": 1,
        "source_fence_id": source_fence_id, "source_fence": deepcopy(source_fence), "domain": domain, "github_repo": github_repo}
    value["digest"] = digest(value)
    return validate_request(value)


def validate_receipt(value, request):
    """These observations deliberately cannot prove quiet or grant effects."""
    request = validate_request(request)
    try:
        if (not isinstance(value, dict) or set(value) != {"schema_version", "kind", "request_digest", "source_fence_id",
                "source_fence_digest", "local_execution_fenced", "quiet", "effect_authority", "inventory_complete", "runs", "observed_at", "digest"}
                or type(value["schema_version"]) is not int or value["schema_version"] != 1 or value["kind"] != STOP_ACTION
                or len(canonical(value)) > MAX_RECEIPT_BYTES or value["request_digest"] != request["digest"]
                or value["source_fence_id"] != request["source_fence_id"] or value["source_fence_digest"] != request["source_fence"]["digest"]
                or value["local_execution_fenced"] is not True or any(value[key] is not False for key in ("quiet", "effect_authority", "inventory_complete"))
                or not _stamp(value["observed_at"]) or not isinstance(value["runs"], list)
                or len(value["runs"]) != len(request["source_fence"]["binding"]["runs"])):
            raise ValueError
        expected = {node["run_id"]: node for node in request["source_fence"]["binding"]["runs"]}
        seen = set()
        for node in value["runs"]:
            if (not isinstance(node, dict) or set(node) != {"run_id", "operation_id", "operation_attempt", "origin_generation",
                    "target_generation", "state_version", "request_sha256", "activities", "models", "lease"}
                    or node["run_id"] not in expected or node["run_id"] in seen
                    or any(node[key] != expected[node["run_id"]][key] for key in ("operation_id", "operation_attempt"))
                    or any(not _integer(node[key]) for key in ("origin_generation", "target_generation", "state_version"))
                    or node["target_generation"] != node["origin_generation"] + 1
                    or not isinstance(node["request_sha256"], str) or not HASH.fullmatch(node["request_sha256"])
                    or node["activities"] not in {"unknown", "running", "closed"}
                    or node["models"] not in {"unknown", "running", "uncertain", "settled"}
                    or node["lease"] not in {"unknown", "held", "released"}):
                raise ValueError
            seen.add(node["run_id"])
        if value["digest"] != digest({k: v for k, v in value.items() if k != "digest"}):
            raise ValueError
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError, RecursionError):
        raise StopContractError("The article source stop receipt is invalid") from None
    return deepcopy(value)
