"""Bounded, versioned audience expressions and portable saved-segment copies.

Snapshots contain their entire definition. Evaluating an audience never follows
a mutable database reference, so edits or deletion of its source cannot change it.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from functools import reduce
from operator import and_, or_

from django.db.models import Q
from django.conf import settings

MAX_DEPTH = 6
MAX_NODES = 100
MAX_SNAPSHOTS = 10
MAX_BYTES = 64 * 1024
_HASH = re.compile(r"^[0-9a-f]{64}$")


def builder_enabled() -> bool:
    """Whether admins can author v2 audiences; saved definitions always remain readable."""

    return bool(getattr(settings, "CRM_SEGMENT_BUILDER_V2_ENABLED", True))


def canonical_json(value) -> str:
    """Serialize validated JSON consistently for provenance and concurrency."""

    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _invalid(at, reason):
    from .segments import SegmentError, _pointer

    return SegmentError("invalid_definition", _pointer(*at) or "/", reason)


def _keys(value, allowed, at):
    if not isinstance(value, dict):
        raise _invalid(at, "expected_object")
    for key in value:
        if key not in allowed:
            raise _invalid((*at, key), "unknown_key")


def validate_v2(definition: dict) -> dict:
    """Validate a v2 tree, counting every embedded leaf against shared limits."""

    try:
        if len(canonical_json(definition).encode("utf-8")) > MAX_BYTES:
            raise _invalid((), "definition_too_large")
    except (ValueError, TypeError, RecursionError) as exc:
        if getattr(exc, "code", None):
            raise
        raise _invalid((), "invalid_json") from None
    budget = {"nodes": 0, "leaves": 0, "snapshots": 0}
    return _definition(definition, (), 0, budget)


def _definition(value, at, depth, budget):
    _keys(value, {"version", "include", "exclude"}, at)
    if type(value.get("version")) is not int or value["version"] != 2:
        raise _invalid((*at, "version"), "unsupported_version")
    include = _node(value.get("include"), (*at, "include"), depth, budget)
    exclude = value.get("exclude")
    if exclude is not None:
        exclude = _node(exclude, (*at, "exclude"), depth, budget)
    return {"version": 2, "include": include, "exclude": exclude}


def _node(value, at, depth, budget):
    from .segments import MAX_RULES, _text, _uuid, _validate_rule

    if depth > MAX_DEPTH:
        raise _invalid(at, "too_deep")
    budget["nodes"] += 1
    if budget["nodes"] > MAX_NODES:
        raise _invalid(at, "too_many_nodes")
    if not isinstance(value, dict):
        raise _invalid(at, "expected_object")
    kind = value.get("kind")
    if kind == "all_contacts":
        _keys(value, {"kind"}, at)
        return {"kind": kind}
    if kind == "rule":
        _keys(value, {"kind", "rule"}, at)
        budget["leaves"] += 1
        if budget["leaves"] > MAX_RULES:
            raise _invalid(at, "too_many_rules")
        return {"kind": kind, "rule": _validate_rule(value.get("rule"), (*at, "rule"))}
    if kind == "group":
        _keys(value, {"kind", "match", "children"}, at)
        if value.get("match") not in ("all", "any"):
            raise _invalid((*at, "match"), "expected_one_of")
        children = value.get("children")
        if not isinstance(children, list):
            raise _invalid((*at, "children"), "expected_array")
        if not children:
            raise _invalid((*at, "children"), "empty_group")
        if len(children) > MAX_NODES:
            raise _invalid((*at, "children"), "too_many_nodes")
        return {
            "kind": kind,
            "match": value["match"],
            "children": [
                _node(child, (*at, "children", index), depth + 1, budget)
                for index, child in enumerate(children)
            ],
        }
    if kind == "saved_segment_snapshot":
        _keys(value, {
            "kind", "source_segment_id", "source_definition_hash", "source_name",
            "captured_at", "definition",
        }, at)
        budget["snapshots"] += 1
        if budget["snapshots"] > MAX_SNAPSHOTS:
            raise _invalid(at, "too_many_snapshots")
        source_hash = value.get("source_definition_hash")
        if not isinstance(source_hash, str) or not _HASH.fullmatch(source_hash):
            raise _invalid((*at, "source_definition_hash"), "invalid_hash")
        captured = value.get("captured_at")
        try:
            if not isinstance(captured, str) or len(captured) > 64:
                raise ValueError
            timestamp = datetime.fromisoformat(captured.replace("Z", "+00:00"))
            if timestamp.tzinfo is None:
                raise ValueError
            captured_at = timestamp.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        except (ValueError, OverflowError):
            raise _invalid((*at, "captured_at"), "invalid_timestamp") from None
        return {
            "kind": kind,
            "source_segment_id": _uuid(value.get("source_segment_id"), (*at, "source_segment_id")),
            "source_definition_hash": source_hash,
            "source_name": _text(200)(value.get("source_name"), (*at, "source_name")),
            "captured_at": captured_at,
            "definition": _definition(value.get("definition"), (*at, "definition"), depth + 1, budget),
        }
    raise _invalid((*at, "kind"), "unknown_node_kind")


def as_v2(definition) -> dict:
    """Return a v2 representation without changing a legacy audience's meaning."""

    from .segments import validate_definition

    value = validate_definition(definition)
    if value.get("version") == 2:
        return value
    include = {"kind": "all_contacts"}
    if value["rules"]:
        include = {
            "kind": "group", "match": value["match"],
            "children": [{"kind": "rule", "rule": rule} for rule in value["rules"]],
        }
    exclude = None
    if value["exclude"]:
        exclude = {
            "kind": "group", "match": "any",
            "children": [{"kind": "rule", "rule": rule} for rule in value["exclude"]],
        }
    return {"version": 2, "include": include, "exclude": exclude}


def definition_hash(definition) -> str:
    """Hash canonical logic, treating equivalent legacy envelopes consistently."""

    return hashlib.sha256(canonical_json(as_v2(definition)).encode("utf-8")).hexdigest()


def definition_etag(segment) -> str:
    """Opaque edit token; count-cache timestamps are deliberately excluded."""

    content = {
        "id": str(segment.pk), "name": segment.name, "description": segment.description,
        "definition": definition_hash(segment.definition),
        "updated_at": segment.updated_at.isoformat() if segment.updated_at else None,
    }
    return '"' + hashlib.sha256(canonical_json(content).encode("utf-8")).hexdigest() + '"'


def tree_q(definition, *, now, activity_as_of=None):
    """Compile one validated v2 expression, preserving local exclusions."""

    result = node_q(definition["include"], ("include",), now, activity_as_of=activity_as_of)
    if definition["exclude"] is not None:
        result &= ~node_q(definition["exclude"], ("exclude",), now, activity_as_of=activity_as_of)
    return result


def node_q(node, at, now, *, activity_as_of=None):
    """Compile a validated node using only allowlisted ORM predicates."""

    from .segments import _rule_q

    if node["kind"] == "all_contacts":
        # Unlike Q(), this remains true when ORed or negated by its parent.
        return Q(pk__isnull=False)
    if node["kind"] == "rule":
        return _rule_q(node["rule"], (*at, "rule"), now, activity_as_of=activity_as_of)
    if node["kind"] == "group":
        return reduce(and_ if node["match"] == "all" else or_, [
            node_q(child, (*at, "children", index), now, activity_as_of=activity_as_of)
            for index, child in enumerate(node["children"])
        ])
    nested = node["definition"]
    result = node_q(nested["include"], (*at, "definition", "include"), now, activity_as_of=activity_as_of)
    if nested["exclude"] is not None:
        result &= ~node_q(nested["exclude"], (*at, "definition", "exclude"), now, activity_as_of=activity_as_of)
    return result


def snapshot_nodes(definition):
    """Yield every embedded source copy, without looking up mutable sources."""

    if definition.get("version") != 2:
        return

    def walk(node):
        if node is None:
            return
        if node["kind"] == "saved_segment_snapshot":
            yield node
            yield from snapshot_nodes(node["definition"])
        elif node["kind"] == "group":
            for child in node["children"]:
                yield from walk(child)

    yield from walk(definition["include"])
    yield from walk(definition["exclude"])
