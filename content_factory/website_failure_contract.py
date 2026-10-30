"""Pure, versioned Articles causal failures; delivery policy belongs to B5.

All public projection is opt-in. Reading a historical receipt never schedules work.
The registry is the authority for class/retryability, not exception text or HTTP alone.
"""
from __future__ import annotations

from copy import deepcopy
from functools import lru_cache, wraps
import hashlib
import json
import logging
from pathlib import Path, PurePosixPath
import re
from typing import Any
from urllib.parse import urlsplit
import uuid

from .website_failure_redaction import redact_secret_text as redact_secrets

SOURCE_CONTRACT_ROOT = Path(__file__).resolve().parents[1] / "contracts"
PACKAGED_CONTRACT_ROOT = Path(__file__).resolve().parents[1] / "contracts"
CONTRACT_ROOT = SOURCE_CONTRACT_ROOT if (SOURCE_CONTRACT_ROOT / "failure-codes.json").is_file() else PACKAGED_CONTRACT_ROOT
CLASSES = ("transient", "waiting", "repairable", "strategy", "needs_user", "unsupported")
MAX_FINDINGS = 64
IDENTIFIER_FIELDS = {"code", "slot", "adapter", "framework", "provider_operation", "route_role", "constraint"}
PATH_FIELDS = {"path", "repo_path", "import", "route_path"}
NUMBER_FIELDS = {"http_status", "observed_count", "required_count", "minimum", "maximum"}
NON_SEMANTIC_FIELDS = {"message", "summary", "description", "claim", "severity", "suggestion", "repair_instructions",
                       "source", "headers", "body", "stack_trace", "timestamp", "checked_at", "run_id", "task_id",
                       "event_id", "operation_id", "operation_attempt", "retry_epoch", "strategy_rung", "commit_sha",
                       "preview_url", "line", "column", "details", "evidence", "retryable", "failure_class"}
FAILURE_FIELDS = {"schema_version", "registry_version", "registry_sha256", "fingerprint_version", "code",
                  "failure_class", "failure_fingerprint", "stage", "retryable", "user_message", "action", "evidence_refs"}
PROJECTION_FIELDS = ("failure", "failure_class", "failure_fingerprint", "failure_occurrence_id", "error_code", "retryable")
_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
_ASSET_HASH = re.compile(r"(?<=[.-])[0-9a-f]{8,64}(?=\.[A-Za-z0-9]+$)")
_LABEL = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.:-]{0,255}")
_CODE = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,127}")
logger = logging.getLogger(__name__)


class FailureContractError(ValueError):
    """A bounded contract diagnosis, never a raw provider message."""


def canonical_bytes(value: Any) -> bytes:
    """Deterministic JSON for fingerprints, independent of wire formatting."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


@lru_cache(maxsize=1)
def load_registry() -> dict:
    """Load and validate immutable canonical files without network or settings."""
    raw = (CONTRACT_ROOT / "failure-codes.json").read_bytes()
    registry = json.loads(raw)
    if (any(type(registry.get(key)) is not int or registry[key] != 1 for key in ("schema_version", "registry_version", "fingerprint_version")) or
            set(registry.get("classes", [])) != set(CLASSES)):
        raise FailureContractError("invalid_registry_version")
    codes = {}
    for entry in registry["codes"]:
        code = entry["code"]
        if (code in codes or entry["failure_class"] not in CLASSES or entry["retry_policy"] not in registry["policies"] or
                registry["policies"][entry["retry_policy"]]["class"] != entry["failure_class"] or
                entry["legacy"]["failure_class"] not in CLASSES or type(entry["legacy"]["retryable"]) is not bool):
            raise FailureContractError("invalid_registry_entry")
        codes[code] = entry
    for entry in registry["codes"]:
        for alias in entry.get("aliases", []):
            if alias in codes:
                raise FailureContractError("ambiguous_registry_alias")
            codes[alias] = entry
    registry["by_code"] = codes
    registry["sha256"] = hashlib.sha256(raw).hexdigest()
    return registry


from .website_failure_scope import taxonomy_enabled


def _identifier(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > 256 or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise FailureContractError("invalid_fingerprint_identifier")
    clean = redact_secrets(value)
    if clean != value or "[REDACTED]" in clean or "?" in clean or "#" in clean:
        raise FailureContractError("secret_or_url_in_fingerprint")
    return _UUID.sub("<execution>", clean)


def normalize_findings(findings: Any) -> list[dict]:
    """Allowlist semantic evidence before redaction/hash; free text is never hashed."""
    if findings is None:
        return []
    if not isinstance(findings, (list, tuple)) or len(findings) > MAX_FINDINGS:
        raise FailureContractError("fingerprint_findings_overflow")
    normalized = []
    finding_codes = set(load_registry().get("finding_codes", [])) | set(load_registry()["by_code"])
    for finding in findings:
        if not isinstance(finding, dict):
            raise FailureContractError("invalid_fingerprint_finding")
        projected = {}
        for key, value in finding.items():
            if key in IDENTIFIER_FIELDS:
                if value in (None, ""):
                    continue
                if not isinstance(value, str) or not _LABEL.fullmatch(value):
                    raise FailureContractError("invalid_semantic_label")
                value = _identifier(value)
                if key == "code" and value not in finding_codes:
                    raise FailureContractError("unregistered_finding_code")
                projected[key] = value
            elif key in PATH_FIELDS:
                if value in (None, ""):
                    continue
                value = _identifier(value)
                # A preview origin is volatile; only its safe path is semantic.
                if "://" in value:
                    parsed = urlsplit(value)
                    if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.scheme not in {"https", "http"}:
                        raise FailureContractError("unsafe_fingerprint_url")
                    value = parsed.path
                if "\\" in value or ".." in PurePosixPath(value).parts or (value.startswith("/") and key != "route_path"):
                    raise FailureContractError("unsafe_fingerprint_path")
                projected[key] = _ASSET_HASH.sub("<asset>", value)
            elif key in NUMBER_FIELDS:
                if type(value) is not int or not 0 <= value <= 1_000_000 or (key == "http_status" and not 100 <= value <= 599):
                    raise FailureContractError("invalid_fingerprint_quantity")
                projected[key] = value
            elif key not in NON_SEMANTIC_FIELDS:
                raise FailureContractError("unknown_fingerprint_field")
        if projected:
            normalized.append(projected)
    return [json.loads(raw) for raw in sorted({canonical_bytes(row) for row in normalized})]


def gate_findings(findings: Any) -> list[dict]:
    """Audited adapter from internal gate reports to typed semantic evidence.

    Fields outside this finite map remain diagnostics; model prose/source never
    reaches fingerprint material. The strict normalizer enforces types and caps.
    """
    if not isinstance(findings, (list, tuple)):
        return [] if findings is None else [{"invalid_finding_shape": True}]
    aliases = {"file_path": "path", "import_path": "import", "required_slot": "slot", "routePath": "route_path",
               "mismatch_id": "constraint", "missing_import": "import"}
    rows = []
    for finding in findings:
        if not isinstance(finding, dict):
            rows.append({"invalid_finding_shape": True}); continue
        projected = {aliases.get(key, key): value for key, value in finding.items()
                     if key in IDENTIFIER_FIELDS | PATH_FIELDS | NUMBER_FIELDS or key in aliases}
        slots = finding.get("missing_slots")
        if isinstance(slots, list):
            rows.extend({**projected, "slot": slot} for slot in slots)
        elif projected:
            rows.append(projected)
    return rows


def _safe_code(code: Any) -> bool:
    """A causal code is a bounded identifier with nothing secret-looking in it."""
    return isinstance(code, str) and bool(_CODE.fullmatch(code)) and redact_secrets(code) == code


def _action(entry: dict, action: Any, failure_class: str | None = None) -> dict | None:
    """The registered owner action; an envelope class outside this registry names its own."""
    if (failure_class or entry["failure_class"]) != "needs_user":
        return None
    action = action if isinstance(action, dict) else {}
    expected = entry.get("action_id") if entry["failure_class"] == "needs_user" else None
    action_id = action.get("id") or expected
    if not isinstance(action_id, str) or (expected and action_id != expected) or action_id not in load_registry()["actions"]:
        raise FailureContractError("invalid_failure_action")
    url = action.get("url") or action.get("href")
    if url:
        if not isinstance(url, str) or len(url) > 2048 or redact_secrets(url) != url:
            raise FailureContractError("invalid_failure_action_url")
        parsed = urlsplit(url)
        if (parsed.username or parsed.password or parsed.fragment or parsed.query or
                (parsed.scheme and (parsed.scheme != "https" or parsed.hostname not in {"github.com", "app.mlai.au", "mlai.au"})) or
                (not parsed.scheme and (not url.startswith("/") or url.startswith("//")))):
            raise FailureContractError("invalid_failure_action_url")
    return {"id": action_id, "label": load_registry()["actions"][action_id], "url": url or None}


def classify_failure(code: str, stage: str, findings: Any = None, action: Any = None, evidence_refs: Any = None) -> dict:
    """Classify a registered causal code; invalid input stops as an operator contract bug."""
    registry = load_registry()
    entry = registry["by_code"].get(code) if isinstance(code, str) else None
    if entry is None:
        entry = registry["by_code"]["UNREGISTERED_FAILURE_CODE"]
        findings = []  # Never hash/log an unknown original string.
        action = None
    try:
        if not isinstance(stage, str) or not _LABEL.fullmatch(stage):
            raise FailureContractError("invalid_failure_stage")
        safe_stage = _identifier(stage)
        semantic = normalize_findings(findings)
        safe_action = _action(entry, action)
        refs = []
        for reference in evidence_refs or []:
            reference = _identifier(reference)
            if reference.startswith("/") or "://" in reference or ".." in PurePosixPath(reference).parts:
                raise FailureContractError("unsafe_evidence_reference")
            refs.append(reference)
        if len(refs) > MAX_FINDINGS:
            raise FailureContractError("evidence_reference_overflow")
    except FailureContractError:
        entry = registry["by_code"]["FAILURE_EVIDENCE_INVALID"]
        safe_stage, semantic, safe_action, refs = "failure_contract", [], None, []
    material = {"fingerprint_version": 1, "code": entry["code"], "stage": safe_stage, "findings": semantic}
    return {"schema_version": 1, "registry_version": registry["registry_version"], "registry_sha256": registry["sha256"],
            "fingerprint_version": 1, "code": entry["code"], "failure_class": entry["failure_class"],
            "failure_fingerprint": "sha256:" + hashlib.sha256(canonical_bytes(material)).hexdigest(),
            "stage": safe_stage, "retryable": entry["failure_class"] == "transient", "user_message": entry["user_message"],
            "action": safe_action, "evidence_refs": sorted(set(refs))}


def _same_action(expected: dict | None, actual: Any) -> bool:
    """Actions agree by id and url; the label is display copy and only has to be safe."""
    if expected is None or actual is None:
        return expected is actual
    if not isinstance(actual, dict) or set(actual) != {"id", "label", "url"}:
        return False
    label = actual["label"]
    return (actual["id"] == expected["id"] and actual["url"] == expected["url"] and
            isinstance(label, str) and 0 < len(label) <= 256 and redact_secrets(label) == label)


def _registered(failure: dict) -> dict | None:
    """The receiver's entry when it agrees with the envelope's code and class."""
    entry = load_registry()["by_code"].get(failure["code"])
    return entry if entry is not None and entry["code"] == failure["code"] and entry["failure_class"] == failure["failure_class"] else None


def validate_failure(failure: Any) -> dict:
    """Validate B1 metadata by meaning; return a copy of the emitter's object.

    The code is checked by membership. A code this registry lacks, or classes
    differently, is logged as drift and never refused: it keeps the envelope's
    class when that class and its owner action are ones this registry knows,
    otherwise it gets the safe default (a stop with no owner action and the
    generic message). registry_sha256 is diagnostic only. Shape stays strict.
    """
    registry = load_registry()
    if not isinstance(failure, dict) or set(failure) != FAILURE_FIELDS:
        raise FailureContractError("invalid_failure_shape")
    message = failure["user_message"]
    if (not _safe_code(failure["code"]) or
            type(failure["schema_version"]) is not int or failure["schema_version"] != 1 or
            type(failure["registry_version"]) is not int or failure["registry_version"] != registry["registry_version"] or
            not isinstance(failure["registry_sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", failure["registry_sha256"]) or
            type(failure["fingerprint_version"]) is not int or failure["fingerprint_version"] != 1 or
            not isinstance(failure["failure_class"], str) or type(failure["retryable"]) is not bool or
            (failure["failure_class"] in CLASSES and failure["retryable"] != (failure["failure_class"] == "transient")) or
            not isinstance(message, str) or not 0 < len(message) <= 512 or redact_secrets(message) != message or
            any(ord(c) < 32 or ord(c) == 127 for c in message) or
            not isinstance(failure["failure_fingerprint"], str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", failure["failure_fingerprint"])):
        raise FailureContractError("invalid_failure_identity")
    _identifier(failure["stage"])
    if not _LABEL.fullmatch(failure["stage"]):
        raise FailureContractError("invalid_failure_stage")
    entry = _registered(failure)
    default = False
    if entry is None:
        known_class = failure["failure_class"] in CLASSES
        logger.warning("failure_registry_drift code=%s failure_class=%s", failure["code"],
                       failure["failure_class"] if known_class else "unrecognized")
        entry = registry["by_code"]["UNREGISTERED_FAILURE_CODE"]
        try:
            default = not known_class or not _same_action(_action(entry, failure["action"], failure["failure_class"]), failure["action"])
        except FailureContractError:
            default = True
    elif not _same_action(_action(entry, failure["action"]), failure["action"]):
        raise FailureContractError("invalid_failure_action")
    elif failure["user_message"] != entry["user_message"]:
        # Founder copy for a registered code is this registry's, whatever the emitter's copy says.
        logger.warning("failure_registry_drift code=%s user_message", failure["code"])
    if not isinstance(failure["evidence_refs"], list) or len(failure["evidence_refs"]) > MAX_FINDINGS:
        raise FailureContractError("invalid_evidence_references")
    for reference in failure["evidence_refs"]:
        _identifier(reference)
        if reference.startswith("/") or "://" in reference or ".." in PurePosixPath(reference).parts:
            raise FailureContractError("unsafe_evidence_reference")
    result = deepcopy(failure)
    if default:
        result.update(failure_class=entry["failure_class"], retryable=False, action=None, user_message=entry["user_message"])
    elif _registered(failure) is entry:
        result["user_message"] = entry["user_message"]
    return result


def legacy_view(failure: dict) -> dict:
    """The audited Phase A view for policy-off receivers; no retry inference."""
    failure = validate_failure(failure)
    entry = _registered(failure)
    if entry is None:
        # Membership fallback: an unknown or reclassified code keeps its own view.
        return {"code": failure["code"], "failure_class": failure["failure_class"], "retryable": failure["retryable"]}
    return deepcopy(entry["legacy"])


def attach_failure_projection(payload: dict, failure: dict, occurrence_id: str | None = None) -> dict:
    """Copy one causal object to every compatibility projection without reclassification."""
    result = deepcopy(payload)
    failure = validate_failure(failure)
    aliases = {"failure": failure, "failure_class": failure["failure_class"], "failure_fingerprint": failure["failure_fingerprint"],
               "error_code": failure["code"], "retryable": failure["retryable"]}
    if occurrence_id:
        try:
            uuid.UUID(occurrence_id)
        except (ValueError, TypeError, AttributeError) as exc:
            raise FailureContractError("invalid_failure_occurrence") from exc
        aliases["failure_occurrence_id"] = occurrence_id
    result.update(deepcopy(aliases))
    for key in ("article_system_setup", "result", "live_preview", "livePreview"):
        if isinstance(result.get(key), dict):
            result[key].update(deepcopy(aliases))
            if key in {"live_preview", "livePreview"}:
                result[key]["errorCode"] = failure["code"]
    return result


def resolve_failure(code, result=None, *, retryable=False, status=None, stage="failure", action=None,
                    findings=None, apply=False, observe=False, local=False):
    """Separate strict B1, audited v0 and shadow policy without scheduling effects.

    Incompatible versioned metadata never falls through to HTTP/retry inference.
    Legacy observations remain readable; unrecognized historical codes are bounded
    attention when the new policy is enabled, rather than guessed transient work.
    """
    from .website_failure_classes import legacy_failure_class
    result = result if isinstance(result, dict) else {}
    nested = result.get("article_system_setup")
    nested = nested if isinstance(nested, dict) else {}
    incoming = result.get("failure") or nested.get("failure")
    versioned = isinstance(incoming, dict) and "schema_version" in incoming
    failure = None
    invalid = False
    if versioned:
        try:
            failure = validate_failure(incoming)
            sources = [result, nested]
            sources.extend(result[key] for key in ("live_preview", "livePreview") if isinstance(result.get(key), dict))
            for source in sources:
                if "failure" in source and source["failure"] != incoming:
                    raise FailureContractError("inconsistent_failure_projection")
                # Projections must agree with what the emitter sent, even when
                # validation substituted the safe default for an unknown class.
                for alias, expected in (("error_code", incoming["code"]), ("errorCode", incoming["code"]), ("failure_class", incoming["failure_class"]),
                                        ("failure_fingerprint", incoming["failure_fingerprint"]), ("retryable", incoming["retryable"])):
                    if alias in source and (type(source[alias]) is not type(expected) or source[alias] != expected):
                        raise FailureContractError("inconsistent_failure_projection")
                occurrence = source.get("failure_occurrence_id")
                if occurrence is not None:
                    if not isinstance(occurrence, str):
                        raise FailureContractError("invalid_failure_occurrence")
                    try:
                        uuid.UUID(occurrence)
                    except (ValueError, TypeError, AttributeError):
                        raise FailureContractError("invalid_failure_occurrence") from None
            if result.get("failure_occurrence_id") and nested.get("failure_occurrence_id") and result["failure_occurrence_id"] != nested["failure_occurrence_id"]:
                raise FailureContractError("inconsistent_failure_occurrence")
        except FailureContractError:
            invalid = True
            failure = classify_failure("FAILURE_CONTRACT_INVALID", "failure_contract")
    elif (apply or observe) and local:
        failure = classify_failure(code, stage, findings, action)
    if failure is not None and (apply or invalid):
        return {**failure, "failure": failure, "versioned": versioned, "contract_invalid": invalid}
    if failure is not None and (versioned or local):
        legacy = legacy_view(failure)
        if local and not versioned and legacy["code"] == code:
            legacy["failure_class"] = legacy_failure_class(code, result, retryable=retryable, status=status)
            legacy["retryable"] = legacy["failure_class"] == "transient"
        return {**legacy, "failure": failure if observe or versioned else None, "versioned": versioned,
                "contract_invalid": False}
    classification = legacy_failure_class(code, result, retryable=retryable, status=status)
    if apply and (not isinstance(code, str) or code not in load_registry()["by_code"]):
        # Unknown historical material is observed, never re-emitted or hashed raw.
        failure = classify_failure("UNREGISTERED_FAILURE_CODE", stage)
        return {**failure, "failure": failure, "versioned": False, "contract_invalid": True}
    return {"code": code, "failure_class": classification, "retryable": classification == "transient",
            "failure": None, "versioned": False, "contract_invalid": False}
