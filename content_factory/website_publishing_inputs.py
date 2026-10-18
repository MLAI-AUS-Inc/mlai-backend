"""Bounded publishing-input candidates; a checksum never grants verification.

The closed schema is vendored from an immutable CF producer. Legacy CI cannot
certify these additional inputs until the paired execution/proof upgrade ships.
"""
from copy import deepcopy
from functools import lru_cache
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
from urllib.parse import urlsplit

from .website_failure_contract import canonical_bytes

MAX_BYTES = 65536
TARGET_OBSERVATIONS = frozenset({"confidence", "verification", "evidence", "source", "unsupported_reason",
    "contract", "contract_digest", "seed_detail_proof", "provisional"})
SCHEMA_PATH = Path(__file__).resolve().parents[1] / "contracts/publishing-inputs-v1.schema.json"
SCHEMA_KEYS = frozenset({"$defs", "$ref", "title", "description", "default", "type", "const", "enum",
    "anyOf", "properties", "required", "additionalProperties", "items", "minItems", "maxItems",
    "minLength", "maxLength", "pattern", "minimum", "maximum", "maxProperties"})


class PublishingInputsError(ValueError):
    """A static diagnosis that never includes configuration or provider text."""


@lru_cache(maxsize=1)
def _schema():
    value = json.loads(SCHEMA_PATH.read_text())
    def check(node):
        if set(node) - SCHEMA_KEYS:
            raise PublishingInputsError("publishing_inputs_schema_unsupported")
        for key in ("$defs", "properties"):
            for child in node.get(key, {}).values():
                check(child)
        for child in node.get("anyOf", []):
            check(child)
        for key in ("items", "additionalProperties"):
            if isinstance(node.get(key), dict):
                check(node[key])
    check(value)
    return value


def _shape(value, schema, document):
    """Validate the finite schema vocabulary without coercion or defaults."""
    if "$ref" in schema:
        ref = schema["$ref"]
        if not ref.startswith("#/$defs/") or ref.count("/") != 2:
            return False
        return _shape(value, document["$defs"][ref.split("/")[-1]], document)
    if "anyOf" in schema and not any(_shape(value, child, document) for child in schema["anyOf"]):
        return False
    kinds = {"object": dict, "array": list, "string": str, "integer": int, "boolean": bool, "null": type(None)}
    if "type" in schema and type(value) is not kinds[schema["type"]]:
        return False
    if "const" in schema and (type(value) is not type(schema["const"]) or value != schema["const"]):
        return False
    if "enum" in schema and not any(type(value) is type(item) and value == item for item in schema["enum"]):
        return False
    if isinstance(value, dict):
        if set(schema.get("required", [])) - set(value) or len(value) > schema.get("maxProperties", 100):
            return False
        for key, item in value.items():
            child = schema.get("properties", {}).get(key, schema.get("additionalProperties", True))
            if child is False or isinstance(child, dict) and not _shape(item, child, document):
                return False
    elif isinstance(value, list):
        if not schema.get("minItems", 0) <= len(value) <= schema.get("maxItems", 100):
            return False
        if any(not _shape(item, schema.get("items", {}), document) for item in value):
            return False
    elif isinstance(value, str):
        if not schema.get("minLength", 0) <= len(value) <= schema.get("maxLength", 4096):
            return False
        if "pattern" in schema and not re.fullmatch(schema["pattern"], value):
            return False
    elif type(value) is int:
        if not schema.get("minimum", -(2**63)) <= value <= schema.get("maximum", 2**63 - 1):
            return False
    return True


def _public_tree(value, *, depth=0, budget=None, argv=False):
    budget = [4000] if budget is None else budget
    budget[0] -= 1
    if depth > 16 or budget[0] < 0:
        raise PublishingInputsError("publishing_inputs_bounds")
    if isinstance(value, dict):
        if len(value) > 100:
            raise PublishingInputsError("publishing_inputs_bounds")
        for key, child in value.items():
            if (not isinstance(key, str) or len(key) > 100
                    or re.search(r"(?:^|_)(?:token|secret|password|credential|authorization|private_key|api_key)(?:$|_)", key, re.I)):
                raise PublishingInputsError("publishing_inputs_private_value")
            _public_tree(child, depth=depth + 1, budget=budget, argv=key == "command" or key.endswith("_command"))
    elif isinstance(value, list):
        if len(value) > 100:
            raise PublishingInputsError("publishing_inputs_bounds")
        for child in value:
            _public_tree(child, depth=depth + 1, budget=budget, argv=argv)
    elif isinstance(value, str):
        if len(value) > 4096 or any(ord(char) < 32 for char in value):
            raise PublishingInputsError("publishing_inputs_string")
        if re.search(r"https?://[^\s/@]+:[^\s/@]+@|github_pat_|gh[opsu]_[A-Za-z0-9]{16,}|sk-[A-Za-z0-9]{16,}|[?&](?:token|secret|api_key)=", value):
            raise PublishingInputsError("publishing_inputs_private_value")
        if argv and (re.search(r"(?:^|[\s=-])(?:authorization|password|token|api[-_]key|secret|private[-_]key)(?:[=:]|\s|$)", value, re.I)
                or re.search(r"(?:^|\s)[A-Z0-9_]*(?:TOKEN|PASSWORD|SECRET|API_KEY)\s*=", value, re.I)):
            raise PublishingInputsError("publishing_inputs_private_value")
    elif value is not None and type(value) not in {bool, int}:
        raise PublishingInputsError("publishing_inputs_scalar")


def _path(value, *, empty=False, slug=False, cwd=False):
    if empty and value == "":
        return
    expanded = value.replace("{slug}", "example") if slug else value
    if (not expanded or expanded == "." and not cwd or expanded != str(PurePosixPath(expanded)) or expanded.startswith("/")
            or any(part in {"..", ".git"} for part in expanded.split("/"))
            or "\\" in expanded or ":" in expanded or "{" in expanded or "}" in expanded):
        raise PublishingInputsError("publishing_inputs_path")


def _route(value, *, empty=False):
    if empty and value == "":
        return
    if (not value.startswith("/") or "//" in value or ".." in value.split("/") or "\\" in value
            or urlsplit(value).query or urlsplit(value).fragment):
        raise PublishingInputsError("publishing_inputs_route")


def _semantics(payload):
    target = payload["publishing"]["target"]
    verification = payload["verification"]
    execution, oracle = verification["execution"], verification["oracle"]
    react = target["kind"] == "react_article_system"
    if (target["delivery_adapter"] != ("react_component" if react else "stack_native")
            or target["registration_strategy"]["type"] != ("registry_seo_patch" if react else "stack_native_collection")
            or target["metadata_strategy"]["type"] != ("react_article_system" if react else "stack_native_collection")):
        raise PublishingInputsError("publishing_inputs_adapter")
    source = payload["publishing"]["source_selection"]
    if set(source) - {"branch", "app_root"} or "branch" in source and not source["branch"]:
        raise PublishingInputsError("publishing_inputs_source")
    if "app_root" in source:
        _path(source["app_root"], empty=True, cwd=True)
    if (target["runtime_family"] != execution["runtime_family"]
            or "app_root" in source and (source["app_root"] or ".") != execution["app_root"]
            or target["metadata_strategy"]["seo_path_template"] != target["route_template"]):
        raise PublishingInputsError("publishing_inputs_execution_mismatch")
    if target["kind"] == "stack_native_collection" and (
            canonical_bytes(target["metadata_strategy"]["static_source_materialization"]) != canonical_bytes(execution["static_source_materialization"])
            or target["registration_strategy"]["path"] != target["content_path_pattern"]):
        raise PublishingInputsError("publishing_inputs_materializer_mismatch")
    _path(target["content_path_pattern"], slug=True)
    _path(target["example_path"], empty=True)
    for key, value in target["registration_strategy"].items():
        if key.endswith("_path") or key == "path":
            _path(value, empty=key in {"author_registry_path", "author_alias_path"})
    for value in (target["route_template"], target["route_path"], target["metadata_strategy"]["seo_path_template"],
                  execution["route_template"], execution["listing_route"]):
        _route(value)
    for key in ("app_root", "install_cwd", "materialize_cwd", "build_cwd", "preview_cwd"):
        _path(execution[key], cwd=True)
    for path in execution["lockfiles"]:
        _path(path)
    if oracle["existing_seed_source"]:
        _path(oracle["existing_seed_source"]["path"])
    if "{slug}" not in execution["route_template"] or (execution["content_source_kind"] == "document_per_file" and "{slug}" not in execution["content_path_pattern"]):
        raise PublishingInputsError("publishing_inputs_slug")
    if (target["route_path"] != execution["listing_route"] or target["route_template"] != execution["route_template"]
            or target["content_path_pattern"] != execution["content_path_pattern"] or target["live_marker"]["value"] != oracle["artifact_digest"]):
        raise PublishingInputsError("publishing_inputs_mapping")
    routes = {execution["listing_route"], execution["route_template"].replace("{slug}", oracle["seed_slug"])}
    if set(oracle["artifact_marker_routes"]) - routes:
        raise PublishingInputsError("publishing_inputs_marker")
    commands = [execution[key] for key in ("install_command", "materialize_command", "build_command", "preview_command")]
    if not execution["build_command"] and not execution["verify_steps"]:
        raise PublishingInputsError("publishing_inputs_build")
    steps = list(execution["verify_steps"])
    baseline = execution["baseline"]
    if baseline["kind"] == "commands":
        commands.append(baseline["install_command"])
        _path(baseline["install_cwd"], cwd=True)
        steps.extend(baseline["verify_steps"])
    else:
        if execution["runtime_family"] != "static":
            raise PublishingInputsError("publishing_inputs_baseline")
        for path in baseline["paths"]:
            _path(path)
    for step in steps:
        commands.append(step["command"])
        _path(step["cwd"], cwd=True)
    static = execution["static_source_materialization"]
    if static:
        if (execution["runtime_family"] != "static" or static["materializer_command"] not in (execution["build_command"], execution["materialize_command"])
                or static["listing_source_path"].startswith(static["preview_output_dir"] + "/")
                or execution["content_path_pattern"].startswith(static["preview_output_dir"] + "/")
                or static["detail_source_path_pattern"] != str(PurePosixPath(static["listing_source_path"]).parent / "{slug}" / "index.html")):
            raise PublishingInputsError("publishing_inputs_materializer")
        for key in ("listing_source_path", "detail_source_path_pattern", "preview_output_dir"):
            _path(static[key], slug=key == "detail_source_path_pattern")
    if any(any(not arg for arg in command) for command in commands):
        raise PublishingInputsError("publishing_inputs_command")


def validate_candidate(value):
    """Return an independent exact-shape candidate, without promoting any proof."""
    try:
        _public_tree(value)
        document = _schema()
        if len(canonical_bytes(value)) > MAX_BYTES or not _shape(value, document, document):
            raise ValueError("invalid_shape")
        _semantics(value["payload"])
        if hashlib.sha256(canonical_bytes(value["payload"])).hexdigest() != value["digest"]:
            raise ValueError("invalid_digest")
        return deepcopy(value)
    except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
        raise PublishingInputsError("publishing_inputs_candidate_invalid") from None


def candidate_for_target(target):
    """Read the single nested candidate location; absent is explicitly legacy."""
    contract = target.get("contract") if isinstance(target, dict) else None
    if isinstance(contract, dict) and "publishing_inputs_v1" in contract:
        candidate = validate_candidate(contract["publishing_inputs_v1"])
        frozen = candidate["payload"]["publishing"]["target"]
        if set(target) - set(frozen) - TARGET_OBSERVATIONS or set(contract) - {"contract_digest", "live_marker", "publishing_inputs_v1"}:
            raise PublishingInputsError("publishing_inputs_target_mismatch")
        actual = {key: target.get(key) for key in frozen}
        marker = actual.get("live_marker")
        if isinstance(marker, dict):
            actual["live_marker"] = {"meta_name": "mlai-artifact-digest", "schema_version": 1, **marker}
        if "live_marker" in contract:
            nested = contract["live_marker"]
            if not isinstance(nested, dict) or canonical_bytes({"meta_name": "mlai-artifact-digest", "schema_version": 1, **nested}) != canonical_bytes(actual["live_marker"]):
                raise PublishingInputsError("publishing_inputs_target_mismatch")
        if canonical_bytes(actual) != canonical_bytes(frozen):
            raise PublishingInputsError("publishing_inputs_target_mismatch")
        return candidate
    return None


def has_candidate(target):
    """Detect presence even when malformed, so old proofs cannot bypass the fence."""
    contract = target.get("contract") if isinstance(target, dict) else None
    return isinstance(contract, dict) and "publishing_inputs_v1" in contract
