"""Event-first private routing using existing ciphertext and owner leases.

The checkpoint contains only source IDs, opaque event generations and scheduling
metadata. The encrypted pending-event list remains the sole source of bodies.
"""
import logging
import math
import time
import uuid
from dataclasses import replace

from django.conf import settings
from django.db import transaction
from django.db.models import F, FloatField, Max, Q
from django.db.models.functions import Cast

from integrations.models import SlackDmMirrorGrant
from . import telemetry
from .scheduler import BudgetDeferred, LeaseLost

KEY = "slack_event_routing_v1"
DELIVERY_KEY = "event_routing_v1"
ACK_KEY = "event_adapter_confirmed_at"
MAX_CANDIDATES = 128
logger = logging.getLogger(__name__)


def enabled():
    """The new lane deliberately does not require the older targeted flag."""
    return bool(getattr(settings, "MESSAGE_SYNC_ENABLED", False)
                and getattr(settings, "MESSAGE_SYNC_EVENT_ROUTING", False))


def _number(value, default=0):
    return float(value) if type(value) in (int, float) and math.isfinite(value) else default


def _state(cursor):
    value = cursor.get(KEY) if isinstance(cursor, dict) else None
    return dict(value) if isinstance(value, dict) else {}


def prune_drained_targets(cursor):
    """Drop completed source scheduling only, retaining real pending cooldowns.

    Run before appending a new event as well as after draining/discarding. This
    covers older sweep paths that remove ciphertext without an event settle.
    Neither the owner lease nor shared provider budgets are changed here.
    """
    from . import staged_routing
    parked = staged_routing.parked_channels(cursor)
    retry = cursor.get(staged_routing.KEY)
    if isinstance(retry, dict) and isinstance(retry.get("channels"), dict):
        channels = {key: value for key, value in retry["channels"].items() if key in parked}
        if channels:
            cursor[staged_routing.KEY] = {**retry, "channels": channels}
        else:
            cursor.pop(staged_routing.KEY, None)
    if KEY not in cursor:
        return
    state = _state(cursor)
    raw = state.get("targets")
    targets = {key: value for key, value in raw.items() if key in parked} if isinstance(raw, dict) else {}
    cursor[KEY] = {**state, "targets": targets}
    cursor[KEY]["next_due"] = _next_due(cursor)


def enqueue_locked(grant, connection, cursor, channel_id, event_key, staged_at):
    """Atomically add a wake beside ciphertext under the caller's authority lock.

    A new arrival changes generation, never an existing cooldown or owner lease.
    The caller saves this cursor in the same transaction as the encrypted event.
    """
    if not enabled():
        return False
    from .staged_routing import parked_channels, staged_seconds
    state = _state(cursor)
    old = state.get("targets")
    targets = dict(old) if isinstance(old, dict) else {}
    parked = parked_channels(cursor)
    targets = {key: value for key, value in targets.items() if key in parked and isinstance(value, dict)}
    previous = targets.get(channel_id) or {}
    targets[channel_id] = {
        "generation": event_key, "due": _number(previous.get("due")),
        "first": _number(previous.get("first"), staged_seconds(staged_at) or time.time()),
        "attempts": min(1000, max(0, int(_number(previous.get("attempts"))))),
    }
    cursor[KEY] = {**state, "targets": targets}
    cursor[KEY]["next_due"] = _next_due(cursor)
    return True


def _due_targets(cursor, now):
    """Recover missing wake metadata directly from durable parked identities."""
    from integrations.services.slack_dm_mirror import PENDING_EVENT_CHECKPOINT_KEY
    from .staged_routing import parked_channels, _retry_state
    parked, retry, state = parked_channels(cursor), _retry_state(cursor), _state(cursor)
    raw = state.get("targets")
    targets = raw if isinstance(raw, dict) else {}
    pending = cursor.get(PENDING_EVENT_CHECKPOINT_KEY, []) if isinstance(cursor, dict) else []
    generations = {item.get("channel_id"): item.get("key") for item in pending
                   if isinstance(item, dict) and isinstance(item.get("channel_id"), str)
                   and isinstance(item.get("key"), str)} if isinstance(pending, list) else {}
    due = []
    for channel, summary in parked.items():
        target = targets.get(channel)
        target = target if isinstance(target, dict) else {}
        if max(_number(target.get("due")), retry.get(channel, {}).get("due", 0)) <= now:
            due.append((channel, str(target.get("generation") or generations.get(channel) or ""), summary))
    return sorted(due, key=lambda item: (item[2]["staged_at"] or now, item[0]))


def _next_due(cursor):
    from .staged_routing import parked_channels, _retry_state
    parked, retry = parked_channels(cursor), _retry_state(cursor)
    raw = _state(cursor).get("targets")
    targets = raw if isinstance(raw, dict) else {}
    return min((max(_number((targets[channel] if isinstance(targets.get(channel), dict) else {}).get("due")), retry.get(channel, {}).get("due", 0))
                for channel in parked), default=time.time() + 300)


def claim_event_routing(interval_seconds, *, lease_seconds=120):
    """Claim one due source with the same User/grants/connection owner lease.

    Unlike a directory turn, this ignores last_discovery_at. Existing owner and
    per-source retry deadlines remain authoritative. Selection is bounded and
    rotates workspaces and owners using the existing served clock.
    """
    if not enabled():
        return None
    from . import discovery
    from django.utils import timezone
    now = timezone.now().timestamp()
    active = SlackDmMirrorGrant.objects.filter(
        status="active", revoked_at__isnull=True, user__is_active=True,
        connection__isnull=False,
    )
    from integrations.services.slack_dm_mirror import PENDING_EVENT_CHECKPOINT_KEY
    active = active.filter(**{f"connection__sync_cursor__{PENDING_EVENT_CHECKPOINT_KEY}__isnull": False})
    for path in (f"connection__sync_cursor__{discovery.KEY}__due",
                 f"connection__sync_cursor__{discovery.KEY}__expires",
                 f"connection__sync_cursor__{KEY}__next_due"):
        active = active.filter(Q(**{f"{path}__isnull": True}) | Q(**{f"{path}__lte": now}))
    served = f"connection__sync_cursor__{discovery.KEY}__served"
    workspaces = list(active.values("slack_workspace_id").annotate(
        served=Max(Cast(served, FloatField())),
    ).order_by(F("served").asc(nulls_first=True), "slack_workspace_id")[:MAX_CANDIDATES])
    checked = 0
    for workspace in workspaces:
        candidates = list(active.filter(slack_workspace_id=workspace["slack_workspace_id"]).order_by(
            F(served).asc(nulls_first=True), "id",
        ).values_list("pk", "connection_id", "user_id")[:MAX_CANDIDATES - checked])
        for grant_id, connection_id, user_id in candidates:
            checked += 1
            lease = discovery.DiscoveryLease(grant_id, connection_id, user_id, uuid.uuid4().hex)
            with transaction.atomic():
                grant, connection = discovery._lock(lease, skip_locked=True)
                if connection is None or not enabled():
                    continue
                cursor = dict(connection.sync_cursor or {})
                previous = cursor.get(discovery.KEY) or {}
                if not isinstance(previous, dict):
                    continue  # Unknown lease state is never permission to steal it.
                if max(_number(previous.get("due")), _number(previous.get("expires"))) > now:
                    continue
                due = _due_targets(cursor, now)
                if not due:
                    state = _state(cursor)
                    cursor[KEY] = {**state, "next_due": _next_due(cursor)}
                    connection.sync_cursor = cursor
                    connection.save(update_fields=["sync_cursor", "updated_at"])
                    continue
                channel_id, generation, _ = due[0]
                lease = replace(lease, previous_served=previous.get("served"),
                                event_channel_id=channel_id, event_generation=generation)
                cursor[discovery.KEY] = {
                    **previous, "token": lease.token, "expires": now + lease_seconds,
                    "served": now, "due": now, "event_channel": channel_id,
                }
                connection.sync_cursor = cursor
                connection.save(update_fields=["sync_cursor", "updated_at"])
                return lease
        if checked >= MAX_CANDIDATES:
            break
    return None


def _settle_event(authority, lease, outcome, *, delay=None):
    from integrations.services import slack_dm_mirror as dm
    from .staged_routing import parked_channels, _retry_state
    with transaction.atomic():
        _, connection = dm._lock_slack_grant_api_authority(authority, required_scopes=dm.DIRECT_DM_SCOPES)
        cursor = dict(connection.sync_cursor or {})
        state = _state(cursor)
        raw = state.get("targets")
        targets = dict(raw) if isinstance(raw, dict) else {}
        parked = parked_channels(cursor)
        target = targets.get(lease.event_channel_id)
        if lease.event_channel_id not in parked:
            targets.pop(lease.event_channel_id, None)
        elif not isinstance(target, dict) or target.get("generation") == lease.event_generation:
            previous = target if isinstance(target, dict) else {}
            retry_due = _retry_state(cursor).get(lease.event_channel_id, {}).get("due", 0)
            targets[lease.event_channel_id] = {
                "generation": lease.event_generation,
                "first": _number(previous.get("first"), parked[lease.event_channel_id]["staged_at"] or time.time()),
                "due": max(retry_due, time.time() + max(1, delay or 1)),
                "attempts": min(1000, int(_number(previous.get("attempts"))) + 1),
            }
        # A new generation keeps its own wake. Retried provider deadlines still
        # apply to the source, so a new callback cannot reset a cooling target.
        counts = state.get("counts")
        counts = dict(counts) if isinstance(counts, dict) else {}
        counts[outcome] = max(0, int(_number(counts.get(outcome)))) + 1
        cursor[KEY] = {"targets": targets, "counts": counts}
        cursor[KEY]["next_due"] = _next_due(cursor)
        connection.sync_cursor = cursor
        connection.save(update_fields=["sync_cursor", "updated_at"])
        # Counts are target attempts, not delivery samples. Keep this distinct
        # from confirmed-delivery timing, including when later revocation erases
        # the private cursor and its diagnostic counters.
        transaction.on_commit(lambda: logger.info("slack_staged_event outcome=%s", outcome), robust=True)


def route_event_claim(grant, authority, lease, *, reset_history=False):
    """Route only the claimed source through all existing privacy checks."""
    from integrations.services import slack_dm_mirror as dm
    from . import discovery, staged_routing
    if not enabled():
        raise LeaseLost("event_routing_disabled")
    with transaction.atomic():
        _, connection = dm._lock_slack_grant_api_authority(authority, required_scopes=dm.DIRECT_DM_SCOPES)
        discovery.guard_discovery(grant, connection)
        parked = staged_routing.parked_channels(connection.sync_cursor or {})
    if lease.event_channel_id not in parked:
        _settle_event(authority, lease, "drained")
        return
    try:
        outcome = staged_routing._route_channel(
            grant, authority, lease.event_channel_id, parked[lease.event_channel_id],
            reset_history=reset_history, profiles=lambda: dm._stored_profile_cache(grant), path="event",
        )
    except LeaseLost:
        raise
    except Exception as exc:
        if dm._is_slack_auth_error(exc) or isinstance(exc, dm.SlackDmMirrorAuthorizationError):
            logger.info("slack_staged_event outcome=authority_denied")
            raise
        delay = dm._slack_retry_after_seconds(exc)
        deferred = isinstance(exc, (BudgetDeferred, dm.SlackDmMirrorRateLimited)) or bool(delay)
        staged_routing._settle_quietly(authority, lease.event_channel_id, delay=(delay or 30) if deferred else "backoff")
        _settle_event(authority, lease, "deferred" if deferred else "retry", delay=delay or 30)
        if isinstance(exc, BudgetDeferred) and exc.before_request_method == "conversations.info":
            exc.discovery_admission_deferred = True
        raise
    _settle_event(authority, lease, outcome)


def preserve_delivery_evidence(previous, incoming):
    """Retain server-owned evidence, never accept it from source metadata."""
    previous = previous if isinstance(previous, dict) else {}
    result = {key: value for key, value in incoming.items() if key not in {DELIVERY_KEY, ACK_KEY}}
    if isinstance(previous.get(DELIVERY_KEY), dict):
        result[DELIVERY_KEY] = dict(previous[DELIVERY_KEY])
    if _number(previous.get(ACK_KEY), None) is not None:
        result[ACK_KEY] = previous[ACK_KEY]
    return result


def record_delivery_completed_locked(delivery, *, now=None, adapter_confirmed=False):
    """Persist one adapter-confirmed sample; emit only after its transaction commits."""
    metadata = dict(delivery.metadata or {})
    marker = metadata.get(DELIVERY_KEY)
    if (delivery.status != "completed" or not delivery.completed_at
            or metadata.get("undeliverable") or metadata.get("history_outside_window")
            or metadata.get("mutation_superseded") or metadata.get("dependency_superseded")
            or metadata.get("history_recovery_superseded")):
        return False
    if adapter_confirmed and (enabled() or isinstance(marker, dict)):
        # History may win before drain attaches its parked marker. Capture its
        # actual confirmed I/O time anyway, for every operation (not just creates).
        # A genuine new ack after revival replaces the old ack, not the original
        # parked timestamp/path or an already recorded completion sample.
        metadata[ACK_KEY] = delivery.completed_at.timestamp()
        delivery.metadata = metadata
        delivery.save(update_fields=["metadata", "updated_at"])
    if not isinstance(marker, dict) or marker.get("completed_at") is not None:
        return False
    parked = _number(marker.get("parked_at"), None)
    completed = _number(metadata.get(ACK_KEY), None)
    if (parked is None or completed is None or completed != delivery.completed_at.timestamp()
            or not isinstance(marker.get("key"), str)
            or marker.get("path") not in {"event", "targeted", "discovery"}):
        return False
    if completed < parked:
        # Cross-worker clock uncertainty must not become a fabricated fast
        # success. Status retains the unmeasured completion in its no-op count.
        return False
    elapsed = round((completed - parked) * 1000)
    metadata[DELIVERY_KEY] = {**marker, "completed_at": completed, "latency_ms": elapsed}
    delivery.metadata = metadata
    delivery.save(update_fields=["metadata", "updated_at"])
    path = marker["path"]
    def report():
        telemetry.record_latency("staged_delivered", elapsed)
        telemetry.record_latency(f"staged_delivered_{path}", elapsed)
        logger.info("slack_staged_delivered path=%s events=1 latency_ms=%s", path, elapsed)
    transaction.on_commit(report, robust=True)
    return True


def summarize(cursors, *, now):
    """Count targets and outcomes without returning any private identifiers."""
    from .staged_routing import parked_channels
    result = {"enabled": enabled(), "pending_targets": 0, "pending_events": 0,
              "oldest_pending_age_seconds": 0, "deferred": 0, "retry": 0, "terminal": 0,
              "leased": 0, "expired_leases": 0}
    for cursor in cursors:
        parked = parked_channels(cursor)
        result["pending_targets"] += len(parked)
        result["pending_events"] += sum(value["events"] for value in parked.values())
        for value in parked.values():
            if value["staged_at"] is not None:
                result["oldest_pending_age_seconds"] = max(result["oldest_pending_age_seconds"], max(0, int(now - value["staged_at"])))
        counts = _state(cursor).get("counts")
        counts = counts if isinstance(counts, dict) else {}
        for name in ("deferred", "retry"):
            result[name] += max(0, int(_number(counts.get(name))))
        result["terminal"] += sum(max(0, int(_number(counts.get(name)))) for name in (
            "unauthorized", "unavailable", "archived", "slack_connect", "ineligible",
        ))
        from .discovery import KEY as DISCOVERY_KEY
        lease = cursor.get(DISCOVERY_KEY) if isinstance(cursor, dict) else None
        if isinstance(lease, dict) and lease.get("event_channel") and lease.get("token"):
            result["leased"] += int(_number(lease.get("expires")) > now)
            result["expired_leases"] += int(_number(lease.get("expires")) <= now)
    return result


def summarize_deliveries(rows, *, now, limit=5000, window_seconds=300, completed_only=False):
    """Bounded durable proof; pending/terminal work remains visible beside samples.

    Rows are (internal timing marker, delivery status). No body, author, source,
    device key or token is loaded by the status caller. A 15-second success
    proportion is a threshold bound, never an invented exact percentile.
    """
    paths = {name: {"confirmed": 0, "under_15s": 0, "at_least_15s": 0, "pending": 0,
                    "terminal": 0, "noop_completed": 0, "oldest_pending_age_seconds": 0}
             for name in ("event", "targeted", "discovery")}
    truncated = False
    for index, (marker, status) in enumerate(rows):
        if index >= limit:
            truncated = True
            break
        if not isinstance(marker, dict) or marker.get("path") not in paths:
            continue
        result = paths[marker["path"]]
        confirmed = _number(marker.get("completed_at"), None)
        latency = _number(marker.get("latency_ms"), None)
        if status == "completed" and confirmed is not None and latency is not None:
            if now - window_seconds <= confirmed <= now:
                result["confirmed"] += 1
                result["under_15s"] += int(latency < 15000)
                result["at_least_15s"] += int(latency >= 15000)
        elif status == "completed":
            result["noop_completed"] += 1
        elif status in {"dead", "failed"}:
            result["terminal"] += 1
        else:
            result["pending"] += 1
            parked = _number(marker.get("parked_at"), now)
            result["oldest_pending_age_seconds"] = max(result["oldest_pending_age_seconds"], max(0, int(now - parked)))
    if completed_only:
        # The command selected confirmations only. Never turn absent pending
        # rows into false per-path zeroes; its all-outstanding aggregate is
        # reported separately from this recent confirmation sample.
        for result in paths.values():
            for field in ("pending", "terminal", "noop_completed", "oldest_pending_age_seconds"):
                result.pop(field)
    return {"by_path": paths, "truncated": truncated, "window_seconds": window_seconds,
            "sample_scope": "completed_confirmations_in_window" if completed_only else "supplied_rows",
            "reliability": "durable bounded delivery metadata; inspect pending, terminal and truncation before a latency claim"}
