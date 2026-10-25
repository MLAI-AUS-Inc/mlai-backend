"""Closed historical receipt decoder; no comparison writer or rollout gate."""

from datetime import datetime, timezone
import json
import re
import uuid

KEY = "community_source_compare_v1"
EPOCH_KEY = "community_source_epoch_v1"
ID = re.compile(r"^[A-Z][A-Z0-9]{1,23}$")
HEX = re.compile(r"^[a-f0-9]{64}$")
OPERATIONS = {"message", "edit", "delete", "reaction_add", "reaction_remove"}
KINDS = {"message", "own_send", "diagnostic"}
ROOM_FIELDS = {"row_id", "workspace_id", "channel_id", "destination_id"}
MARKER_FIELDS = {"version", "epoch", "cohort", "room", "comparison_id", "operation", "kind", "message_ns", "own_send_ns", "sources"}


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def stamp(value):
    """UTC nanoseconds using integer arithmetic, without a float timestamp."""
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("source_utc_required")
    value = value.astimezone(timezone.utc) - datetime(1970, 1, 1, tzinfo=timezone.utc)
    return (value.days * 86400 + value.seconds) * 10**9 + value.microseconds * 1000


def identifier(value, prefix):
    return type(value) is str and bool(ID.fullmatch(value)) and value.startswith(prefix)


def uuid_value(value):
    if type(value) is not str:
        return False
    try:
        return str(uuid.UUID(value)) == value
    except (ValueError, AttributeError):
        return False


def room_valid(value):
    return (type(value) is dict and set(value) == ROOM_FIELDS
            and type(value["row_id"]) is int and 0 < value["row_id"] < 2**63
            and identifier(value["workspace_id"], "T")
            and identifier(value["channel_id"], "C") and uuid_value(value["destination_id"]))


def marker(value):
    """Never return a partially decoded or open provider-controlled object."""
    if (type(value) is not dict or set(value) != MARKER_FIELDS
            or type(value.get("version")) is not int or value["version"] != 1
            or not uuid_value(value["epoch"]) or not room_valid(value["room"])
            or type(value["cohort"]) is not str or not HEX.fullmatch(value["cohort"])
            or type(value["comparison_id"]) is not str or not HEX.fullmatch(value["comparison_id"])
            or type(value["operation"]) is not str or value["operation"] not in OPERATIONS
            or type(value["kind"]) is not str or value["kind"] not in KINDS
            or type(value["message_ns"]) is not int or not 0 < value["message_ns"] < 10**30
            or type(value["own_send_ns"]) is not int or not 0 <= value["own_send_ns"] < 10**30
            or type(value["sources"]) is not dict or not value["sources"]
            or not set(value["sources"]) <= {"bot", "members"}
            or any(type(at) is not int or at <= 0 or at >= 10**30 for at in value["sources"].values())):
        return None
    if (((value["operation"] == "message") != (value["kind"] in {"message", "own_send"}))
            or (value["kind"] == "own_send") != (value["own_send_ns"] > 0)):
        return None
    return json.loads(canonical(value))
