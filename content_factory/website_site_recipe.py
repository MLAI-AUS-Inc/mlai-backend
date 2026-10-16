"""Current service-owned verifier inputs, separate from repository recipe data."""
from copy import deepcopy
import hashlib
import json
import re

from django.conf import settings

from .website_contract import WebsiteAuthorityError
from .website_failure_redaction import redact_secret_text

_DIGEST = re.compile(r"[a-f0-9]{64}")
_SHA = re.compile(r"[a-f0-9]{40}(?:[a-f0-9]{24})?")
_TARGET_FIELDS = ("target_id", "kind", "delivery_adapter", "content_path_pattern", "route_template",
    "registration_strategy", "metadata_strategy")
_SCOPE_FIELDS = ("website_connection_id", "connection_generation", "repository_id", "github_repo", "branch", "app_root")


def native_target_digest(target):
    """Use CF's existing route/storage/adapter/marker canonical target identity."""
    value = {key: target.get(key) for key in _TARGET_FIELDS}
    value["live_marker"] = (target.get("contract") or {}).get("live_marker")
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


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
    target = connection.targets.filter(generation=connection.generation, target_key=original.get("target_id"),
        source_sha=original["expected_source_sha"]).first()
    if target is None or not isinstance(target.contract, dict):
        deny()
    stored = target.contract
    probe = saved.get("site_recipe_probe")
    if (stored.get("target_id") != original.get("target_id")
            or stored.get("site_contract_digest") != original["site_contract_digest"]
            or stored.get("contract_digest") != original["contract_digest"]
            or type(stored.get("adapter_version", 1)) is not int or stored.get("adapter_version", 1) != 1
            or not isinstance(probe, dict) or set(probe) - {"slug", "title", "port", "is_setup_example", "marker_on_listing_only"}
            or not isinstance(probe.get("slug"), str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", probe["slug"])
            or not isinstance(probe.get("title"), str) or not 0 < len(probe["title"]) <= 256
            or any(ord(c) < 32 for c in probe["title"])
            or type(probe.get("port", 3000)) is not int or not 1024 <= probe.get("port", 3000) <= 65535
            or any(key in probe and type(probe[key]) is not bool for key in ("is_setup_example", "marker_on_listing_only"))):
        deny()
    try:
        selected = {key: stored.get(key) for key in _TARGET_FIELDS}
        marker = (stored.get("contract") or {}).get("live_marker")
        if (not isinstance(marker, dict) or set(marker) - {"kind", "value", "meta_name", "schema_version"}
                or marker.get("kind") != "artifact_digest" or not isinstance(marker.get("value"), str)
                or not _DIGEST.fullmatch(marker["value"])):
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
