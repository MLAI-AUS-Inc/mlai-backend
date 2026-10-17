"""Separate private admission for a stopped article's editorial-only snapshot."""
from copy import deepcopy

if __package__:
    from .article_source_stop_contract import (canonical, digest, validate_request as validate_stop,
        validate_receipt as validate_stop_receipt, _uuid, _scope, _integer, _stamp, HASH, SHA)
else:
    from article_source_stop_contract import (canonical, digest, validate_request as validate_stop,
        validate_receipt as validate_stop_receipt, _uuid, _scope, _integer, _stamp, HASH, SHA)

ACTION = "article_source_history_seal"
MAX_REQUEST_BYTES = 49152
MAX_RECEIPT_BYTES = 32768
EDITORIAL_FILES = (
    ("plan_article", "article_plan.json"),
    ("assemble_article", "grounded_article_spec.json"),
    ("generate_content_images", "image_manifest.json"),
    ("plan_resource_asset", "resource_asset_plan.json"),
    ("generate_resource_pdf", "resource_manifest.json"),
    ("apply_component_feedback", "revision_spec_amendment.json"),
)


class HistoryContractError(ValueError):
    """Fixed diagnostics omit original article and credential content."""


def stopped_runs(stop_request, receipt):
    """Keep stable causal execution pins; volatile usage is never a read grant."""
    value = validate_stop_receipt(receipt, stop_request)
    return [{key: row[key] for key in ("run_id", "operation_id", "operation_attempt", "request_sha256",
        "origin_generation", "target_generation")} for row in value["runs"]]


def validate_request(value):
    """Bind a new read-only operation, current source and original fenced payment."""
    try:
        if (not isinstance(value, dict) or set(value) != {"schema_version", "kind", "operation_id", "operation_attempt",
                "stop_request", "stopped_runs", "read_scope", "current_source_sha", "digest"}
                or type(value["schema_version"]) is not int or value["schema_version"] != 1 or value["kind"] != ACTION
                or len(canonical(value)) > MAX_REQUEST_BYTES or not _uuid(value["operation_id"])
                or type(value["operation_attempt"]) is not int or value["operation_attempt"] != 1
                or not _scope(value["read_scope"]) or not isinstance(value["current_source_sha"], str)
                or not SHA.fullmatch(value["current_source_sha"])):
            raise ValueError
        stop = validate_stop(value["stop_request"])
        binding = stop["source_fence"]["binding"]
        if (value["operation_id"] in {stop["operation_id"], stop["source_fence_id"], *[row["operation_id"] for row in binding["runs"]]}
                or any(value["read_scope"][key] != binding["fence_scope"][key] for key in value["read_scope"] if key != "configuration_revision")
                or value["read_scope"]["configuration_revision"] < binding["fence_scope"]["configuration_revision"]
                or not isinstance(value["stopped_runs"], list) or len(value["stopped_runs"]) != len(binding["runs"])):
            raise ValueError
        expected = {row["run_id"]: row for row in binding["runs"]}
        seen = set()
        for row in value["stopped_runs"]:
            if (not isinstance(row, dict) or set(row) != {"run_id", "operation_id", "operation_attempt", "request_sha256", "origin_generation", "target_generation"}
                    or row["run_id"] not in expected or row["run_id"] in seen
                    or any(type(row[key]) is not type(expected[row["run_id"]][key]) or row[key] != expected[row["run_id"]][key]
                        for key in ("operation_id", "operation_attempt"))
                    or not all(_integer(row[key]) for key in ("origin_generation", "target_generation"))
                    or row["target_generation"] != row["origin_generation"] + 1
                    or not isinstance(row["request_sha256"], str) or not HASH.fullmatch(row["request_sha256"])):
                raise ValueError
            seen.add(row["run_id"])
        if value["digest"] != digest({key: item for key, item in value.items() if key != "digest"}):
            raise ValueError
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError, RecursionError):
        raise HistoryContractError("The original article history request is invalid") from None
    return deepcopy(value)


def make_request(*, operation_id, stop_request, stop_receipt, read_scope, current_source_sha):
    """Create a distinct operation; original paid identity remains inside its stop cause."""
    value = {"schema_version": 1, "kind": ACTION, "operation_id": operation_id, "operation_attempt": 1,
        "stop_request": deepcopy(stop_request), "stopped_runs": stopped_runs(stop_request, stop_receipt),
        "read_scope": deepcopy(read_scope), "current_source_sha": current_source_sha}
    value["digest"] = digest(value)
    return validate_request(value)


def validate_receipt(value, request):
    """A durable editorial observation never carries approval or effect authority."""
    request = validate_request(request)
    try:
        if (not isinstance(value, dict) or set(value) != {"schema_version", "kind", "request_digest", "source_fence_id", "source_run_id",
                "seal_sha256", "content_state", "file_count", "quiet", "effect_authority", "approval_carried", "sealed_at", "digest"}
                or type(value["schema_version"]) is not int or value["schema_version"] != 1 or value["kind"] != ACTION
                or len(canonical(value)) > MAX_RECEIPT_BYTES or value["request_digest"] != request["digest"]
                or value["source_fence_id"] != request["stop_request"]["source_fence_id"]
                or value["source_run_id"] != request["stop_request"]["source_fence"]["binding"]["root_run_id"]
                or not isinstance(value["seal_sha256"], str) or not HASH.fullmatch(value["seal_sha256"])
                or value["content_state"] not in {"partial", "draft_available"}
                or type(value["file_count"]) is not int or not 0 <= value["file_count"] <= len(EDITORIAL_FILES)
                or any(value[key] is not False for key in ("quiet", "effect_authority", "approval_carried"))
                or not _stamp(value["sealed_at"])
                or value["digest"] != digest({key: item for key, item in value.items() if key != "digest"})):
            raise ValueError
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError, RecursionError):
        raise HistoryContractError("The original article history receipt is invalid") from None
    return deepcopy(value)
