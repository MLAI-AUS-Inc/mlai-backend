"""Pure setup v1 validation and canonical digests, shared with the producer."""

from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
import re

MAX_BYTES = 2 * 1024 * 1024
MAX_DEPTH = 16
SCHEMA_PATH = Path(__file__).resolve().parents[1] / "contracts" / "setup-delivery-v1.schema.json"
FORBIDDEN_KEYS = frozenset("""delivery_v1 delivery_intents delivery_journal delivery_receipts
lease leases ack acknowledgment acked_at payload_sha256 state_sha256 journal token access_token
refresh_token github_token github_access_token api_key authorization password private_key headers
cookies credentials service_credentials stdout stderr output output_excerpt source_content source_code
file_content content patch diff html traceback stack_trace logs log_excerpt log_tail stdout_tail stderr_tail
output_tail raw_response response_body source route_source template template_body script markup
file_contents raw_output error message detail details error_message command snippet excerpt error_excerpt""".split())


class DeliveryContractError(ValueError):
    """A bounded diagnostic containing field pointers, never payload contents."""

    def __init__(self, code, paths):
        self.code, self.paths = code, list(paths)[:12]
        super().__init__(code)


def canonical_bytes(value):
    """Use stable UTF-8 JSON; finite report floats are allowed, NaN is not."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def digest(value):
    """Hash canonical protocol JSON without transport metadata."""
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def state_digest(envelope):
    """Compare the same committed business projection across all delivery kinds."""
    return digest({"scope": envelope["scope"], "state_projection": envelope["body"]["state_projection"]})


def payload_digest(envelope):
    """Hash delivery identity and bytes together; a body-only hash is unsafe."""
    return digest({key: value for key, value in envelope.items() if key != "payload_sha256"})


def _pointer(path, key):
    from .website_failure_redaction import redact_secret_text
    safe = redact_secret_text(str(key))
    return path + "/" + safe.replace("~", "~0").replace("/", "~1")


def _schema_errors(value, schema, path="", document=None):
    document = document or schema
    if "$ref" in schema:
        reference = schema["$ref"]
        if reference.startswith("#/"):
            target = document
            for part in reference[2:].split("/"):
                target = target[part]
            return _schema_errors(value, target, path, document)
        if reference == "failure-envelope-v1.schema.json":
            from .website_failure_contract import FailureContractError, validate_failure
            try:
                validate_failure(value)
                return []
            except FailureContractError:
                return [path or "/"]
        raise ValueError("Unsupported local delivery schema reference")
    if "oneOf" in schema:
        matches = sum(not _schema_errors(value, choice, path, document) for choice in schema["oneOf"])
        if matches != 1:
            return [path or "/"]
    if "not" in schema and not _schema_errors(value, schema["not"], path, document):
        return [path or "/"]
    kinds = {"object": dict, "array": list, "string": str, "boolean": bool, "integer": int, "null": type(None), "number": (int, float)}
    kind = schema.get("type")
    allowed_kinds=kind if isinstance(kind,list) else [kind]
    if kind and not any(type(value) in kinds[name] if name=="number" else type(value) is kinds[name]
        for name in allowed_kinds):
        return [path or "/"]
    if "enum" in schema and value not in schema["enum"] or "const" in schema and value != schema["const"]:
        return [path or "/"]
    errors = []
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        errors.extend(_pointer(path, key) for key in schema.get("required", []) if key not in value)
        for key, item in value.items():
            child = properties.get(key)
            if child is None:
                extra = schema.get("additionalProperties", True)
                if extra is False:
                    errors.append(path + "/<unknown_field>")
                elif isinstance(extra, dict):
                    errors.extend(_schema_errors(item, extra, _pointer(path, key), document))
            else:
                errors.extend(_schema_errors(item, child, _pointer(path, key), document))
            if "propertyNames" in schema:
                errors.extend(_schema_errors(key, schema["propertyNames"], _pointer(path, key), document))
        if "maxProperties" in schema and len(value) > schema["maxProperties"]:
            errors.append(path or "/")
    elif isinstance(value, list):
        if len(value) > schema.get("maxItems", MAX_BYTES):
            errors.append(path or "/")
        for index, item in enumerate(value):
            errors.extend(_schema_errors(item, schema.get("items", {}), _pointer(path, index), document))
    elif isinstance(value, str):
        if not schema.get("minLength", 0) <= len(value) <= schema.get("maxLength", 65536):
            errors.append(path or "/")
        if "pattern" in schema and not re.fullmatch(schema["pattern"], value):
            errors.append(path or "/")
    elif type(value) is int:
        if ("minimum" in schema and value < schema["minimum"] or "maximum" in schema and value > schema["maximum"]):
            errors.append(path or "/")
    return errors[:12]


def _tree_errors(value, path="", depth=0):
    if depth > MAX_DEPTH:
        return [path or "/"]
    if isinstance(value, dict):
        if len(value) > 512:
            return [path or "/"]
        errors = []
        for key, item in value.items():
            if not isinstance(key, str) or len(key) > 256 or any(ord(char) < 32 for char in key):
                errors.append(path or "/")
                continue
            normalized = re.sub(r"(?<!^)(?=[A-Z])", "_", key).lower().replace("-", "_")
            if normalized in FORBIDDEN_KEYS or normalized.startswith("_delivery") or normalized.endswith("_source"):
                errors.append(_pointer(path, key))
            elif normalized == "code" and (not isinstance(item, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,256}", item)):
                errors.append(_pointer(path, key))
            else:
                errors.extend(_tree_errors(item, _pointer(path, key), depth + 1))
        return errors[:12]
    if isinstance(value, list):
        if len(value) > 512:
            return [path or "/"]
        errors = []
        for index, item in enumerate(value):
            errors.extend(_tree_errors(item, _pointer(path, index), depth + 1))
        return errors[:12]
    if isinstance(value, str):
        from .website_failure_redaction import redact_secret_text
        return [path or "/"] if len(value) > 4096 or "\x00" in value or redact_secret_text(value) != value else []
    if type(value) is float:
        return [] if math.isfinite(value) else [path or "/"]
    if type(value) not in {int, bool, type(None)}:
        return [path or "/"]
    return []


def _result_errors(result):
    """Check shapes consumed by setup/scan validators before any claim exists."""
    objects = {"article_system_setup", "article_system", "repository_inventory", "repo_execution_contract",
        "tech_stack", "setup_plan", "adoption", "live_preview", "directory_quality_gates",
        "directory_quality_verification", "directory_build_verification", "directory_build_report",
        "directory_static_report", "directory_dependency_report", "directory_browser_repair",
        "directory_visual_style_report", "directory_visual_repair", "repair_diagnostics", "failure"}
    errors = []
    for key in objects:
        if result.get(key) is not None and not isinstance(result[key], dict):
            errors.append("/body/state_projection/result/" + key)
        item = result.get(key)
        if isinstance(item, dict) and item.get("status") is not None and not isinstance(item["status"], str):
            errors.append("/body/state_projection/result/" + key + "/status")
    for key in ("status", "setup_status", "repair_state"):
        if result.get(key) is not None and not isinstance(result[key], str):
            errors.append("/body/state_projection/result/" + key)
    if result.get("repair_status") is not None and type(result["repair_status"]) not in {str, dict}:
        errors.append("/body/state_projection/result/repair_status")
    for key in ("planned_file_count", "created_file_count", "updated_file_count", "user_action_count", "resume_generation"):
        value = result.get(key)
        if value is not None and (type(value) is not int or value < 0):
            errors.append("/body/state_projection/result/" + key)
    targets = result.get("publish_targets")
    if targets is not None:
        if not isinstance(targets, list) or any(not isinstance(item, dict) for item in targets):
            errors.append("/body/state_projection/result/publish_targets")
        else:
            for item in targets:
                if item.get("verification") is not None and not isinstance(item["verification"], dict):
                    errors.append("/body/state_projection/result/publish_targets")
                    continue
                proof = item.get("verification") or {}
                if proof.get("status") is not None and not isinstance(proof["status"], str):
                    errors.append("/body/state_projection/result/publish_targets/verification/status")
                for key in ("target_id", "publish_capability", "delivery_adapter", "adapter_version"):
                    if item.get(key) is not None and not isinstance(item[key], str):
                        errors.append("/body/state_projection/result/publish_targets/" + key)
                for key in ("source_sha", "base_sha", "commit_sha"):
                    if proof.get(key) is not None and (not isinstance(proof[key], str)
                            or proof[key] and not re.fullmatch(r"[a-fA-F0-9]{40}(?:[a-fA-F0-9]{24})?", proof[key])):
                        errors.append("/body/state_projection/result/publish_targets/verification/" + key)
                if proof.get("preview_capable") is not None and type(proof["preview_capable"]) is not bool:
                    errors.append("/body/state_projection/result/publish_targets/verification/preview_capable")
    for item in (result, result.get("article_system_setup"), result.get("live_preview")):
        if not isinstance(item, dict):
            continue
        for key in ("build_verified", "browser_verified", "adopted", "adoption_kit_preview_verified",
                "verification_kit_required", "exactRender", "exact_render", "retryable", "requires_user_action"):
            if item.get(key) is not None and type(item[key]) is not bool:
                errors.append("/body/state_projection/result/" + key)
        for key in ("source_sha", "repo_head_sha", "head_sha", "commit_sha", "branch_commit_sha",
                "preview_commit_sha", "last_scanned_sha"):
            if item.get(key) not in (None, "") and (not isinstance(item[key], str)
                    or not re.fullmatch(r"[a-fA-F0-9]{40}(?:[a-fA-F0-9]{24})?", item[key])):
                errors.append("/body/state_projection/result/" + key)
    evidence = [result.get("directory_quality_verification"), result.get("live_preview")]
    if isinstance(targets, list):
        evidence.extend(item.get("verification") for item in targets if isinstance(item, dict))
    for proof in evidence:
        if not isinstance(proof, dict):
            continue
        if proof.get("preview_capable") is not None and type(proof["preview_capable"]) is not bool:
            errors.append("/body/state_projection/result/verification/preview_capable")
        for key in ("status", "task_id", "completed_at"):
            if proof.get(key) is not None and not isinstance(proof[key], str):
                errors.append("/body/state_projection/result/verification/" + key)
        completed = proof.get("completed_at")
        if isinstance(completed, str) and completed:
            from django.utils.dateparse import parse_datetime
            try:
                parsed = parse_datetime(completed)
            except (ValueError, OverflowError):
                parsed = None
            if parsed is None or parsed.tzinfo is None:
                errors.append("/body/state_projection/result/verification/completed_at")
        value = proof.get("resume_generation")
        if value is not None and (type(value) is not int or value < 0):
            errors.append("/body/state_projection/result/verification/resume_generation")
        for key in ("source_sha", "base_sha", "commit_sha", "commitSha"):
            if proof.get(key) is not None and (not isinstance(proof[key], str)
                    or proof[key] and not re.fullmatch(r"[a-fA-F0-9]{40}(?:[a-fA-F0-9]{24})?", proof[key])):
                errors.append("/body/state_projection/result/" + key)
    return errors[:12]


def validate_envelope(payload, *, expected_kind=None, path_run_id=None):
    """Validate wire types, original identity and both hashes before persistence."""
    if not isinstance(payload, dict):
        raise DeliveryContractError("delivery_schema_invalid", ["/"])
    try:
        encoded = canonical_bytes(payload)
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise DeliveryContractError("delivery_schema_invalid", ["/"]) from None
    if len(encoded) > MAX_BYTES:
        raise DeliveryContractError("delivery_payload_too_large", ["/"])
    schema = json.loads(SCHEMA_PATH.read_text())
    body = payload.get("body")
    projected = body.get("state_projection") if isinstance(body, dict) else None
    errors = _tree_errors(payload.get("scope"), "/scope") + _tree_errors(projected, "/body/state_projection")
    if errors:
        raise DeliveryContractError("delivery_schema_invalid", errors)
    errors = _schema_errors(payload, schema)
    if errors:
        raise DeliveryContractError("delivery_schema_invalid", errors)
    from .website_failure_redaction import redact_secret_text
    for key in ("event_id", "run_id"):
        if redact_secret_text(payload[key]) != payload[key]:
            raise DeliveryContractError("delivery_schema_invalid", ["/" + key])
    from django.utils.dateparse import parse_datetime
    from uuid import UUID
    try:
        emitted = parse_datetime(payload["emitted_at"])
    except (ValueError, OverflowError):
        emitted = None
    if emitted is None or emitted.tzinfo is None:
        raise DeliveryContractError("delivery_schema_invalid", ["/emitted_at"])
    for key in ("website_connection_id", "operation_id"):
        try:
            if str(UUID(payload["scope"][key])) != payload["scope"][key]:
                raise ValueError()
        except ValueError:
            raise DeliveryContractError("delivery_schema_invalid", ["/scope/" + key]) from None
    if expected_kind and payload["kind"] != expected_kind:
        raise DeliveryContractError("delivery_kind_mismatch", ["/kind"])
    if path_run_id is not None and payload["run_id"] != path_run_id:
        raise DeliveryContractError("delivery_identity_mismatch", ["/run_id"])
    projection = payload["body"]["state_projection"]
    if projection["status"] in {"failed", "blocked"} and not projection["failure"]:
        raise DeliveryContractError("delivery_failure_invalid", ["/body/state_projection/failure"])
    if len(projection["current_step"]) > 120:
        raise DeliveryContractError("delivery_schema_invalid", ["/body/state_projection/current_step"])
    errors = _result_errors(projection["result"])
    if errors:
        raise DeliveryContractError("delivery_schema_invalid", errors)
    if payload["kind"] == "setup_callback":
        if not isinstance(payload["event_type"], str):
            raise DeliveryContractError("delivery_schema_invalid", ["/event_type"])
    elif payload["event_type"] is not None:
        raise DeliveryContractError("delivery_schema_invalid", ["/event_type"])
    for name, step in projection["step_states"].items():
        if (not isinstance(name, str) or not 1 <= len(name) <= 120 or not isinstance(step, dict)
                or set(step) != {"status", "required", "attempts"} or type(step["required"]) is not bool
                or type(step["attempts"]) is not int or step["attempts"] < 0
                or not isinstance(step["status"], str) or step["status"] not in {
                    "pending", "running", "completed", "failed", "cancelled", "blocked", "skipped"}):
            raise DeliveryContractError("delivery_schema_invalid", ["/body/state_projection/step_states"])
    if ("://" in payload["scope"]["domain"] or "/" in payload["scope"]["domain"]
            or re.search(r"\s", payload["scope"]["domain"])
            or not re.fullmatch(r"[^/\s]+/[^/\s]+", payload["scope"]["github_repo"])):
        raise DeliveryContractError("delivery_schema_invalid", ["/scope"])
    for key in ("repair_diagnostics", "repair_status"):
        diagnostics = projection["result"].get(key)
        if isinstance(diagnostics, dict) and len(canonical_bytes(diagnostics)) > 65536:
            raise DeliveryContractError("delivery_schema_invalid", ["/body/state_projection/result/" + key])
    if any(projection[key] != payload["execution"][key] for key in ("generation", "state_version")):
        raise DeliveryContractError("delivery_identity_mismatch", ["/execution"])
    if projection["failure"]:
        from .website_failure_contract import FailureContractError, validate_failure
        try:
            validate_failure(projection["failure"])
        except FailureContractError:
            raise DeliveryContractError("delivery_failure_invalid", ["/body/state_projection/failure"]) from None
        if not projection["failure_occurrence_id"]:
            raise DeliveryContractError("delivery_failure_invalid", ["/body/state_projection/failure_occurrence_id"])
    for item in (projection["result"], projection["result"].get("article_system_setup"), projection["result"].get("live_preview")):
        if not isinstance(item, dict):
            continue
        if item.get("failure") and item["failure"] != projection["failure"]:
            raise DeliveryContractError("delivery_failure_invalid", ["/body/state_projection/result/failure"])
        if item.get("failure_occurrence_id") and item["failure_occurrence_id"] != projection["failure_occurrence_id"]:
            raise DeliveryContractError("delivery_failure_invalid", ["/body/state_projection/result/failure_occurrence_id"])
        failure = projection["failure"]
        if failure:
            for key, expected in (("error_code", failure["code"]), ("errorCode", failure["code"]), ("failure_class", failure["failure_class"]),
                    ("failure_fingerprint", failure["failure_fingerprint"]), ("retryable", failure["retryable"])):
                if key in item and (type(item[key]) is not type(expected) or item[key] != expected):
                    raise DeliveryContractError("delivery_failure_invalid", ["/body/state_projection/result/" + key])
    if state_digest(payload) != payload["state_sha256"]:
        raise DeliveryContractError("delivery_state_digest_mismatch", ["/state_sha256"])
    if payload_digest(payload) != payload["payload_sha256"]:
        raise DeliveryContractError("delivery_payload_digest_mismatch", ["/payload_sha256"])
    return deepcopy(payload)
