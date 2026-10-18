"""Portable original-publisher proposals and component receipts, never Ready.

Producer and receiver inject their existing closed native-v1 parser and schema
validator. The versioned bytes here are vendored exactly by the receiver.
"""
from copy import deepcopy
import hashlib
import json
from pathlib import PurePosixPath
import re
from uuid import UUID
from urllib.parse import urlsplit

MAX_BYTES = 98304
SHA = r"[a-f0-9]{40}(?:[a-f0-9]{24})?"
HASH = r"[a-f0-9]{64}"


class PublisherContractError(ValueError):
    """A fixed diagnostic without source, commands, input values or credentials."""


def canonical(value):
    """Use finite, uncoerced UTF-8 JSON for both candidate and receipt identity."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()


def digest(value):
    """Hash the exact normalized public object."""
    return hashlib.sha256(canonical(value)).hexdigest()


def obj(properties):
    return {"type": "object", "additionalProperties": False, "properties": properties, "required": list(properties)}


def text(pattern=None, *, maximum=500, minimum=1):
    return {"type": "string", "minLength": minimum, "maxLength": maximum, **({"pattern": pattern} if pattern else {})}


def array(items, minimum, maximum):
    return {"type": "array", "items": items, "minItems": minimum, "maxItems": maximum}


def integer(minimum=1, maximum=2**63-1):
    return {"type": "integer", "minimum": minimum, "maximum": maximum}


def candidate_schema(native_schema):
    """Wrap, rather than reinterpret, the closed native-v1 input seal."""
    native = deepcopy(native_schema)
    definitions = native.pop("$defs")
    definitions["NativeInputsV1"] = native
    command = array(text(), 2, 20)
    recipe = obj({"schema_version": {"const": 1}, "kind": {"const": "static_json_existing_publisher"},
        "source_sha": text(SHA), "source_inventory_digest": text(HASH), "store_path": text(),
        "listing_path": text(), "detail_path_pattern": text(), "publisher_argv": command,
        "checks": array(obj({"argv": command, "workflow_path": text(), "job": text(maximum=100),
            "step": integer(0, 999)}), 1, 10), "generated_paths": array(text(), 0, 20), "site_origin": text()})
    scope = obj({"website_connection_id": text(maximum=36), "connection_generation": integer(),
        "repository_id": integer(), "github_repo": text(r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}", maximum=201),
        "deletion_epoch": integer(0), "branch": text(maximum=200), "app_root": {"const": ""},
        "setup_operation_id": text(maximum=36), "setup_operation_attempt": integer(), "site_origin": text()})
    executor = obj({"profile": {"const": "pinned_existing_publisher_v1"}, "controller_sha256": text(HASH),
        "runtime_image": text("sha256:" + HASH), "python_version": text(r"\d+\.\d+\.\d+", maximum=20),
        "pyyaml_version": text(r"\d+\.\d+\.\d+", maximum=20)})
    payload = obj({"normalizer_version": {"const": 2}, "native_inputs_v1": {"$ref": "#/$defs/NativeInputsV1"},
        "original_scope": scope, "baseline": obj({"source_sha": text(SHA), "source_tree_sha": text(SHA),
            "source_inventory_digest": text(HASH)}), "recipe": recipe, "executor": executor,
        "probe_record_digests": array(text(HASH), 2, 2),
        "pipeline": obj({"profile": {"const": "original_python_static_marker_v1"},
            "wrapper_path": {"const": ".mlai/materialize-existing-articles.py"}, "wrapper_sha256": text(HASH),
            "marker_policy": obj({"kind": {"const": "artifact_digest"}, "artifact_digest": text(HASH),
                "meta_name": {"const": "mlai-artifact-digest"}, "listing_path": text()})})})
    result = obj({"schema_version": {"const": 2}, "canonicalization": {"const": "mlai_utf8_json_v1"},
        "digest": text(HASH), "payload": payload})
    result["$defs"] = definitions
    return result


def receipt_schema():
    """A passed recipe component explicitly cannot certify its unexecuted pipeline."""
    hashes = {"type": "object", "additionalProperties": text(HASH), "maxProperties": 22}
    publication = obj({"article_count": integer(1, 2002), "output_inventory_digest": text(HASH),
        "generated_sha256": hashes, "new_detail_sha256": {**hashes, "maxProperties": 2}})
    component = obj({"schema_version": {"const": 1}, "kind": {"const": "existing_publisher_execution_component"},
        "status": {"const": "passed"}, "recipe_digest": text(HASH), "source_sha_input": text(SHA),
        "source_inventory_digest": text(HASH), "record_digests": array(text(HASH), 2, 2),
        "original_article_count": integer(0, 2000), "regeneration_reproduced": {"const": True},
        "independent_publications": array(publication, 2, 2), "successive_publications": array(publication, 2, 2),
        "support_proven": {"const": False}, "live_goal_proof": {"const": False}, "browser_proven": {"const": False},
        "production_adoption": {"const": "unimplemented"}})
    execution = obj({"schema_version": {"const": 1}, "kind": {"const": "pinned_existing_publisher_component"},
        "source_sha": text(SHA), "source_tree_sha": text(SHA), "source_inventory_digest": text(HASH),
        "recipe_digest": text(HASH), "record_digests": array(text(HASH), 2, 2), "controller_sha256": text(HASH),
        "python_version": text(r"\d+\.\d+\.\d+", maximum=20), "pyyaml_version": text(r"\d+\.\d+\.\d+", maximum=20)})
    isolation = obj({"kind": {"const": "ephemeral_container"}, "source_sha": text(SHA), "source_tree_sha": text(SHA),
        "source_ref": text(), "runtime_image": text("sha256:" + HASH), "runtime_image_reference": text(minimum=0),
        "lockfile_digests": obj({}), "network": {"const": "none"}})
    return obj({"schema_version": {"const": 1}, "kind": {"const": "original_publisher_component"},
        "publishing_inputs_digest": text(HASH), "original_scope_digest": text(HASH),
        "component_result": obj({"publisher_component": component, "publisher_execution": execution, "isolation": isolation}),
        "pipeline_verified": {"const": False}, "publication_authority": {"const": False}, "receipt_id": text(HASH)})


def _path(value, *, slug=False):
    expanded = value.replace("{slug}", "example") if slug else value
    if (not expanded or expanded in {".", ".."} or expanded != str(PurePosixPath(expanded)) or expanded.startswith("/")
            or "\\" in expanded or ":" in expanded or "{" in expanded or "}" in expanded
            or any(part in {"..", ".git"} or part.startswith(".env") for part in expanded.split("/"))):
        raise PublisherContractError("publisher_contract_invalid")


def _command(argv):
    if argv[0] != "python3" or not argv[1].endswith(".py"):
        raise PublisherContractError("publisher_contract_invalid")
    _path(argv[1])
    if any(re.search(r"(?:^|[\s=-])(?:authorization|password|token|api[-_]key|secret|private[-_]key)(?:[=:]|\s|$)", arg, re.I)
            or re.search(r"(?:^|\s)[A-Z0-9_]*(?:TOKEN|PASSWORD|SECRET|API_KEY)\s*=", arg, re.I) for arg in argv):
        raise PublisherContractError("publisher_contract_invalid")


def _semantics(payload):
    native = payload["native_inputs_v1"]["payload"]
    scope, baseline, recipe, pipeline = [payload[key] for key in ("original_scope", "baseline", "recipe", "pipeline")]
    for key in ("website_connection_id", "setup_operation_id"):
        if str(UUID(scope[key])) != scope[key]:
            raise ValueError
    if len(baseline["source_sha"]) != len(baseline["source_tree_sha"]):
        raise ValueError
    origin = urlsplit(recipe["site_origin"])
    if (origin.scheme not in {"http", "https"} or not origin.hostname or origin.username is not None or origin.password is not None
            or origin.path not in {"", "/"} or origin.query or origin.fragment or recipe["site_origin"] != scope["site_origin"]):
        raise ValueError
    origin.port  # Reject an invalid port before any execution or receipt binding.
    if (scope["branch"].startswith(("-", "/")) or scope["branch"].endswith(("/", "."))
            or any(part.startswith(".") or part.endswith(".lock") for part in scope["branch"].split("/"))
            or any(token in scope["branch"] for token in ("..", "//", "@{"))
            or any(char.isspace() or char in "~^:?*[\\" for char in scope["branch"])):
        raise ValueError
    target, execution, oracle = native["publishing"]["target"], native["verification"]["execution"], native["verification"]["oracle"]
    materialization = execution["static_source_materialization"]
    if (target["kind"] != "stack_native_collection" or target["runtime_family"] != "static"
            or execution["runtime_family"] != "static" or execution["content_source_kind"] != "shared_collection"
            or payload["executor"]["python_version"].split(".")[:2] != execution["runtime_version"].split(".")[:2]
            or native["publishing"]["source_selection"] != {key: scope[key] for key in ("branch", "app_root")}
            or baseline["source_sha"] != native["verification"]["baseline_policy"]["source_sha"]
            or recipe["source_sha"] != baseline["source_sha"]
            or recipe["source_inventory_digest"] != baseline["source_inventory_digest"]
            or recipe["store_path"] != target["content_path_pattern"]
            or recipe["listing_path"] != materialization["listing_source_path"]
            or recipe["detail_path_pattern"] != materialization["detail_source_path_pattern"]):
        raise ValueError
    for path in (recipe["store_path"], recipe["listing_path"], pipeline["wrapper_path"]):
        _path(path)
    _path(recipe["detail_path_pattern"], slug=True)
    if (not recipe["store_path"].endswith(".json") or not recipe["listing_path"].endswith(".html")
            or recipe["detail_path_pattern"] != str(PurePosixPath(recipe["listing_path"]).parent / "{slug}" / "index.html")):
        raise ValueError
    wrapper = ["python3", pipeline["wrapper_path"]]
    if (materialization["materializer_command"] != wrapper or execution["materialize_command"] != wrapper
            or execution["build_command"] != wrapper or execution["materialize_cwd"] != "." or execution["build_cwd"] != "."
            or pipeline["marker_policy"]["listing_path"] != recipe["listing_path"]
            or pipeline["marker_policy"]["artifact_digest"] != oracle["artifact_digest"]
            or oracle["artifact_marker_routes"] != [execution["listing_route"]]
            or len(set(payload["probe_record_digests"])) != 2):
        raise ValueError
    _command(recipe["publisher_argv"])
    if recipe["publisher_argv"][1].startswith(".mlai/"):
        raise ValueError
    generated = recipe["generated_paths"]
    if generated != sorted(set(generated)):
        raise ValueError
    for path in generated:
        _path(path)
        if (path in {recipe["store_path"], recipe["listing_path"]} or path.startswith((".github/", ".mlai/", "scripts/"))
                or not path.endswith((".md", ".txt", ".xml", ".json"))):
            raise ValueError
    for check in recipe["checks"]:
        _command(check["argv"])
        _path(check["workflow_path"])
        if not check["workflow_path"].startswith(".github/workflows/") or not check["workflow_path"].endswith((".yml", ".yaml")):
            raise ValueError
    if ([{"command": step["command"], "cwd": step["cwd"]} for step in execution["verify_steps"]]
            != [{"command": check["argv"], "cwd": "."} for check in recipe["checks"]]):
        raise ValueError


def validate_candidate(value, *, native_validate, wire_validate, native_schema):
    """Validate a pending proposal; the checksum proves no execution or authority."""
    try:
        wire_validate(value, candidate_schema(native_schema))
        if len(canonical(value)) > MAX_BYTES or digest(value["payload"]) != value["digest"]:
            raise ValueError
        native_validate(value["payload"]["native_inputs_v1"])
        _semantics(value["payload"])
        return deepcopy(value)
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError, RecursionError):
        raise PublisherContractError("publisher_contract_invalid") from None


def bind_candidate(candidate, *, original_binding, trusted_profile):
    """Compare validated inputs to independently saved scope and supported code.

    Neither trusted argument may be populated from a proposed candidate/receipt.
    This comparison still grants no Ready or publication capability.
    """
    try:
        payload = candidate["payload"]
        expected = {**payload["original_scope"], **payload["baseline"], "publishing_inputs_digest": candidate["digest"]}
        if (type(original_binding) is not dict or set(original_binding) != set(expected)
                or canonical(expected) != canonical(original_binding)
                or type(trusted_profile) is not dict or set(trusted_profile) != {"executor", "wrapper_sha256", "native_verifier"}
                or canonical(trusted_profile["executor"]) != canonical(payload["executor"])
                or canonical(trusted_profile["native_verifier"]) != canonical(payload["native_inputs_v1"]["payload"]["verification"]["verifier"])
                or trusted_profile["wrapper_sha256"] != payload["pipeline"]["wrapper_sha256"]):
            raise ValueError
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError, RecursionError):
        raise PublisherContractError("publisher_contract_binding_mismatch") from None


def _component_matches(result, payload):
    component, execution, isolation = [result[key] for key in ("publisher_component", "publisher_execution", "isolation")]
    baseline, recipe, executor = [payload[key] for key in ("baseline", "recipe", "executor")]
    if (any(execution[key] != value for key, value in baseline.items())
            or any(isolation[key] != baseline[key] for key in ("source_sha", "source_tree_sha"))
            or any(execution[key] != executor[key] for key in ("controller_sha256", "python_version", "pyyaml_version"))
            or isolation["runtime_image"] != executor["runtime_image"]
            or isolation["source_ref"] not in {payload["original_scope"]["branch"], baseline["source_sha"]}
            or execution["recipe_digest"] != digest(recipe) or component["recipe_digest"] != digest(recipe)
            or execution["record_digests"] != payload["probe_record_digests"]
            or component["record_digests"] != payload["probe_record_digests"]
            or component["source_sha_input"] != baseline["source_sha"]
            or component["source_inventory_digest"] != baseline["source_inventory_digest"]):
        raise ValueError
    generated = {recipe["listing_path"], recipe["store_path"], *recipe["generated_paths"]}
    base = component["original_article_count"]
    for key, counts in (("independent_publications", [base + 1, base + 1]), ("successive_publications", [base + 1, base + 2])):
        for row, count in zip(component[key], counts):
            if row["article_count"] != count or set(row["generated_sha256"]) != generated:
                raise ValueError
            for path in row["new_detail_sha256"]:
                _path(path)
                prefix, suffix = recipe["detail_path_pattern"].split("{slug}")
                if not path.startswith(prefix) or not path.endswith(suffix) or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", path[len(prefix):-len(suffix)]):
                    raise ValueError
    first, second = component["successive_publications"]
    independent = component["independent_publications"]
    if (any(len(row["new_detail_sha256"]) != 1 for row in [*independent, first])
            or len(second["new_detail_sha256"]) != 2
            or set(independent[0]["new_detail_sha256"]) != set(first["new_detail_sha256"])
            or set(independent[0]["new_detail_sha256"]) & set(independent[1]["new_detail_sha256"])
            or set(second["new_detail_sha256"]) != set(independent[0]["new_detail_sha256"]) | set(independent[1]["new_detail_sha256"])
            or any(second["new_detail_sha256"].get(path) != checksum for path, checksum in first["new_detail_sha256"].items())):
        raise ValueError


def validate_receipt(value, *, candidate, origin, original_binding, trusted_profile,
                     native_validate, wire_validate, native_schema):
    """Validate authenticated component evidence, explicitly not pipeline/Ready proof."""
    try:
        if type(origin) is not str or origin != "authenticated_hosted":
            raise ValueError
        sealed = validate_candidate(candidate, native_validate=native_validate, wire_validate=wire_validate, native_schema=native_schema)
        bind_candidate(sealed, original_binding=original_binding, trusted_profile=trusted_profile)
        wire_validate(value, receipt_schema())
        if (len(canonical(value)) > MAX_BYTES or value["publishing_inputs_digest"] != sealed["digest"]
                or value["original_scope_digest"] != digest(sealed["payload"]["original_scope"])
                or value["receipt_id"] != digest({key: item for key, item in value.items() if key != "receipt_id"})):
            raise ValueError
        _component_matches(value["component_result"], sealed["payload"])
        return deepcopy(value)
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError, RecursionError):
        raise PublisherContractError("publisher_component_mismatch") from None


def create_receipt(result, *, candidate, original_binding, trusted_profile, native_validate, wire_validate, native_schema):
    """Project the actual trusted broker result; keep all qualification flags false."""
    try:
        if type(result) is not dict or result.get("success") is not True:
            raise ValueError
        value = {"schema_version": 1, "kind": "original_publisher_component", "publishing_inputs_digest": candidate["digest"],
            "original_scope_digest": digest(candidate["payload"]["original_scope"]),
            "component_result": {key: deepcopy(result[key]) for key in ("publisher_component", "publisher_execution", "isolation")},
            "pipeline_verified": False, "publication_authority": False}
        value["receipt_id"] = digest(value)
        return validate_receipt(value, candidate=candidate, origin="authenticated_hosted", original_binding=original_binding,
            trusted_profile=trusted_profile, native_validate=native_validate, wire_validate=wire_validate, native_schema=native_schema)
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError, RecursionError):
        raise PublisherContractError("publisher_component_mismatch") from None
