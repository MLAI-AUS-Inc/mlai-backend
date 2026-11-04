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
# Closed hold codes a deferred send may report; they mirror the held row's
# `last_error`. Unknown codes collapse to the generic hold reason.
HOLD_REASONS = frozenset({
    "provider_budget_deferred", "member_delivery_deferred", "legacy_community_send_unproven",
    "community_bot_reader_unavailable", "community_bot_reader_scope_unavailable",
    "community_channel_not_admitted", "community_mapping_pending",
    "community_member_access_disabled", "community_receipt_unverified",
    "community_source_closed", "parent_mapping_pending", "member_membership_unproven",
})
# Closed codes for permanent drops (row becomes dead without a provider call).
DROP_REASONS = frozenset({"consent_required", "device_unverified", "account_unverified", "permanent_failure"})
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
        "outcome": {"settled", "deferred", "retry", "unavailable", "dropped"},
        "reason": {"recorded_strategy", "reconciled", "authority_or_provider_hold"} | HOLD_REASONS | DROP_REASONS,
    },
    "slack_public_echo": {"kind": {"create", "edit", "delete", "reaction_add", "reaction_remove"}},

}
_NUMBERS = frozenset({"mapping_id", "delivery_id", "receipt_id"})
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


def send(row, actor, delivery_id, outcome, *, reconciled=False, reason=None):
    """`actor` is None when selection failed before a poster was chosen."""
    snapshot = room_snapshot(row)
    if snapshot is not None and (actor is None or isinstance(actor, dict)):
        if reason is None:
            reason = ("reconciled" if reconciled else "recorded_strategy"
                      if outcome == "settled" else "authority_or_provider_hold")
        fields = {**snapshot, "delivery_id": delivery_id, "outcome": outcome, "reason": reason}
        if actor is not None:
            fields["poster"] = actor.get("kind")
        emit("slack_member_send", **fields)


def failure_outcome(error):
    """Map a send exception to a closed (outcome, reason) by type and code only.

    Error messages and provider response data are deliberately never read,
    including when they contain an unexpected body.
    """
    from integrations.services.message_sync.scheduler import BudgetDeferred
    from .ai_consent import PublicBridgeConsentRequired
    from .member_delivery import MemberDeliveryUnavailable
    from .worker import ParentMappingPending

    if isinstance(error, BudgetDeferred):
        reason = getattr(error, "reason", None)
        return "deferred", reason if reason in HOLD_REASONS else "authority_or_provider_hold"
    if isinstance(error, ParentMappingPending):
        return "deferred", "parent_mapping_pending"
    if isinstance(error, MemberDeliveryUnavailable):
        return "unavailable", "authority_or_provider_hold"
    if isinstance(error, PublicBridgeConsentRequired):
        reason = getattr(error, "reason", None)
        return "dropped", reason if reason in DROP_REASONS else "consent_required"
    if getattr(error, "permanent", False) is True:
        return "dropped", "permanent_failure"
    return "retry", "authority_or_provider_hold"


def send_failure(row, actor, delivery_id, error):
    try:
        outcome, reason = failure_outcome(error)
    except Exception:
        return
    send(row, actor, delivery_id, outcome, reason=reason)


def echo(row, receipt_id, kind):
    snapshot = room_snapshot(row)
    if snapshot is not None:
        emit("slack_public_echo", **snapshot, receipt_id=receipt_id, kind=kind)
