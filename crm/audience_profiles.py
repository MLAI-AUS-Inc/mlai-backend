"""Validated, protected audience facts independent of consent and delivery state.

This module performs no database I/O. Source adapters must attest provenance;
arbitrary imported attributes are never promoted merely because a key matches.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json

from .audience_taxonomy import (
    ACTIVITY_SOURCES, ACTIVITY_STATUSES, FACETS, FACET_STATUSES, IDENTITY_STATUSES,
    PROVENANCES, SOURCE_KINDS, TAXONOMY_VERSION,
)

AUDIENCE_PROFILE_KEY = "audience_profile_v1"
MAX_PROFILE_BYTES = 64 * 1024
MAX_VALUES = 50
MAX_EVIDENCE = 50
MAX_ACTIVITY_LAG_SECONDS = 48 * 60 * 60
EVIDENCE_KEYS = {"provenance", "source_kind", "source_record_id", "observed_at", "values", "self_report_confirmed"}
FACET_KEYS = {"status", "values", "value_count", "evidence"} | EVIDENCE_KEYS
ACTIVITY_KEYS = {"last_observed_at", "covered_from", "covered_through", "status", "identity_status"}


class AudienceProfileError(ValueError):
    """A supplied audience fact violates its typed contract."""

    def __init__(self, reason: str, pointer: str = "/"):
        self.reason, self.pointer = reason, pointer
        super().__init__(reason)


def _object(value, allowed: set, pointer: str) -> dict:
    if not isinstance(value, dict):
        raise AudienceProfileError("expected_object", pointer)
    extra = set(value) - allowed
    if extra:
        raise AudienceProfileError("unknown_key", f"{pointer}/{sorted(extra)[0]}")
    return value


def parse_instant(value, pointer: str = "/", *, nullable=False) -> datetime | None:
    """Accept explicit timezone-aware instants only, never a substituted current time."""

    if value is None and nullable:
        return None
    if not isinstance(value, str) or len(value) > 64:
        raise AudienceProfileError("expected_timestamp", pointer)
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None or result.utcoffset() is None:
            raise AudienceProfileError("timezone_required", pointer)
        return result.astimezone(timezone.utc)
    except (ValueError, OverflowError) as exc:
        if isinstance(exc, AudienceProfileError):
            raise
        raise AudienceProfileError("invalid_timestamp", pointer) from exc


def _iso(value: datetime | None) -> str | None:
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z") if value else None


def _values(facet: str, values, pointer: str, *, allow_empty=True) -> list[str]:
    if not isinstance(values, list) or len(values) > MAX_VALUES:
        raise AudienceProfileError("expected_bounded_values", pointer)
    if any(not isinstance(value, str) or value not in FACETS[facet]["options"] for value in values):
        raise AudienceProfileError("unknown_value", pointer)
    result = sorted(set(values))
    if not allow_empty and not result:
        raise AudienceProfileError("required", pointer)
    if not FACETS[facet]["multiple"] and len(result) > 1:
        raise AudienceProfileError("single_value_required", pointer)
    return result


def validate_evidence(facet: str, value: dict, pointer: str = "/evidence") -> dict:
    """Validate a source attestation; sensitive facets require explicit self-report evidence."""

    _object(value, EVIDENCE_KEYS, pointer)
    if value.get("provenance") not in PROVENANCES:
        raise AudienceProfileError("invalid_provenance", pointer + "/provenance")
    if value.get("source_kind") not in SOURCE_KINDS:
        raise AudienceProfileError("invalid_source", pointer + "/source_kind")
    source_id = value.get("source_record_id")
    if not isinstance(source_id, str) or not source_id.strip() or len(source_id) > 255:
        raise AudienceProfileError("source_reference_required", pointer + "/source_record_id")
    confirmed = value.get("self_report_confirmed", False)
    if type(confirmed) is not bool:
        raise AudienceProfileError("expected_boolean", pointer + "/self_report_confirmed")
    if FACETS[facet].get("self_report_only") and (
        value["provenance"] not in ("self_reported", "verified") or not confirmed
    ):
        raise AudienceProfileError("self_report_evidence_required", pointer)
    if facet == "career_stage" and value["provenance"] not in ("self_reported", "verified"):
        raise AudienceProfileError("reported_or_verified_stage_required", pointer)
    return {
        "provenance": value["provenance"], "source_kind": value["source_kind"],
        "source_record_id": source_id.strip(),
        "observed_at": _iso(parse_instant(value.get("observed_at"), pointer + "/observed_at")),
        "values": _values(facet, value.get("values", []), pointer + "/values"),
        "self_report_confirmed": confirmed,
    }


def validate_facet(facet: str, value, pointer: str = "/facets") -> dict:
    """Return canonical queryable facts, with missingness separate from source availability."""

    if not isinstance(facet, str) or facet not in FACETS:
        raise AudienceProfileError("unknown_facet", pointer)
    _object(value, FACET_KEYS, pointer)
    status = value.get("status")
    if status not in FACET_STATUSES:
        raise AudienceProfileError("invalid_status", pointer + "/status")
    values = _values(facet, value.get("values", []), pointer + "/values", allow_empty=status != "known")
    if status != "known" and values:
        raise AudienceProfileError("unknown_status_has_values", pointer + "/values")
    supplied = value.get("evidence", [])
    if not isinstance(supplied, list) or len(supplied) > MAX_EVIDENCE:
        raise AudienceProfileError("too_much_evidence", pointer + "/evidence")
    evidence = [validate_evidence(facet, item, f"{pointer}/evidence/{index}") for index, item in enumerate(supplied)]
    if not evidence and any(key in value for key in EVIDENCE_KEYS - {"values"}):
        evidence = [validate_evidence(facet, {key: value[key] for key in EVIDENCE_KEYS if key in value}, pointer)]
    if status in ("known", "withheld") and not evidence:
        raise AudienceProfileError("evidence_required", pointer)
    if status == "known" and not set(values).issubset({item for entry in evidence for item in entry["values"]}):
        raise AudienceProfileError("value_without_evidence", pointer + "/values")
    result = {"status": status, "values": values, "value_count": len(values), "evidence": evidence}
    if evidence:
        latest = max(evidence, key=lambda item: item["observed_at"])
        result.update({key: latest[key] for key in EVIDENCE_KEYS - {"values"}})
    return result


def validate_activity(value, pointer: str = "/activity") -> dict:
    """Validate dated activity and explicit source/identity coverage without inventing inactivity."""

    _object(value, ACTIVITY_KEYS, pointer)
    if value.get("status") not in ACTIVITY_STATUSES:
        raise AudienceProfileError("invalid_coverage", pointer + "/status")
    if value.get("identity_status") not in IDENTITY_STATUSES:
        raise AudienceProfileError("invalid_identity", pointer + "/identity_status")
    dates = {key: parse_instant(value.get(key), pointer + "/" + key, nullable=True)
             for key in ("last_observed_at", "covered_from", "covered_through")}
    start, end = dates["covered_from"], dates["covered_through"]
    if bool(start) != bool(end) or (start and end and start > end):
        raise AudienceProfileError("invalid_coverage_interval", pointer)
    if value["status"] == "available" and (not start or not end):
        raise AudienceProfileError("coverage_interval_required", pointer)
    return {**{key: _iso(item) for key, item in dates.items()},
            "status": value["status"], "identity_status": value["identity_status"]}


def validate_profile(value) -> dict:
    """Validate the protected profile envelope before any persistence."""

    _object(value, {"schema_version", "taxonomy_version", "facets", "activity"}, "")
    if type(value.get("schema_version")) is not int or value["schema_version"] != 1:
        raise AudienceProfileError("unsupported_profile_version", "/schema_version")
    if value.get("taxonomy_version") != TAXONOMY_VERSION:
        raise AudienceProfileError("unsupported_taxonomy_version", "/taxonomy_version")
    facets = _object(value.get("facets", {}), set(FACETS), "/facets")
    activity = _object(value.get("activity", {}), set(ACTIVITY_SOURCES), "/activity")
    result = {"schema_version": 1, "taxonomy_version": TAXONOMY_VERSION,
              "facets": {key: validate_facet(key, item, "/facets/" + key) for key, item in facets.items()},
              "activity": {key: validate_activity(item, "/activity/" + key) for key, item in activity.items()}}
    if len(json.dumps(result, ensure_ascii=False).encode()) > MAX_PROFILE_BYTES:
        raise AudienceProfileError("profile_too_large")
    return result


def empty_profile() -> dict:
    """Return an empty typed profile; absence does not imply a demographic value."""

    return {"schema_version": 1, "taxonomy_version": TAXONOMY_VERSION, "facets": {}, "activity": {}}


def audience_profile(attributes) -> dict | None:
    """Return the separate safe DTO, or None for an absent/invalid projection."""

    if not isinstance(attributes, dict) or AUDIENCE_PROFILE_KEY not in attributes:
        return None
    try:
        return validate_profile(attributes[AUDIENCE_PROFILE_KEY])
    except AudienceProfileError:
        return None


def with_profile(attributes, profile: dict) -> dict:
    """Replace only the protected namespace, preserving consent/rating/import metadata."""

    return {**deepcopy(attributes if isinstance(attributes, dict) else {}),
            AUDIENCE_PROFILE_KEY: validate_profile(profile)}


def correction_profile(attributes, data: dict, *, now: datetime) -> dict:
    """Apply an explicitly attributed admin correction without touching contact eligibility."""

    _object(data, {"facet", "status", "values", "source_record_id", "observed_at", "provenance", "self_report_confirmed"}, "")
    facet = data.get("facet")
    if not isinstance(facet, str) or facet not in FACETS:
        raise AudienceProfileError("unknown_facet", "/facet")
    evidence = validate_evidence(facet, {
        "provenance": data.get("provenance", "verified"), "source_kind": "admin_correction",
        "source_record_id": data.get("source_record_id"), "observed_at": data.get("observed_at"),
        "self_report_confirmed": data.get("self_report_confirmed", False), "values": data.get("values", []),
    })
    if parse_instant(evidence["observed_at"]) > now:
        raise AudienceProfileError("future_observation", "/observed_at")
    profile = audience_profile(attributes)
    if profile is None and isinstance(attributes, dict) and AUDIENCE_PROFILE_KEY in attributes:
        raise AudienceProfileError("invalid_existing_profile")
    profile = profile or empty_profile()
    profile["facets"][facet] = validate_facet(facet, {
        "status": data.get("status", "known"), "values": data.get("values", []), "evidence": [evidence],
    }, "/facets/" + facet)
    return validate_profile(profile)


def activity_is_covered(value: dict | None, *, since: datetime, now: datetime) -> bool:
    """True only for a verified identity and complete coverage of the requested interval."""

    try:
        value = validate_activity(value)
    except AudienceProfileError:
        return False
    return bool(value["status"] == "available" and value["identity_status"] == "verified"
                and parse_instant(value["covered_from"]) <= since
                and parse_instant(value["covered_through"]) >= now)


def activity_match(value: dict | None, *, within_days: int, now: datetime, negative=False) -> bool | None:
    """Three-state reference semantics used by pure contract tests and source previews."""

    if type(within_days) is not int or not 1 <= within_days <= 3650:
        raise AudienceProfileError("invalid_days")
    try:
        value = validate_activity(value)
    except AudienceProfileError:
        return None
    if value["identity_status"] != "verified":
        return None
    evaluation_time = now
    if negative:
        through = parse_instant(value["covered_through"], nullable=True)
        if through is None or through > now or now - through > timedelta(seconds=MAX_ACTIVITY_LAG_SECONDS):
            return None
        evaluation_time = through
    since = evaluation_time - timedelta(days=within_days)
    last = parse_instant(value["last_observed_at"], nullable=True)
    if last is not None and last > now:
        return None
    # Later verified positive evidence must not be mistaken for inactivity at
    # the common coverage watermark when only a latest-event projection exists.
    observed = last is not None and since <= last <= now
    if observed:
        return not negative
    if not activity_is_covered(value, since=since, now=evaluation_time):
        return None
    return negative
