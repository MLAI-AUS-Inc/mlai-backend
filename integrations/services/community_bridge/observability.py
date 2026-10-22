"""Closed, best-effort public bridge observations; never an authority source."""

from contextlib import contextmanager
from contextvars import ContextVar
import logging
import re
from uuid import UUID

from django.db import DEFAULT_DB_ALIAS, connections


logger = logging.getLogger(__name__)
_route = ContextVar("public_bridge_log_route", default=None)
_ID = re.compile(r"[TC][A-Z0-9]{1,23}")
_SCHEMAS = {
    "slack_member_event": {
        "route": {"members", "community"},
        "outcome": {"accepted", "enqueued", "duplicate", "ignored", "failed",
                    "member_source_reduction_queued", "member_leave_observed",
                    "member_join_observed", "member_refresh_queued",
                    "member_audience_pending", "community_source_pending"},
    },
    "slack_member_channel": {
        "outcome": {"room_created", "room_updated", "held", "archived", "audience_updated"},
    },
    "slack_member_send": {
        "poster": {"member", "bot"},
        "outcome": {"settled", "deferred", "retry", "unavailable"},
        "reason": {"recorded_strategy", "reconciled", "authority_or_provider_hold"},
    },
    "slack_public_echo": {"kind": {"create", "edit", "delete", "reaction_add", "reaction_remove"}},
    "slack_community_source compare": {
        "kind": {"observation", "report", "fallback"},
        "population": {"message", "own_send", "diagnostic"},
        "outcome": {"only_bot", "only_members", "both", "pass", "fail", "inconclusive",
                    "no_reader", "members", "bot", "hold"},
    },
}
_NUMBERS = frozenset({"mapping_id", "delivery_id", "receipt_id", "only_bot", "only_members", "both", "N", "M"})
_IDENTITIES = frozenset({"workspace_id", "channel_id", "room_id"})


def _closed(family, fields):
    schema = _SCHEMAS.get(family)
    if schema is None or not fields or not set(fields) <= set(schema) | _NUMBERS | _IDENTITIES:
        return None
    result = {}
    for key, value in fields.items():
        if key in schema:
            if type(value) is not str or value not in schema[key]:
                return None
        elif key in _NUMBERS:
            minimum = 1 if key.endswith("_id") else 0
            if type(value) is not int or not minimum <= value < 2**63:
                return None
        elif key == "room_id":
            if type(value) is not str:
                return None
            try:
                if str(UUID(value)) != value:
                    return None
            except (ValueError, AttributeError):
                return None
        elif type(value) is not str or not _ID.fullmatch(value) or not value.startswith("T" if key == "workspace_id" else "C"):
            return None
        result[key] = value
    return result


def emit(family, **fields):
    """Snapshot primitives and wait for a real commit without opening a DB.

    A closed connection or manual transaction cannot certify a commit. Logging
    and handler failures are swallowed; they never change delivery semantics.
    """
    try:
        snapshot = _closed(family, fields)
        if snapshot is None:
            return
        def committed():
            try:
                logger.info("%s %s", family, " ".join(f"{key}={value}" for key, value in sorted(snapshot.items())))
            except Exception:
                pass
        connection = getattr(connections._connections, DEFAULT_DB_ALIAS, None)
        if connection is None or connection.connection is None:
            return
        if connection.in_atomic_block:
            connection.on_commit(committed, robust=True)
        elif connection.autocommit is True:
            committed()
    except Exception:
        pass


def room_snapshot(row):
    """Only an existing mapped public-source bridge row, never a Slack hint.

    Former public rooms may now be archived/private/held. Their mapping IDs
    remain useful lifecycle metadata; this never inspects a private mirror.
    """
    try:
        if row.audience not in {"members", "community"} or row.destination_platform != "buzz":
            return None
        fields = {"mapping_id": row.pk, "workspace_id": row.slack_workspace_id,
                  "channel_id": row.slack_channel_id}
        if _closed("slack_member_channel", fields) is None:
            return None
        room = row.destination_channel_id
        if type(room) is str:
            try:
                if str(UUID(room)) == room:
                    fields["room_id"] = room
            except ValueError:
                pass
        return fields
    except Exception:
        return None


def room(row, outcome):
    snapshot = room_snapshot(row)
    if snapshot is not None:
        emit("slack_member_channel", **snapshot, outcome=outcome)


@contextmanager
def capture_route():
    snapshot = {}
    token = _route.set(snapshot)
    try:
        yield snapshot
    finally:
        _route.reset(token)


def public_route(row):
    target = _route.get()
    snapshot = room_snapshot(row)
    if target is not None and snapshot is not None:
        target.update(snapshot, route=row.audience)


def event(snapshot, result):
    if snapshot and isinstance(result, dict):
        fields = {**snapshot, "outcome": result.get("status")}
        if type(result.get("receipt_id")) is int:
            fields["receipt_id"] = result["receipt_id"]
        emit("slack_member_event", **fields)


def send(row, actor, delivery_id, outcome, *, reconciled=False):
    snapshot = room_snapshot(row)
    if snapshot is not None and isinstance(actor, dict):
        reason = ("reconciled" if reconciled else "recorded_strategy"
                  if outcome == "settled" else "authority_or_provider_hold")
        emit("slack_member_send", **snapshot, delivery_id=delivery_id,
             poster=actor.get("kind"), outcome=outcome, reason=reason)


def send_failure(row, actor, delivery_id, error):
    # Inspect exception types only. Error messages/provider response data are
    # deliberately absent, including when they contain an unexpected body.
    from .member_delivery import MemberDeliveryDeferred, MemberDeliveryUnavailable
    outcome = ("deferred" if isinstance(error, MemberDeliveryDeferred)
               else "unavailable" if isinstance(error, MemberDeliveryUnavailable) else "retry")
    send(row, actor, delivery_id, outcome)


def echo(row, receipt_id, kind):
    snapshot = room_snapshot(row)
    if snapshot is not None:
        emit("slack_public_echo", **snapshot, receipt_id=receipt_id, kind=kind)


def compare(row, *, kind, outcome, **counts):
    snapshot = room_snapshot(row)
    if snapshot is not None and row.audience == "community":
        emit("slack_community_source compare", **snapshot, kind=kind, outcome=outcome, **counts)
