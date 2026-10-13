"""Scan all verified owner devices without skipping a partially scanned cursor."""

import re
from uuid import UUID

from .native_sources import NativeCursorReset

MAX_CALLERS = 20
HEX_KEY = re.compile(r"^[0-9a-f]{64}$")


def caller_batches(owner_key, caller_keys):
    """Include the bound owner in each bounded batch of distinct owned keys."""
    if not isinstance(owner_key, str) or not HEX_KEY.fullmatch(owner_key):
        raise ValueError("Invalid native owner key.")
    seen = {owner_key}
    batch = [owner_key]
    for key in caller_keys:
        if not isinstance(key, str) or not HEX_KEY.fullmatch(key):
            raise ValueError("Invalid native caller key.")
        if key in seen:
            continue
        seen.add(key)
        batch.append(key)
        if len(batch) == MAX_CALLERS:
            yield batch
            batch = [owner_key]
    if len(batch) > 1 or len(seen) == 1:
        yield batch


def scan_mentions(owner_key, caller_keys, cursor, query):
    """Return deduplicated events and the position fully scanned by every batch.

    Every query starts from the same saved cursor. A later batch may observe a
    newer head; only the minimum completed position is safe to commit. Events
    beyond that position may be admitted early and will be deduplicated by the
    durable invocation source constraint when they appear again next tick.
    """
    request_cursor = {}
    if cursor.get("epoch") is not None and cursor.get("sequence") is not None:
        request_cursor = {"epoch": cursor["epoch"], "after_sequence": cursor["sequence"]}
    epoch = request_cursor.get("epoch")
    sequence = None
    events = {}
    for callers in caller_batches(owner_key, caller_keys):
        result = query(caller_pubkeys=callers, **request_cursor)
        next_cursor = result.get("cursor")
        if (not isinstance(next_cursor, dict) or set(next_cursor) != {"epoch", "sequence"}
                or not isinstance(next_cursor["epoch"], str)
                or type(next_cursor["sequence"]) is not int or next_cursor["sequence"] < 0):
            raise ValueError("Native cursor is invalid.")
        UUID(next_cursor["epoch"])
        if epoch is not None and next_cursor["epoch"] != epoch:
            raise NativeCursorReset()
        epoch = next_cursor["epoch"]
        if next_cursor["sequence"] < request_cursor.get("after_sequence", 0):
            raise ValueError("Native cursor moved backwards.")
        sequence = next_cursor["sequence"] if sequence is None else min(sequence, next_cursor["sequence"])
        page = result.get("events")
        if not isinstance(page, list) or len(page) > 100:
            raise ValueError("Native event page is invalid.")
        for event in page:
            if (not isinstance(event, dict) or not isinstance(event.get("event_id"), str)
                    or not HEX_KEY.fullmatch(event["event_id"])):
                raise ValueError("Invalid native trigger.")
            existing = events.get(event["event_id"])
            if existing is not None and existing != event:
                raise ValueError("Native trigger changed between batches.")
            events[event["event_id"]] = event
    return list(events.values()), {"epoch": epoch, "sequence": sequence}
