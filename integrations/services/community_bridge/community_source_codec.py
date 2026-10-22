"""Closed content-free source-comparison values and exact integer gate math."""

from datetime import datetime, timezone
import hashlib
import json
import re
import uuid

KEY = "community_source_compare_v1"
EPOCH_KEY = "community_source_epoch_v1"
WEEK_NS = 604800 * 10**9
STALE_NS = 180 * 10**9  # Existing message_sync_status worker-health bound.
ID = re.compile(r"^[A-Z][A-Z0-9]{1,23}$")
TS = re.compile(r"^[0-9]{1,20}\.[0-9]{1,9}$")
HEX = re.compile(r"^[a-f0-9]{64}$")
OPERATIONS = {"message", "edit", "delete", "reaction_add", "reaction_remove"}
KINDS = {"message", "own_send", "diagnostic"}
ROOM_FIELDS = {"row_id", "workspace_id", "channel_id", "destination_id"}
MARKER_FIELDS = {"version", "epoch", "cohort", "room", "comparison_id", "operation", "kind", "message_ns", "own_send_ns", "sources"}


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def digest(domain, value):
    return hashlib.sha256(domain + canonical(value)).hexdigest()


def stamp(value):
    """UTC nanoseconds using integer arithmetic, without a float timestamp."""
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("source_utc_required")
    value = value.astimezone(timezone.utc) - datetime(1970, 1, 1, tzinfo=timezone.utc)
    return (value.days * 86400 + value.seconds) * 10**9 + value.microseconds * 1000


def timestamp(value):
    if type(value) is not str or not TS.fullmatch(value):
        raise ValueError("source_message_timestamp")
    seconds, fraction = value.split(".")
    number = int(seconds) * 10**9 + int(fraction.ljust(9, "0"))
    if number <= 0:
        raise ValueError("source_message_timestamp")
    return f"{int(seconds)}.{fraction.rstrip('0') or '0'}", number


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


def merge(existing, sighting):
    incoming = marker(sighting)
    if incoming is None:
        raise ValueError("source_sighting_invalid")
    current = marker(existing)
    if current is None:
        return incoming
    identity = MARKER_FIELDS - {"sources", "kind", "own_send_ns"}
    if any(current[key] != incoming[key] for key in identity):
        raise ValueError("source_observation_binding_changed")
    if current["kind"] == "own_send" or incoming["kind"] == "own_send":
        current["kind"] = "own_send"
        current["own_send_ns"] = min(at for at in (current["own_send_ns"], incoming["own_send_ns"]) if at > 0)
    elif current["kind"] != incoming["kind"]:
        raise ValueError("source_observation_kind_changed")
    for role, at in incoming["sources"].items():
        current["sources"][role] = min(current["sources"].get(role, at), at)
    return current


def report(values, *, epoch_id, cohort, start_ns, cutoff_ns, complete, reader_counts, partial=False):
    """Relative bot-reference callback coverage, never provider completeness."""
    if (not uuid_value(epoch_id) or type(start_ns) is not int or start_ns <= 0
            or type(partial) is not bool or type(cutoff_ns) is not int
            or cutoff_ns < start_ns or (not partial and cutoff_ns < start_ns + WEEK_NS)
            or type(complete) is not bool or type(cohort) is not list or len(cohort) != 15
            or not all(room_valid(room) for room in cohort)
            or len({room["row_id"] for room in cohort}) != 15
            or type(reader_counts) is not dict):
        raise ValueError("source_report_binding")
    cohort_hash = digest(b"mlai:community-source-cohort:v1\0", cohort)
    rooms = {room["row_id"]: room for room in cohort}
    grouped, diagnostic, excluded = {}, {key: set() for key in rooms}, {key: set() for key in rooms}
    invalid = 0
    end_ns = start_ns + WEEK_NS
    for raw in values:
        value = marker(raw)
        if value is None:
            invalid += 1
            continue
        if (value["epoch"] != epoch_id or value["cohort"] != cohort_hash
                or value["room"] != rooms.get(value["room"]["row_id"])):
            invalid += 1
            continue
        if not start_ns <= value["message_ns"] < end_ns:
            continue
        key = value["room"]["row_id"]
        if value["operation"] != "message":
            if any(at < value["message_ns"] or at < start_ns for at in value["sources"].values()):
                invalid += 1
            if any(start_ns <= at < cutoff_ns for at in value["sources"].values()):
                diagnostic[key].add(value["comparison_id"])
            continue
        identity = key, value["comparison_id"]
        found = grouped.setdefault(identity, {"sources": {}, "own_send": False})
        if value["kind"] == "own_send":
            if value["own_send_ns"] < value["message_ns"] or value["own_send_ns"] < start_ns:
                invalid += 1
            elif value["own_send_ns"] < cutoff_ns:
                found["own_send"] = True
        for role, at in value["sources"].items():
            if at < value["message_ns"] or at < start_ns:
                invalid += 1
                continue
            if at < cutoff_ns:
                found["sources"][role] = min(found["sources"].get(role, at), at)
    sets = {key: {"bot": set(), "members": set()} for key in rooms}
    for (key, comparison_id), value in grouped.items():
        if value["own_send"]:
            if value["sources"]:
                excluded[key].add(comparison_id)
        else:
            for role in value["sources"]:
                sets[key][role].add(comparison_id)
    rows = []
    for key in rooms:
        bot, member = sets[key]["bot"], sets[key]["members"]
        count = reader_counts.get(key)
        rows.append({"room_id": key, "only_bot": len(bot - member), "only_members": len(member - bot),
                     "both": len(bot & member), "N": len(bot), "M": len(bot - member),
                     "miss_ratio": len(bot - member) / len(bot) if bot else None,
                     "eligible_readers": count if type(count) is int and count >= 0 else None,
                     "excluded_own_send": len(excluded[key]), "mutation_diagnostics": len(diagnostic[key])})
    total_n, total_m = sum(row["N"] for row in rows), sum(row["M"] for row in rows)
    readers_ready = all(type(row["eligible_readers"]) is int and row["eligible_readers"] >= 2 for row in rows)
    conclusive = complete and cutoff_ns >= end_ns and invalid == 0 and total_n > 0 and readers_ready
    return {"version": 1, "epoch": epoch_id, "cohort": cohort_hash, "start_ns": start_ns,
            "end_ns": end_ns, "cutoff_ns": cutoff_ns, "rooms": rows,
            "N": total_n, "M": total_m, "miss_ratio": total_m / total_n if total_n else None,
            "strict_under_half_percent": total_n > 0 and 200 * total_m < total_n,
            "all_rooms_two_readers": readers_ready, "invalid_evidence": invalid,
            "complete": complete, "window_complete": cutoff_ns >= end_ns, "status": "pass" if conclusive and 200 * total_m < total_n else "inconclusive" if not conclusive else "fail",
            "provider_delivery_completeness_proved": False,
            "scope": "relative_authenticated_callbacks_received_before_cutoff"}
