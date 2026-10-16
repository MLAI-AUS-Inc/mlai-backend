"""Native target metadata compatibility; descriptors never establish readiness."""
from copy import deepcopy
import hashlib
import json
import re

from .website_contract import WebsiteAuthorityError

_FIELDS = ("target_id", "kind", "delivery_adapter", "content_path_pattern", "route_template",
    "registration_strategy", "metadata_strategy")
_MARKER_FIELDS = ("kind", "value", "meta_name", "schema_version")
_DIGEST = re.compile(r"[a-f0-9]{64}")


def native_target_digest(target):
    """Match CF's canonical route/storage/adapter and four stable marker fields."""
    value = {key: target.get(key) for key in _FIELDS}
    marker = (target.get("contract") or {}).get("live_marker")
    value["live_marker"] = ({key: marker[key] for key in _MARKER_FIELDS if key in marker}
        if isinstance(marker, dict) else marker)
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def normalize_native_metadata(target):
    """Expose CF's nested native metadata where backend verifier consumers read it.

    Retain the complete descriptor and all verification state. Only matching,
    correctly shaped native identity data can be lifted; conflicting root and
    nested claims deny the update. Legacy root-only and custom targets retain
    their existing paths. Source, provider proof and readiness gates stay outside
    this data transformation.
    """
    if not isinstance(target, dict) or target.get("delivery_adapter") == "custom_contract_v1" or target.get("kind") == "custom_contract_v1":
        return deepcopy(target)
    nested = target.get("contract")
    if not isinstance(nested, dict) or "contract_digest" not in nested or "live_marker" not in nested:
        return deepcopy(target)

    def deny():
        raise WebsiteAuthorityError("ci_attestation_contract_mismatch", "Native target identity metadata is inconsistent.")

    try:
        digest, marker = nested["contract_digest"], nested["live_marker"]
        if (not isinstance(digest, str) or not _DIGEST.fullmatch(digest)
                or not isinstance(marker, dict) or marker.get("kind") != "artifact_digest"
                or not isinstance(marker.get("value"), str) or not _DIGEST.fullmatch(marker["value"])
                or "schema_version" in marker and (type(marker["schema_version"]) is not int or marker["schema_version"] != 1)
                or "meta_name" in marker and (not isinstance(marker["meta_name"], str)
                    or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.:-]{0,99}", marker["meta_name"]))
                or digest != native_target_digest(target)
                or target.get("contract_digest") is not None and target["contract_digest"] != digest):
            deny()
        canonical_marker = {key: marker[key] for key in _MARKER_FIELDS if key in marker}
        if target.get("live_marker") is not None:
            root = target["live_marker"]
            if not isinstance(root, dict) or any(key not in canonical_marker or type(value) is not type(canonical_marker[key])
                    or value != canonical_marker[key] for key, value in root.items() if key in _MARKER_FIELDS):
                deny()
        result = deepcopy(target)
        result.update(contract_digest=digest, live_marker=deepcopy(canonical_marker))
        return result
    except WebsiteAuthorityError:
        raise
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
        deny()
