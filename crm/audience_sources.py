"""Pure reviewed-source mapping and deterministic audience projection updates.

Adapters supply records already ingested by MLAI. These helpers neither fetch
external profiles nor grant consent, and never infer a demographic from a name.
"""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
from datetime import datetime

from .audience_profiles import (
    AUDIENCE_PROFILE_KEY, AudienceProfileError, audience_profile, empty_profile,
    parse_instant, validate_activity, validate_evidence, validate_facet, validate_profile,
)
from .audience_taxonomy import FACETS, PROVENANCES, SOURCE_KINDS, normalize_value, normalized_label

_AUTHORITY = {"self_reported": 3, "verified": 3, "derived": 2, "imported": 1}
MAPPING_KEYS = {"id", "source_kind", "source_key", "facet", "provenance", "self_report_confirmed", "aliases", "reviewed"}


def validate_mapping(value: dict) -> dict:
    """Validate an explicit reviewed field/question mapping; fuzzy label matching is excluded."""

    if not isinstance(value, dict) or set(value) - MAPPING_KEYS:
        raise AudienceProfileError("invalid_mapping")
    for key in ("id", "source_key"):
        if not isinstance(value.get(key), str) or not value[key].strip() or len(value[key]) > 255:
            raise AudienceProfileError("required", "/" + key)
    if ":" in value["id"]:
        raise AudienceProfileError("mapping_id_contains_reserved_separator", "/id")
    facet = value.get("facet")
    if not isinstance(facet, str) or facet not in FACETS or value.get("source_kind") not in SOURCE_KINDS:
        raise AudienceProfileError("invalid_mapping_target")
    if value.get("reviewed") is not True or value.get("provenance") not in PROVENANCES:
        raise AudienceProfileError("reviewed_mapping_required")
    confirmed = value.get("self_report_confirmed", False)
    if type(confirmed) is not bool:
        raise AudienceProfileError("expected_boolean", "/self_report_confirmed")
    aliases = value.get("aliases", {})
    if not isinstance(aliases, dict) or len(aliases) > 200:
        raise AudienceProfileError("invalid_aliases")
    normalized = {}
    for key, target in aliases.items():
        if not isinstance(key, str) or not key.strip() or len(key) > 255 or not isinstance(target, str) or target not in FACETS[facet]["options"]:
            raise AudienceProfileError("invalid_alias", "/aliases")
        name = normalized_label(key)
        if name in normalized and normalized[name] != target:
            raise AudienceProfileError("ambiguous_alias", "/aliases")
        normalized[name] = target
    # Reuse demographic/stage provenance checks before any records are mapped.
    validate_evidence(facet, {"provenance": value["provenance"], "source_kind": value["source_kind"],
        "source_record_id": "mapping-validation", "observed_at": "2000-01-01T00:00:00Z",
        "self_report_confirmed": confirmed, "values": []})
    return {**deepcopy(value), "self_report_confirmed": confirmed, "aliases": normalized}


def mapped_observation(mapping: dict, raw, *, source_record_id: str, observed_at: str, now: datetime) -> dict:
    """Normalize a supplied answer without treating unknown text as a known demographic."""

    mapping = validate_mapping(mapping)
    facet = mapping["facet"]
    instant = parse_instant(observed_at)
    if instant > now:
        raise AudienceProfileError("future_observation", "/observed_at")
    raw_values = raw if isinstance(raw, list) else [raw]
    if len(raw_values) > 50 or any(item is not None and (not isinstance(item, str) or len(item) > 2000) for item in raw_values):
        raise AudienceProfileError("invalid_source_value")
    nonempty = [item for item in raw_values if isinstance(item, str) and item.strip()]
    withheld = any(normalized_label(item) in ("prefer not to say", "prefer not to disclose") for item in nonempty)
    values = [normalize_value(facet, item, aliases=mapping["aliases"]) for item in nonempty]
    status = "withheld" if withheld else "not_provided" if not nonempty else "unmapped" if None in values else "known"
    canonical_values = sorted(set(values)) if status == "known" else []
    evidence = validate_evidence(facet, {"provenance": mapping["provenance"], "source_kind": mapping["source_kind"],
        "source_record_id": source_record_id, "observed_at": observed_at,
        "values": canonical_values, "self_report_confirmed": mapping["self_report_confirmed"]})
    return {"facet": facet, "status": status, "values": canonical_values, "evidence": evidence}


def merge_observation(profile: dict | None, observation: dict) -> dict:
    """Upsert a source record; same-record updates supersede, concurrent contradictions conflict."""

    profile = validate_profile(profile) if profile is not None else empty_profile()
    facet = observation.get("facet")
    if not isinstance(facet, str) or facet not in FACETS:
        raise AudienceProfileError("unknown_facet")
    incoming = validate_facet(facet, {"status": observation.get("status"), "values": observation.get("values", []),
        "evidence": [observation.get("evidence")]})
    previous = profile["facets"].get(facet)
    if previous is None:
        profile["facets"][facet] = incoming
        return validate_profile(profile)
    new_evidence = incoming["evidence"][0]
    identity = (new_evidence["source_kind"], new_evidence["source_record_id"])
    old_evidence = previous["evidence"]
    prior_record = next((item for item in old_evidence if (item["source_kind"], item["source_record_id"]) == identity), None)
    if prior_record and parse_instant(prior_record["observed_at"]) > parse_instant(new_evidence["observed_at"]):
        return profile  # Replaying an older import never replaces a newer report.
    evidence = [item for item in old_evidence if (item["source_kind"], item["source_record_id"]) != identity]
    evidence.append(new_evidence)
    if len(evidence) > 50:
        raise AudienceProfileError("too_much_evidence")
    previous_authority = max((_AUTHORITY[item["provenance"]] for item in old_evidence), default=0)
    if previous["status"] == "withheld" and _AUTHORITY[new_evidence["provenance"]] < previous_authority:
        return profile
    if incoming["status"] in ("withheld", "not_provided", "unmapped"):
        # A same-source explicit retraction removes its prior contribution. A
        # weaker ordinary import cannot clear a person's protected choice.
        if prior_record is None and _AUTHORITY[new_evidence["provenance"]] < previous_authority:
            return profile
        if incoming["status"] == "withheld" and _AUTHORITY[new_evidence["provenance"]] >= previous_authority:
            profile["facets"][facet] = incoming
            return validate_profile(profile)
    usable = [item for item in evidence if item["values"]]
    if not usable:
        profile["facets"][facet] = incoming
        return validate_profile(profile)
    authority = max(_AUTHORITY[item["provenance"]] for item in usable)
    chosen = [item for item in usable if _AUTHORITY[item["provenance"]] == authority]
    values = sorted({value for item in chosen for value in item["values"]})
    conflicted = not FACETS[facet]["multiple"] and len(values) > 1
    profile["facets"][facet] = validate_facet(facet, {
        "status": "conflicted" if conflicted else "known", "values": [] if conflicted else values, "evidence": evidence,
    })
    return validate_profile(profile)


def project_records(attributes, records: list[dict], mappings: list[dict], *, now: datetime) -> tuple[dict, dict]:
    """Preview/apply bounded records with exact mapping IDs; return profile and aggregate counts."""

    if len(records) > 1000 or len(mappings) > 50:
        raise AudienceProfileError("batch_too_large")
    normalized = [validate_mapping(item) for item in mappings]
    by_id = {item["id"]: item for item in normalized}
    if len(by_id) != len(normalized):
        raise AudienceProfileError("duplicate_mapping_id")
    profile = audience_profile(attributes)
    if profile is None and isinstance(attributes, dict) and AUDIENCE_PROFILE_KEY in attributes:
        raise AudienceProfileError("invalid_existing_profile")
    counts = Counter()
    for record in records:
        if not isinstance(record, dict) or set(record) - {"mapping_id", "source_record_id", "observed_at", "value"}:
            raise AudienceProfileError("invalid_source_record")
        mapping_id = record.get("mapping_id")
        mapping = by_id.get(mapping_id) if isinstance(mapping_id, str) else None
        if mapping is None:
            raise AudienceProfileError("unknown_mapping")
        observation = mapped_observation(mapping, record.get("value"), source_record_id=record.get("source_record_id"),
                                         observed_at=record.get("observed_at"), now=now)
        profile = merge_observation(profile, observation)
        counts[observation["status"]] += 1
    return profile or empty_profile(), dict(counts)


def merge_activity(profile: dict | None, source: str, evidence: dict) -> dict:
    """Refresh coverage while retaining a real dated observation across ingestion outages."""

    from .audience_taxonomy import ACTIVITY_SOURCES
    if source not in ACTIVITY_SOURCES:
        raise AudienceProfileError("unknown_activity_source")
    profile = validate_profile(profile) if profile is not None else empty_profile()
    new = validate_activity(evidence)
    old = profile["activity"].get(source)
    if old and old["last_observed_at"] and new["last_observed_at"] is None:
        new["last_observed_at"] = old["last_observed_at"]
        if old["identity_status"] == "verified" and new["identity_status"] == "unmapped":
            # A failed/empty collector cannot erase the attested identity of
            # retained historical evidence. An explicit conflict still wins.
            new["identity_status"] = "verified"
    profile["activity"][source] = new
    return validate_profile(profile)


def retract_mapping_sources(profile: dict, mapping: dict, active_ids: set[str], *, now: datetime) -> dict:
    """Retract removed ingested records belonging to one exact mapping, not other evidence."""

    mapping = validate_mapping(mapping)
    profile = validate_profile(profile)
    facet = profile["facets"].get(mapping["facet"], {})
    prefix = f"mapping:{mapping['id']}:"
    for evidence in list(facet.get("evidence", [])):
        if (evidence["source_kind"] == mapping["source_kind"]
                and evidence["source_record_id"].startswith(prefix)
                and evidence["source_record_id"] not in active_ids and evidence["values"]):
            profile = merge_observation(profile, {
                "facet": mapping["facet"], "status": "not_provided", "values": [],
                # Removing an ingested record is not a newer personal answer.
                # Retain its source time so an exact restoration can supersede
                # this empty contribution, and repeated refreshes remain inert.
                "evidence": {**evidence, "values": []},
            })
    return profile


def activity_projection(observations, coverage: dict, *, now: datetime) -> dict:
    """Adapt existing scored evidence without asserting complete historical coverage it lacks."""

    from .audience_taxonomy import ACTIVITY_SOURCES
    kinds = {"luma_checkin": ("luma", "checkin"), "slack_message": ("slack", "slack_message"),
             "app_login": ("app", "app_login"), "newsletter_click": ("newsletter", "newsletter_click")}
    result = {}
    for target in ACTIVITY_SOURCES:
        source, kind = kinds[target]
        dated = []
        for item in observations:
            if getattr(item, "source", None) != source or getattr(item, "kind", None) != kind:
                continue
            raw = getattr(item, "occurred_at", None)
            if isinstance(raw, datetime) and raw.tzinfo is not None and raw <= now:
                dated.append(raw)
            elif isinstance(raw, str):
                try:
                    instant = parse_instant(raw)
                    if instant <= now:
                        dated.append(instant)
                except AudienceProfileError:
                    pass  # A date-only observation cannot be assigned an invented time.
        source_coverage = coverage.get(source, {})
        status = source_coverage.get("status", "unavailable")
        complete = (status == "available" and source_coverage.get("covered_from")
                    and source_coverage.get("covered_through") and source_coverage.get("identity_status") == "verified")
        result[target] = validate_activity({
            "last_observed_at": max(dated).isoformat() if dated else None,
            "covered_from": source_coverage.get("covered_from") if complete else None,
            "covered_through": source_coverage.get("covered_through") if complete else None,
            "status": "available" if complete else "partial" if status in ("available", "partial") else status,
            "identity_status": "verified" if dated or complete else "unmapped",
        })
    return result
