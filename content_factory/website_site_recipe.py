"""Current service-owned verifier inputs, separate from repository recipe data."""
from copy import deepcopy
import json
from pathlib import PurePosixPath
import re

from django.conf import settings

from .website_contract import WebsiteAuthorityError
from .website_failure_redaction import redact_secret_text
from .website_target_contract import native_target_digest

_DIGEST = re.compile(r"[a-f0-9]{64}")
_SHA = re.compile(r"[a-f0-9]{40}(?:[a-f0-9]{24})?")
_TARGET_FIELDS = ("target_id", "kind", "delivery_adapter", "content_path_pattern", "route_template",
    "registration_strategy", "metadata_strategy")
_SCOPE_FIELDS = ("website_connection_id", "connection_generation", "repository_id", "github_repo", "branch", "app_root")


def _valid_probe(probe):
    def existing_source(value):
        if value is None:
            return True
        if not isinstance(value, dict) or set(value) != {"path", "sha256"}:
            return False
        path, digest = value["path"], value["sha256"]
        return (isinstance(path, str) and 0 < len(path) <= 500 and path != "." and path == str(PurePosixPath(path))
            and not path.startswith("/") and not any(c in path for c in "\\%:?#{}")
            and not any(ord(c) < 32 or ord(c) == 127 for c in path)
            and not any(part in {"..", ".git"} or part.startswith(".env") for part in path.split("/"))
            and isinstance(digest, str) and _DIGEST.fullmatch(digest) is not None)

    return (isinstance(probe, dict) and not set(probe) - {"slug", "title", "port", "is_setup_example", "marker_on_listing_only", "existing_seed_source"}
        and isinstance(probe.get("slug"), str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", probe["slug"])
        and isinstance(probe.get("title"), str) and 0 < len(probe["title"]) <= 256
        and not any(ord(c) < 32 for c in probe["title"])
        and type(probe.get("port", 3000)) is int and 1024 <= probe.get("port", 3000) <= 65535
        and all(key not in probe or type(probe[key]) is bool for key in ("is_setup_example", "marker_on_listing_only"))
        and existing_source(probe.get("existing_seed_source")))


def dispatch_selection(connection, target, source):
    """Choose v2 only from an already enrolled current target; never from caller data."""
    from .website_hosted_verification import enabled
    if not getattr(settings, "ARTICLES_SITE_CONTRACT_V2_ENABLED", False) or not enabled(connection):
        return None
    stored = target.contract if isinstance(target.contract, dict) else {}
    if "site_contract_version" not in stored:
        return None
    try:
        probe = stored.get("site_verification_probe")
        if (type(stored["site_contract_version"]) is not int or stored["site_contract_version"] != 2
                or str(target.connection_id) != str(connection.pk) or type(target.generation) is not int
                or target.generation != connection.generation
                or not isinstance(source, str) or not _SHA.fullmatch(source)
                or stored.get("target_id") != target.target_key
                or not isinstance(stored.get("site_contract_digest"), str) or not _DIGEST.fullmatch(stored["site_contract_digest"])
                or stored.get("contract_digest") != native_target_digest(stored) or not _valid_probe(probe)):
            raise ValueError("A current enrolled site recipe is required")
        raw = json.dumps(probe, sort_keys=True, separators=(",", ":"), allow_nan=False)
        if redact_secret_text(raw) != raw:
            raise ValueError("Secret-free verification probe required")
        return {"site_recipe_version": 2, "site_contract_digest": stored["site_contract_digest"], "probe": deepcopy(probe)}
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
        raise WebsiteAuthorityError("verified_target_required", "The current enrolled site recipe is unavailable.") from None


def authorization_inputs(connection, query):
    """Project only a saved current reservation while the caller holds authority_guard.

    A digest supplied by a worker never enrolls a target. This function performs
    scoped database reads only; it grants no publishing capability or ready state.
    """
    from . import website_hosted_verification as hosted
    from .website_operations import validate_operation, deletion_epoch
    from .website_connections import contract_for

    def deny():
        raise WebsiteAuthorityError("verified_target_required", "The current site recipe verification reservation is unavailable.")

    if not getattr(settings, "ARTICLES_SITE_CONTRACT_V2_ENABLED", False) or not hosted.enabled(connection):
        deny()
    operation = validate_operation(connection, query)
    saved = operation.payload if operation and isinstance(operation.payload, dict) else {}
    original = saved.get("binding")
    if (not operation or operation.action != "workflow" or operation.generation != connection.generation
            or operation.state not in {"pending", "running", "completed"}
            or saved.get("workflow") != "hosted_native_verification" or not isinstance(original, dict)
            or type(original.get("site_recipe_version")) is not int or original["site_recipe_version"] != 2):
        deny()
    scope = {key: value for key, value in contract_for(connection).items() if key in _SCOPE_FIELDS}
    current = {**scope, "operation_id": str(operation.pk), "operation_attempt": saved.get("attempt", 1),
        "deletion_epoch": deletion_epoch(connection)}
    if (set(scope) != set(_SCOPE_FIELDS)
            or any(type(current[key]) is not int or current[key] < minimum for key, minimum in (
                ("connection_generation", 1), ("repository_id", 1), ("operation_attempt", 1), ("deletion_epoch", 0)))
            or any(type(original.get(key)) is not type(value) or original[key] != value for key, value in current.items())
            or any(str(query.get(key)) != str(original.get(key)) for key in (
                *_SCOPE_FIELDS, "run_id", "operation_id", "operation_attempt", "deletion_epoch", "target_id", "site_contract_digest", "site_recipe_version"))
            or not isinstance(original.get("run_id"), str) or not original["run_id"]
            or original.get("run_id") != saved.get("run_id")
            or query.get("expected_source_sha") != original.get("expected_source_sha")
            or not isinstance(original.get("expected_source_sha"), str) or not _SHA.fullmatch(original["expected_source_sha"])
            or not isinstance(original.get("site_contract_digest"), str) or not _DIGEST.fullmatch(original["site_contract_digest"])
            or not isinstance(original.get("contract_digest"), str) or not _DIGEST.fullmatch(original["contract_digest"])):
        deny()
    # This is an accepted recipe definition, not a build fact about the new
    # commit. CF must read the matching recipe at the reserved source and emit
    # fresh evidence; the existing promotion/source-review gates still apply.
    target = connection.targets.filter(generation=connection.generation, target_key=original.get("target_id")).first()
    if target is None or not isinstance(target.contract, dict):
        deny()
    stored = target.contract
    probe = saved.get("site_recipe_probe")
    if (stored.get("target_id") != original.get("target_id")
            or stored.get("site_contract_digest") != original["site_contract_digest"]
            or stored.get("contract_digest") != original["contract_digest"]
            or type(stored.get("adapter_version", 1)) is not int or stored.get("adapter_version", 1) != 1
            or not _valid_probe(probe)):
        deny()
    try:
        selected = {key: stored.get(key) for key in _TARGET_FIELDS}
        raw_marker = (stored.get("contract") or {}).get("live_marker")
        marker = {key: raw_marker[key] for key in ("kind", "value", "meta_name", "schema_version") if key in raw_marker} if isinstance(raw_marker, dict) else None
        if (not isinstance(marker, dict)
                or marker.get("kind") != "artifact_digest" or not isinstance(marker.get("value"), str)
                or not _DIGEST.fullmatch(marker["value"])
                or "schema_version" in marker and (type(marker["schema_version"]) is not int or marker["schema_version"] != 1)
                or "meta_name" in marker and (not isinstance(marker["meta_name"], str)
                    or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.:-]{0,99}", marker["meta_name"]))):
            deny()
        selected.update(contract={"live_marker": marker}, site_contract_digest=original["site_contract_digest"])
        if native_target_digest(selected) != original["contract_digest"]:
            deny()
        binding = {**current, **{key: original[key] for key in ("expected_source_sha", "target_id", "contract_digest", "site_contract_digest")},
            "adapter_id": stored.get("delivery_adapter") or stored.get("kind"), "adapter_version": 1}
        result = {"version": 1, "scope": scope, "binding": binding, "target": selected, "probe": probe}
        raw = json.dumps(result, sort_keys=True, separators=(",", ":"), allow_nan=False)
        if len(raw.encode()) > 65536 or redact_secret_text(raw) != raw:
            deny()
        return deepcopy(result)
    except WebsiteAuthorityError:
        raise
    except (TypeError, ValueError, KeyError, AttributeError, RecursionError):
        deny()
