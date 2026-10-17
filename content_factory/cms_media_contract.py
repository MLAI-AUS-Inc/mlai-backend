"""Immutable owned-image intents and uncertain-upload recovery; no authority."""

from copy import deepcopy
from dataclasses import asdict
from datetime import datetime
import re
from uuid import UUID

from .channel_artifact_contract import digest
from .cms_api_transport import CmsApiError
from .cms_connection_contract import validate_binding, CmsContractError
from .cms_media_evidence import _source_url, validate_owned_observation
from .cms_media_transport import UploadedImage, validate_uploaded_image, _media_url

INTENT_FIELDS = {"schema_version", "kind", "binding", "review_operation_id", "handoff_digest", "source_run_id",
    "source_generation", "asset", "upload_id", "title", "digest"}
STATE_FIELDS = {"schema_version", "intent_digest", "phase", "create_count", "probe_count", "acknowledgement", "evidence"}
ACK_FIELDS = {"platform", "provider_id", "upload_id", "source_sha256", "url", "public_bytes_verified"}
PHASES = {"reserved", "create_ready", "upload_issued", "provider_ack", "public_verified", "reconcile_exhausted", "recovery_unsupported"}


def validate_intent(value):
    """Validate a closed original raster/context; no approval is created."""
    try:
        if (not isinstance(value, dict) or set(value) != INTENT_FIELDS or
                type(value["schema_version"]) is not int or value["schema_version"] != 1 or value["kind"] != "cms_owned_media_intent" or
                not isinstance(value["source_run_id"], str) or re.fullmatch(r"[A-Za-z0-9_-]{1,100}", value["source_run_id"]) is None or
                type(value["source_generation"]) is not int or not 0 <= value["source_generation"] < 10**12 or
                not isinstance(value["title"], str) or not 0 < len(value["title"]) <= 200 or any(ord(c) < 32 for c in value["title"]) or
                any(not isinstance(value[key], str) or str(UUID(value[key])) != value[key] for key in ("upload_id", "review_operation_id")) or
                not isinstance(value["handoff_digest"], str) or re.fullmatch(r"[a-f0-9]{64}", value["handoff_digest"]) is None):
            raise CmsApiError("invalid_request")
        validate_binding(value["binding"])
        _source_url(value["asset"])
        if len(value["asset"]["alt"]) > 1000 or value["digest"] != digest({key: item for key, item in value.items() if key != "digest"}):
            raise CmsApiError("invalid_request")
        return value
    except CmsApiError:
        raise
    except (CmsContractError, ValueError, TypeError, KeyError, AttributeError, RecursionError):
        raise CmsApiError("invalid_request") from None


def make_intent(**fields):
    """Seal backend-selected media scope before any upload effect."""
    value = {"schema_version": 1, "kind": "cms_owned_media_intent", **deepcopy(fields)}
    value["digest"] = digest(value)
    return deepcopy(validate_intent(value))


def slug_for(intent):
    """Match the fixed upload transport's UUID/hash slug without reading bytes."""
    validate_intent(intent)
    return "mlai-" + UUID(intent["upload_id"]).hex + "-" + intent["asset"]["sha256"][:16]


def acknowledgement(value, intent):
    """Reconstruct only exact private acknowledgement fields, never worker JSON."""
    try:
        if (not isinstance(value, dict) or set(value) != ACK_FIELDS or value["public_bytes_verified"] is not False or
                value["platform"] != intent["binding"]["platform"] or value["upload_id"] != intent["upload_id"] or
                value["source_sha256"] != intent["asset"]["sha256"] or
                value["platform"] == "wordpress" and (type(value["provider_id"]) is not int or not 0 < value["provider_id"] < 10**19) or
                value["platform"] == "ghost" and value["provider_id"] is not None):
            raise CmsApiError("invalid_response")
        _media_url(value["url"], intent["binding"])
        return UploadedImage(**value)
    except CmsApiError:
        raise
    except (ValueError, TypeError, AttributeError, KeyError):
        raise CmsApiError("invalid_response") from None


def validate_state(state, intent, *, now=None):
    """Reject rollback, duplicate-create counters and unbound public evidence."""
    validate_intent(intent)
    try:
        if (not isinstance(state, dict) or set(state) != STATE_FIELDS or type(state["schema_version"]) is not int or state["schema_version"] != 1 or
                state["intent_digest"] != intent["digest"] or state["phase"] not in PHASES or
                type(state["create_count"]) is not int or state["create_count"] not in {0, 1} or
                type(state["probe_count"]) is not int or not 0 <= state["probe_count"] <= 8):
            raise CmsApiError("invalid_request")
        phase = state["phase"]
        if (phase in {"reserved", "create_ready"} and (state["create_count"] != 0 or state["probe_count"] != 0) or
                phase in {"upload_issued", "reconcile_exhausted", "recovery_unsupported"} and state["create_count"] != 1 or
                (phase == "reconcile_exhausted") != (state["probe_count"] == 8) or
                phase == "recovery_unsupported" and (intent["binding"]["platform"] != "ghost" or state["probe_count"] != 0) or
                phase in {"provider_ack", "public_verified"} and not isinstance(state["acknowledgement"], dict) or
                phase not in {"provider_ack", "public_verified"} and state["acknowledgement"] is not None or
                (phase == "public_verified") != (state["evidence"] is not None)):
            raise CmsApiError("invalid_request")
        if phase in {"provider_ack", "public_verified"}:
            ack = acknowledgement(state["acknowledgement"], intent)
            if phase == "public_verified":
                # A durable journal retains historical observations. Freshness
                # must be requested by the final review/publisher, not used to
                # cancel unfinished staging after an ordinary scheduler wait.
                checked = now if now is not None else datetime.fromisoformat(state["evidence"]["checked_at"].replace("Z", "+00:00"))
                validate_owned_observation(state["evidence"], binding=intent["binding"], handoff_digest=intent["handoff_digest"],
                    asset=intent["asset"], acknowledgement=ack, now=checked)
        return state
    except CmsApiError:
        raise
    except (ValueError, TypeError, KeyError, AttributeError):
        raise CmsApiError("invalid_request") from None


def new_state(intent):
    """Begin with no provider write and no acknowledgement."""
    validate_intent(intent)
    return {"schema_version": 1, "intent_digest": intent["digest"], "phase": "reserved", "create_count": 0,
        "probe_count": 0, "acknowledgement": None, "evidence": None}


def lookup_options(intent, state):
    """Only WordPress exposes an owned attachment lookup for an uncertain POST."""
    validate_state(state, intent)
    if intent["binding"]["platform"] != "wordpress" or state["phase"] not in {"reserved", "upload_issued"}:
        raise CmsApiError("invalid_request")
    return {"method": "GET", "resource": "media", "query": {"slug": slug_for(intent), "context": "edit", "per_page": 2}}


def ghost_ready(intent, state):
    """Ghost has no attachment-list preflight; it still receives only one POST."""
    validate_state(state, intent)
    if intent["binding"]["platform"] != "ghost" or state["phase"] != "reserved":
        raise CmsApiError("invalid_request")
    return {**deepcopy(state), "phase": "create_ready"}


def observe_lookup(intent, state, rows, *, image):
    """Recover an exact UUID/hash-marked attachment; absence never repeats POST."""
    lookup_options(intent, state)
    if not isinstance(rows, list) or len(rows) > 1:
        raise CmsApiError("invalid_response")
    if rows:
        ack = validate_uploaded_image(rows[0], binding=intent["binding"], upload_id=intent["upload_id"], image=image)
        return record_ack(intent, state, ack, from_lookup=True)
    if state["phase"] == "reserved":
        return {**deepcopy(state), "phase": "create_ready"}
    probes = state["probe_count"] + 1
    return {**deepcopy(state), "probe_count": probes, "phase": "reconcile_exhausted" if probes == 8 else "upload_issued"}


def issue_upload(intent, state):
    """Persist this transition before the one permitted provider POST."""
    validate_state(state, intent)
    if state["phase"] != "create_ready":
        raise CmsApiError("invalid_request")
    return {**deepcopy(state), "phase": "upload_issued", "create_count": 1}


def record_ack(intent, state, ack, *, from_lookup=False):
    """Bind provider response to original intent; no public bytes are inferred."""
    validate_state(state, intent)
    if not isinstance(ack, UploadedImage) or state["phase"] not in ({"reserved", "upload_issued"} if from_lookup else {"upload_issued"}):
        raise CmsApiError("invalid_request")
    parsed = acknowledgement(asdict(ack), intent)
    return {**deepcopy(state), "phase": "provider_ack", "acknowledgement": asdict(parsed)}


def ghost_unknown(intent, state):
    """Record missing recovery support without inventing a query or re-upload."""
    validate_state(state, intent)
    if intent["binding"]["platform"] != "ghost" or state["phase"] != "upload_issued":
        raise CmsApiError("invalid_request")
    return {**deepcopy(state), "phase": "recovery_unsupported"}


def record_public(intent, state, evidence):
    """Complete only after exact original public raster bytes were observed."""
    validate_state(state, intent)
    if state["phase"] != "provider_ack":
        raise CmsApiError("invalid_request")
    result = {**deepcopy(state), "phase": "public_verified", "evidence": deepcopy(evidence)}
    validate_state(result, intent)
    return result
