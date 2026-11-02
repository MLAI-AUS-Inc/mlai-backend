"""Typed delivery candidates; execution events remain lifecycle authority.

Hashes prove retained bytes, not approval or delivery. A candidate must also
match the canonical current article or independently observed provider effect
before its delivery facts may be projected by the backend owner.
"""
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import re
from time import monotonic
from urllib.parse import quote

from .run_ownership import (
    WIRE_EXECUTION_KEY, WIRE_REQUEST_KEY, require_event_execution, require_event_identity,
)

KINDS = frozenset({"article_body_v1", "draft_ready_v1", "publish_pr_v1"})
MAX_BYTES = 4_000_000
HASH = re.compile(r"[a-f0-9]{64}\Z")


class ArtifactObservationInvalid(ValueError):
    """A bounded typed evidence candidate cannot be admitted or verified."""


@dataclass(frozen=True)
class ValidatedEvidence:
    """Immutable, identity-checked candidates with no delivered-value authority."""

    run_id: str
    admission: dict
    execution: dict
    records: tuple


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
        ensure_ascii=True, allow_nan=False).encode("utf-8")).hexdigest()


def _hash(value):
    return type(value) is str and HASH.fullmatch(value) is not None


def _text(value, maximum):
    return type(value) is str and 0 < len(value) <= maximum


def _timestamp(value):
    from django.utils.dateparse import parse_datetime
    from django.utils.timezone import is_aware
    parsed = parse_datetime(value) if _text(value, 40) else None
    return parsed is not None and is_aware(parsed)


def _payload(kind, payload):
    if not isinstance(payload, dict):
        return False
    if kind == "article_body_v1":
        return (set(payload) == {"format", "body", "revision", "article_sha256"}
            and payload["format"] == "markdown" and _text(payload["body"], 2_000_000)
            and len(payload["body"].encode("utf-8")) <= 2_000_000
            and _text(payload["revision"], 255) and _hash(payload["article_sha256"])
            and hashlib.sha256(payload["body"].encode("utf-8")).hexdigest() == payload["article_sha256"])
    if kind == "draft_ready_v1":
        return (set(payload) == {"version", "revision", "at"} and type(payload["version"]) is int
            and payload["version"] == 1 and _text(payload["revision"], 255) and _timestamp(payload["at"]))
    required = {"kind", "number", "repository_id", "head_repository_id", "branch", "base", "head_sha", "pr_url"}
    optional = {"article_sha256", "review_quality_inputs_sha256"}
    return (required <= set(payload) <= required | optional and payload.get("kind") in {"article", "setup", "scaffold", "proxy"}
        and all(type(payload.get(key)) is int and payload[key] > 0 for key in ("number", "repository_id", "head_repository_id"))
        and all(_text(payload.get(key), 255) for key in ("branch", "base")) and _text(payload.get("pr_url"), 2048)
        and type(payload.get("head_sha")) is str and re.fullmatch(r"[a-f0-9]{40}", payload["head_sha"]) is not None
        and all(payload.get(key) is None or _hash(payload[key]) for key in optional)
        and (payload["kind"] != "article" or _hash(payload.get("article_sha256"))))


def validate_evidence(run, value):
    """Verify exact current read identity and each retained creation identity."""
    required = {"schema_version", "run_id", WIRE_REQUEST_KEY, WIRE_EXECUTION_KEY, "evidence"}
    record_keys = {"schema_version", "run_id", "kind", WIRE_REQUEST_KEY, WIRE_EXECUTION_KEY, "payload",
                   "payload_sha256", "source_sha256", "observed_at", "evidence_sha256"}
    if (not isinstance(value, dict) or set(value) != required or type(value.get("schema_version")) is not int
            or value["schema_version"] != 1 or value.get("run_id") != run.run_id
            or not isinstance(value.get("evidence"), list) or len(value["evidence"]) > 3):
        raise ArtifactObservationInvalid("The evidence envelope is invalid.")
    try:
        require_event_identity(run, value)
        require_event_execution(run, value, current=True)
        if len(json.dumps(value, allow_nan=False).encode("utf-8")) > MAX_BYTES:
            raise ArtifactObservationInvalid("The evidence envelope exceeds its bound.")
        kinds = set()
        for record in value["evidence"]:
            if (not isinstance(record, dict) or set(record) != record_keys
                    or type(record.get("schema_version")) is not int or record["schema_version"] != 1
                    or record.get("run_id") != run.run_id or record.get("kind") not in KINDS
                    or record["kind"] in kinds or not _timestamp(record.get("observed_at"))
                    or not all(_hash(record.get(key)) for key in ("payload_sha256", "source_sha256", "evidence_sha256"))
                    or not _payload(record["kind"], record.get("payload"))
                    or record["payload_sha256"] != _digest(record["payload"])
                    or record["evidence_sha256"] != _digest({key: item for key, item in record.items() if key != "evidence_sha256"})):
                raise ArtifactObservationInvalid("A typed evidence record is invalid.")
            require_event_identity(run, record)
            require_event_execution(run, record)
            kinds.add(record["kind"])
    except (TypeError, ValueError, RecursionError, UnicodeError) as exc:
        raise ArtifactObservationInvalid("The evidence identity or retained bytes changed.") from exc
    return ValidatedEvidence(run.run_id, deepcopy(value[WIRE_REQUEST_KEY]),
        deepcopy(value[WIRE_EXECUTION_KEY]), tuple(deepcopy(value["evidence"])))


def fetch_evidence(run):
    """One bounded authenticated read on the configured CF origin, outside locks."""
    import requests
    from django.db import connection
    from .vibe_marketing_views import _content_factory_headers, _content_factory_remote_config
    from .website_connections import require_unlocked_remote_call
    if connection.in_atomic_block:
        raise ArtifactObservationInvalid("Evidence reads require an unlocked boundary.")
    require_unlocked_remote_call()
    config = _content_factory_remote_config()
    if not config.get("enabled"):
        raise ArtifactObservationInvalid("The current evidence source is unavailable.")
    deadline = monotonic() + 15
    try:
        response = requests.get(config["base_url"].rstrip("/") + "/api/runs/" + quote(run.run_id, safe="") + "/evidence",
            headers=_content_factory_headers(), timeout=(3, 10), stream=True, allow_redirects=False)
    except requests.RequestException as exc:
        raise ArtifactObservationInvalid("The current evidence source is unavailable.") from exc
    try:
        if response.status_code != 200:
            raise ArtifactObservationInvalid("The current evidence source is unavailable.")
        raw = bytearray()
        for part in response.iter_content(chunk_size=1):
            if len(raw) + len(part) > MAX_BYTES or monotonic() > deadline:
                raise ArtifactObservationInvalid("The evidence source exceeds its bound.")
            raw.extend(part)
        return validate_evidence(run, json.loads(raw))
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ArtifactObservationInvalid("The current evidence source is not readable.") from exc
    finally:
        response.close()
