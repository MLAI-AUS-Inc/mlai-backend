"""Typed ORM predicates and capability metadata for protected audience facts."""

from __future__ import annotations

from datetime import timedelta, timezone
from functools import reduce
from operator import and_, or_

from django.db.models import Count, DateTimeField, Exists, Min, OuterRef, Q
from django.db.models.fields.json import KeyTextTransform
from django.db.models.functions import Cast

from .audience_profiles import AUDIENCE_PROFILE_KEY, MAX_ACTIVITY_LAG_SECONDS
from .audience_taxonomy import ACTIVITY_SOURCES, FACETS, FACET_STATUSES, TAXONOMY_VERSION

PREFIX = f"attributes__{AUDIENCE_PROFILE_KEY}"
FACET_OPS = ("has_any", "has_all", "has_none", "is_provided", "is_not_provided", "has_status")
PROFILE_RULE_TYPES = {
    "audience_facet": {"label": "Audience profile", "keys": {"facet", "op", "values", "status"}, "available": True},
    "location_affinity": {"label": "City connection", "keys": {"op", "city_ids"}, "available": True},
    "activity": {"label": "Observed activity", "keys": {"source", "op", "within_days"}, "available": True},
}


def _error(reason, at):
    from .segments import SegmentError
    pointer = "".join("/" + str(item).replace("~", "~0").replace("/", "~1") for item in at)
    raise SegmentError("invalid_definition", pointer or "/", reason)


def validate_profile_rule(rule: dict, at: tuple = ()) -> dict:
    """Return leaf fields only, using the same strict error pointers as existing rules."""

    if not isinstance(rule, dict):
        _error("expected_object", at)
    kind = rule.get("type")
    if not isinstance(kind, str) or kind not in PROFILE_RULE_TYPES:
        _error("unknown_rule_type", at + ("type",))
    unknown = set(rule) - PROFILE_RULE_TYPES[kind]["keys"] - {"type"}
    if unknown:
        _error("unknown_key", at + (sorted(unknown)[0],))
    if kind == "activity":
        source, op, days = rule.get("source"), rule.get("op"), rule.get("within_days")
        if not isinstance(source, str) or source not in ACTIVITY_SOURCES:
            _error("unknown_activity_source", at + ("source",))
        if op not in ("within", "not_within"):
            _error("expected_one_of", at + ("op",))
        if type(days) is not int or not 1 <= days <= 3650:
            _error("expected_integer_between_1_and_3650", at + ("within_days",))
        return {"source": source, "op": op, "within_days": days}
    facet = "location_affinity" if kind == "location_affinity" else rule.get("facet")
    if not isinstance(facet, str) or facet not in FACETS:
        _error("unknown_facet", at + ("facet",))
    op = rule.get("op")
    if op not in FACET_OPS and not (kind == "location_affinity" and op == "multiple"):
        _error("expected_one_of", at + ("op",))
    if kind == "location_affinity" and op == "has_status":
        _error("use_audience_facet_for_status", at + ("op",))
    result = {"op": op}
    if kind == "audience_facet":
        result["facet"] = facet
    key = "city_ids" if kind == "location_affinity" else "values"
    if op in ("has_any", "has_all", "has_none"):
        values = rule.get(key)
        if not isinstance(values, list) or not 1 <= len(values) <= 50:
            _error("expected_bounded_nonempty_array", at + (key,))
        if any(not isinstance(value, str) or value not in FACETS[facet]["options"] for value in values):
            _error("unknown_value", at + (key,))
        if op == "has_all" and not FACETS[facet]["multiple"] and len(set(values)) > 1:
            _error("single_value_facet", at + (key,))
        result[key] = sorted(set(values))
    elif key in rule:
        _error("unexpected_values", at + (key,))
    if op == "has_status":
        if rule.get("status") not in FACET_STATUSES:
            _error("unknown_status", at + ("status",))
        result["status"] = rule["status"]
    elif "status" in rule:
        _error("unexpected_status", at + ("status",))
    return result


def _valid_profile() -> Q:
    return Q(**{PREFIX + "__schema_version": 1, PREFIX + "__taxonomy_version": TAXONOMY_VERSION})


def _timestamp(value) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _activity_coverage_q(rule: dict, now) -> Q:
    base = PREFIX + "__activity__" + rule["source"]
    return _valid_profile() & Q(**{
        base + "__status": "available", base + "__identity_status": "verified",
        base + "__covered_from__lte": _timestamp(now - timedelta(days=rule["within_days"])),
        base + "__covered_through__gte": _timestamp(now),
    })


def profile_rule_q(rule: dict, now, activity_as_of=None) -> Q:
    """Compile known-value predicates; ordinary negative filters never include missing facts."""

    if rule["type"] == "activity":
        observed_until = now
        now = (activity_as_of or {}).get(rule["source"], now)
        base = PREFIX + "__activity__" + rule["source"]
        since = _timestamp(now - timedelta(days=rule["within_days"]))
        observed = _valid_profile() & Q(**{
            base + "__identity_status": "verified", base + "__last_observed_at__gte": since,
            base + "__last_observed_at__lte": _timestamp(observed_until),
        })
        if rule["op"] == "not_within":
            # Negating a JSON comparison alone leaves SQL UNKNOWN for nulls.
            # Explicit absence plus complete coverage is observed inactivity.
            absent = (Q(**{base + "__last_observed_at__isnull": True})
                      | Q(**{base + "__last_observed_at": None})
                      | Q(**{base + "__last_observed_at__lt": since}))
            return _activity_coverage_q(rule, now) & absent
        return observed
    facet = "location_affinity" if rule["type"] == "location_affinity" else rule["facet"]
    base = PREFIX + "__facets__" + facet
    known = _valid_profile() & Q(**{base + "__status": "known", base + "__value_count__gte": 1})
    op = rule["op"]
    if op == "is_provided":
        return known
    if op == "is_not_provided":
        return Q(**{PREFIX + "__isnull": True}) | (_valid_profile() & (
            Q(**{base + "__isnull": True}) | Q(**{base + "__status": "not_provided"})))
    if op == "has_status":
        return _valid_profile() & Q(**{base + "__status": rule["status"]})
    if op == "multiple":
        return known & Q(**{base + "__value_count__gte": 2})
    values = rule.get("city_ids", rule.get("values", []))
    expressions = [Q(**{base + "__values__contains": [value]}) for value in values]
    combined = reduce(and_ if op == "has_all" else or_, expressions)
    return known & (~combined if op == "has_none" else combined)


def _activity_rules(definition: dict, *, negative=False):
    """Find activity leaves whose absence could change set subtraction or negation."""

    if definition.get("version") == 2:
        yield from _activity_nodes(definition.get("include"), negative=negative)
        yield from _activity_nodes(definition.get("exclude"), negative=not negative)
    else:
        for rule in definition.get("rules", []):
            if rule.get("type") == "activity" and (negative or rule.get("op") == "not_within"):
                yield rule
        for rule in definition.get("exclude", []):
            if rule.get("type") == "activity":
                yield rule


def _activity_nodes(node, *, negative=False):
    if not isinstance(node, dict):
        return
    if node.get("kind") == "rule":
        rule = node.get("rule", {})
        if rule.get("type") == "activity" and (negative or rule.get("op") == "not_within"):
            yield rule
    elif node.get("kind") == "group":
        for child in node.get("children", []):
            yield from _activity_nodes(child, negative=negative)
    elif node.get("kind") == "saved_segment_snapshot":
        yield from _activity_rules(node.get("definition", {}), negative=negative)


def ensure_activity_coverage(definition: dict, now, base_queryset=None) -> dict:
    """Fail closed before negative/excluded activity can turn an ingestion gap into membership.

    Return one fresh source watermark per negative/excluded activity source.
    All leaves for that source use the returned instant. Ordinary leaves do no
    I/O; completeness checks occur only when membership depends on absence.
    """

    rules = list(_activity_rules(definition))
    if not rules:
        return {}
    if base_queryset is None:
        from .models import Contact
        base_queryset = Contact.objects.all()
    checked, source_times = set(), {}
    for rule in rules:
        key = (rule["source"], rule["within_days"])
        if key in checked:
            continue
        checked.add(key)
        source = rule["source"]
        if source not in source_times:
            base = PREFIX + "__activity__" + source
            eligible = base_queryset.filter(_valid_profile(), **{
                base + "__status": "available", base + "__identity_status": "verified",
                base + "__covered_through__lte": _timestamp(now),
            })
            watermark = eligible.aggregate(observation_as_of=Min(Cast(
                KeyTextTransform.from_lookup(base + "__covered_through"), output_field=DateTimeField(),
            )))["observation_as_of"]
            if watermark is None or watermark > now or now - watermark > timedelta(seconds=MAX_ACTIVITY_LAG_SECONDS):
                from .segments import SegmentError
                raise SegmentError("rule_not_available", "/", "activity_coverage_incomplete")
            source_times[source] = watermark
        covered = base_queryset.filter(pk=OuterRef("pk")).filter(_activity_coverage_q(rule, source_times[source]))
        covered = covered.filter(**{PREFIX + "__activity__" + source + "__covered_through__lte": _timestamp(now)})
        if base_queryset.filter(~Q(Exists(covered))).exists():
            from .segments import SegmentError
            raise SegmentError("rule_not_available", "/", "activity_coverage_incomplete")
    return source_times


def activity_coverage_metadata(activity_as_of: dict, now) -> list[dict]:
    """Explain the exact source cutoffs used for absence-dependent audience predicates."""

    return [{"source": source, "status": "available", "observation_as_of": _timestamp(instant),
             "lag_seconds": max(0, int((now - instant).total_seconds())),
             "max_lag_seconds": MAX_ACTIVITY_LAG_SECONDS, "includes_newer_observations": True}
            for source, instant in sorted(activity_as_of.items())]


def _orm_profile_counts(base_queryset, *, now=None) -> dict:
    """Portable reference implementation for the PostgreSQL aggregate parity tests."""

    metrics = {"total": Count("pk")}
    for facet, spec in FACETS.items():
        base = PREFIX + "__facets__" + facet
        for status in FACET_STATUSES:
            metrics[f"{facet}_{status}"] = Count("pk", filter=_valid_profile() & Q(**{base + "__status": status}))
        for index, value in enumerate(spec["options"]):
            metrics[f"{facet}_option_{index}"] = Count("pk", filter=profile_rule_q({
                "type": "audience_facet", "facet": facet, "op": "has_any", "values": [value]}, now))
    for source in ACTIVITY_SOURCES:
        base = PREFIX + "__activity__" + source
        known = (_valid_profile() & Q(**{base + "__identity_status": "verified"})
                 & Q(**{base + "__last_observed_at__isnull": False}) & ~Q(**{base + "__last_observed_at": None}))
        if now is not None:
            known &= Q(**{base + "__last_observed_at__lte": _timestamp(now)})
        metrics[f"activity_{source}_known"] = Count("pk", filter=known)
        for status in ("available", "partial", "stale", "unavailable"):
            metrics[f"activity_{source}_{status}"] = Count("pk", filter=_valid_profile() & Q(**{base + "__status": status}))
    return base_queryset.aggregate(**metrics)


def profile_catalog(base_queryset=None, *, now=None) -> dict:
    """Return typed choices and actual aggregate coverage from the authorized queryset."""

    aggregates = {}
    if base_queryset is not None:
        from django.db import connections
        from django.db.models import QuerySet
        from .audience_catalog import postgres_profile_counts
        if isinstance(base_queryset, QuerySet) and connections[base_queryset.db].vendor == "postgresql":
            aggregates = postgres_profile_counts(base_queryset, now=now)
        else:
            aggregates = _orm_profile_counts(base_queryset, now=now)
    facets = []
    for facet, spec in FACETS.items():
        known = aggregates.get(f"{facet}_known")
        present = sum(aggregates.get(f"{facet}_{status}", 0) for status in FACET_STATUSES)
        state = "unmapped" if known is None or not present else "available" if known else "no_data"
        facets.append({
            "id": facet, "label": spec["label"], "status": state,
            "reason": "No trusted source mapping has populated this field." if state == "unmapped" else
                      "No usable reported values are available." if state == "no_data" else "",
            "multiple": spec["multiple"], "self_report_only": spec.get("self_report_only", False),
            "known_count": known, "total_count": aggregates.get("total"),
            "status_counts": {status: aggregates.get(f"{facet}_{status}") for status in FACET_STATUSES},
            "operators": list(FACET_OPS),
            "options": [{"value": value, "label": label, "count": aggregates.get(f"{facet}_option_{index}")}
                        for index, (value, label) in enumerate(spec["options"].items())],
        })
    activities = []
    for source, label in ACTIVITY_SOURCES.items():
        known = aggregates.get(f"activity_{source}_known")
        stale = aggregates.get(f"activity_{source}_stale", 0) + aggregates.get(f"activity_{source}_unavailable", 0)
        status = "available" if known else "temporarily_unavailable" if stale else "no_data" if known == 0 else "unmapped"
        activities.append({"value": source, "label": label, "status": status, "known_count": known,
            "reason": "Known dated observations only; source coverage may be incomplete." if known else
                      "No verified dated observations have been projected for this source.",
            "coverage_counts": {item: aggregates.get(f"activity_{source}_{item}")
                                for item in ("available", "partial", "stale", "unavailable")}})
    return {"taxonomy_version": TAXONOMY_VERSION, "facets": facets, "activity_sources": activities}
