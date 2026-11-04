"""Bounded owner/device open intents; provider work belongs to discovery."""

from datetime import datetime, timedelta, timezone as dt_timezone
import math
import logging
import time
import uuid

from django.conf import settings
from django.db import transaction
from django.core.cache import cache
from integrations.services.message_sync import telemetry
from django.utils import timezone

from integrations.services.message_sync.scheduler import BudgetDeferred, LeaseLost

KEY = "slack_conversation_open_requests_v1"
MAX_REQUESTS = 16
REQUEST_TTL = 900
TIMEOUT_REPORT_GRACE = 120
MAX_RETAINED_GRACE = 16
SOURCE_VALIDATION_TTL = 60
logger = logging.getLogger(__name__)


def _live_requests(cursor, now):
    return {key: value for key, value in (cursor.get(KEY) or {}).items()
            if isinstance(value, dict) and value.get("until", 0) > now}


def _retained_requests(cursor, now):
    live = _live_requests(cursor, now)
    grace = [(key, value) for key, value in (cursor.get(KEY) or {}).items()
             if isinstance(value, dict) and value.get("state") == "pending"
             and value.get("until", 0) <= now < value.get("until", 0) + TIMEOUT_REPORT_GRACE
             and not value.get("timeout_reported_at")]
    live.update(sorted(grace, key=lambda item: item[1]["until"], reverse=True)[:MAX_RETAINED_GRACE])
    return live


def _mark_polled(grant, request, now):
    try:
        cache.set(f"slack-open-polled:{request['id']}", now, 30)
    except Exception:
        logger.warning("slack_open_poll_mark_failed grant_id=%s", grant.pk)


def _progress(request, live, now):
    return {
        "stage": request.get("stage") or ("importing_history" if request.get("state") == "importing" else "queued"),
        "attempts": int(request.get("attempts") or 0), "request_id": request["id"],
        "requested_at": datetime.fromtimestamp(request["requested_at"], tz=dt_timezone.utc).isoformat(),
        "deadline_at": datetime.fromtimestamp(request["until"], tz=dt_timezone.utc).isoformat(),
        "queue_position": sum(v.get("state") == "pending" and v.get("requested_at", now) < request["requested_at"] for v in live.values()),
        "device_access": "pending" if request.get("device_only") else "none",
    }


def has_pending_opens(cursor):
    """Preserve work queued while a full directory scan was in flight."""
    return any(value.get("state") == "pending"
               for value in _live_requests(cursor or {}, time.time()).values())


def complete_open_locked(connection, device, source_id):
    """Free queue capacity after the current device can open a published room."""
    cursor = dict(connection.sync_cursor or {})
    requests = dict(cursor.get(KEY) or {})
    if requests.pop(f"{device.pk}:{source_id}", None) is None:
        return
    cursor[KEY] = requests
    connection.sync_cursor = cursor
    connection.save(update_fields=["sync_cursor", "updated_at"])


def _schedule_requests_locked(grant, connection, cursor, requests, now):
    from integrations.services.message_sync.discovery import KEY as DISCOVERY_KEY

    cursor[KEY] = requests
    discovery = dict(cursor.get(DISCOVERY_KEY) or {})
    # Preserve an active lease and this owner's fairness position.
    discovery["due"] = min(float(discovery.get("due") or 0), now)
    if getattr(settings, "MESSAGE_SYNC_OPEN_PRIORITY_ENABLED", True):
        discovery["open_requested_at"] = min(float(discovery.get("open_requested_at") or now), now)
    cursor[DISCOVERY_KEY] = discovery
    connection.sync_cursor = cursor
    connection.save(update_fields=["sync_cursor", "updated_at"])
    grant.last_discovery_at = None
    grant.save(update_fields=["last_discovery_at", "updated_at"])


def enqueue_open_locked(grant, connection, authority, device, row, *, device_only=False):
    """Coalesce opens under the existing authority lock without provider I/O."""
    from integrations.services.slack_owner_inventory import device_epoch, state_for
    from integrations.services.slack_owner_inventory_api import InventoryError

    now = time.time()
    cursor = dict(connection.sync_cursor or {})
    requests = _retained_requests(cursor, now)
    live = _live_requests(cursor, now)
    key = f"{device.pk}:{row.slack_conversation_id}"
    epoch = device_epoch(grant, authority, device, state=state_for(connection, authority))
    stale = (cursor.get(KEY) or {}).get(key)
    if (isinstance(stale, dict) and stale.get("epoch") == epoch and stale.get("state") == "pending"
            and stale.get("until", 0) <= now < stale.get("until", 0) + TIMEOUT_REPORT_GRACE
            and not stale.get("timeout_reported_at")):
        requests[key] = {**stale, "timeout_reported_at": now}
        cursor[KEY] = requests
        connection.sync_cursor = cursor
        connection.save(update_fields=["sync_cursor", "updated_at"])
        logger.info("slack_open_timed_out grant_id=%s request_id=%s", grant.pk, stale["id"])
        return {"state": "failed", "error": "open_timed_out"}
    previous = live.get(key)
    if previous and previous.get("epoch") == epoch:
        if previous.get("error"):
            raise InventoryError(previous["error"], previous["status_code"])
        if previous.get("state") == "importing":
            if not previous.get("receipt_recovery_complete"):
                # A pre-upgrade intent already left discovery. Give its cached
                # room one receipt-recovery pass before considering a terminal
                # source limit; retain its ID, device epoch and provider due time.
                requests[key] = {**previous, "state": "pending"}
                _schedule_requests_locked(grant, connection, cursor, requests, now)
            else:
                from integrations.services.slack_chat_catalog import source_limited_without_pending
                if source_limited_without_pending(grant, row.slack_conversation_id):
                    raise InventoryError("inventory_source_limited", 409)
        _mark_polled(grant, previous, now)
        # Polling must not bypass Retry-After or restart successful provisioning.
        return {"state": "importing", "mlai_channel_id": None,
                "retry_after_seconds": max(2, math.ceil(previous.get("due", now) - now)),
                **_progress(previous, live, now)}
    if key not in live and len(live) >= MAX_REQUESTS:
        raise InventoryError("inventory_rate_limited", 429, retry_after_seconds=10)
    requests[key] = {
        "id": uuid.uuid4().hex, "epoch": epoch, "source_id": row.slack_conversation_id,
        "public_key": device.public_key, "requested_at": now,
        "until": now + REQUEST_TTL, "due": now, "state": "pending",
        "stage": "adding_device" if device_only else "queued", "device_only": bool(device_only),
    }
    _schedule_requests_locked(grant, connection, cursor, requests, now)
    _mark_polled(grant, requests[key], now)
    return {"state": "importing", "mlai_channel_id": None, "retry_after_seconds": 2, **_progress(requests[key], live, now)}


def _update(authority, key, request, **changes):
    from integrations.services import slack_dm_mirror as dm

    with transaction.atomic():
        _, connection = dm._lock_slack_grant_api_authority(authority, required_scopes={"im:read"})
        cursor = dict(connection.sync_cursor or {})
        now = time.time()
        requests = _retained_requests(cursor, now)
        current = requests.get(key)
        if current is None or current.get("until", 0) <= now or current.get("id") != request["id"]:
            return
        requests[key] = {**current, **changes}
        cursor[KEY] = requests
        connection.sync_cursor = cursor
        connection.save(update_fields=["sync_cursor", "updated_at"])


def _prioritize_history(conversation, authority, request):
    from integrations.services import slack_dm_mirror as dm
    from integrations.services.slack_chat_refresh import FOREGROUND_STATE_ID

    with transaction.atomic():
        dm._lock_slack_grant_api_authority(authority, required_scopes={"im:read"})
        marker = dm._ensure_history_state(
            conversation, source_message_id=FOREGROUND_STATE_ID,
            metadata={"history_scan_state": "foreground-refresh",
                      "open_request_id": request["id"], "open_requested_at": request["requested_at"]},
        )
        marker.available_at = timezone.now() + timedelta(minutes=5)
        marker.save(update_fields=["available_at", "updated_at"])


def _retry_failure(authority, key, request, code):
    attempts = int(request.get("attempts", 0)) + 1
    logger.warning("slack_open_retry grant_id=%s error=%s attempt=%s",
                   authority.grant_id, code, attempts)
    if attempts >= 3:
        _update(authority, key, request, state="error", error="slack_upstream_unavailable",
                status_code=502, until=time.time() + 60, attempts=attempts)
    else:
        _update(authority, key, request, due=time.time() + 30, attempts=attempts)


def _validated_source(grant, authority, row, progress):
    """Resume a recent source check across nested membership/profile deferrals.

    The existing directory checkpoint binds this to one request, OAuth/consent
    generation, source, history window and current room membership. Persist only
    bounded conversation metadata: Slack's info response can contain messages.
    """
    from integrations.services.slack_owner_inventory_api import _validate_open_source

    now = time.time()
    cached = progress.value.get("open_source") or {}
    if (isinstance(cached, dict)
            and isinstance(cached.get("checked_at"), (int, float))
            and 0 <= now - cached["checked_at"] < SOURCE_VALIDATION_TTL
            and cached.get("membership_fence") == progress.membership_fence
            and isinstance(cached.get("details"), dict)
            and cached["details"].get("id") == row.slack_conversation_id
            and isinstance(cached.get("activity"), int)):
        return dict(cached["details"]), cached["activity"]
    details, activity = _validate_open_source(grant, authority, row)
    safe_details = {
        key: str(details[key])[:limit]
        for key, limit in (("id", 100), ("user", 100), ("name", 255))
        if key in details
    }
    safe_details.update({
        key: details[key] for key in (
            "is_im", "is_mpim", "is_private", "is_member", "is_open", "is_archived",
        ) if isinstance(details.get(key), bool)
    })
    progress.value["open_source"] = {
        "details": safe_details, "activity": activity,
        "checked_at": time.time(), "membership_fence": progress.membership_fence,
    }
    progress.save()
    return safe_details, activity


def process_next_open(grant, authority):
    """Process one requested source before full-directory work, preserving quotas.

    A source is never authorized by the hint: current consent, device, epoch,
    source membership and import window are checked before provisioning. Every
    provider call still uses the shared budget and discovery lease.
    """
    from integrations.services import slack_dm_mirror as dm
    from integrations.services.slack_discovery_progress import conversation_progress
    from integrations.services.slack_owner_inventory import device_epoch
    from integrations.services.slack_owner_inventory_api import (
        InventoryError, _authorized,
    )

    now = time.time()
    requests = _live_requests(grant.connection.sync_cursor or {}, now)
    pending = [(key, value) for key, value in requests.items()
               if value.get("state") == "pending"]
    if not pending:
        return False
    # The directory metadata checkpoint is one resumable source per owner.
    # Keep this short foreground attempt until it completes or expires, so
    # background pages cannot discard successful membership/profile stages.
    try:
        polled = cache.get_many([f"slack-open-polled:{v['id']}" for _, v in pending])
    except Exception:
        polled = {}
    due = [i for i in pending if i[1].get("due", 0) <= now]
    in_flight = [i for i in pending if i[1].get("stage") in {"validating", "provisioning"}]
    recent = [i for i in due if now - float(polled.get(f"slack-open-polled:{i[1]['id']}") or 0) <= 15]
    key, request = min(in_flight or recent or due or pending, key=lambda i: i[1]["requested_at"])
    if request.get("due", 0) > now:
        deferred = BudgetDeferred(request["due"] - now)
        if getattr(settings, "MESSAGE_SYNC_OPEN_PRIORITY_ENABLED", True):
            deferred.discovery_admission_deferred = True
        raise deferred
    try:
        current_grant, current_authority, device, state = _authorized(grant.user, request["public_key"])
        if current_authority != authority or request["epoch"] != device_epoch(
            current_grant, current_authority, device, state=state,
        ):
            raise InventoryError("slack_authority_changed", 403)
        row = current_grant.owner_conversation_inventory.filter(
            slack_conversation_id=request["source_id"], eligibility="eligible",
        ).first()
        if row is None or row.kind == "public_channel":
            raise InventoryError("inventory_conversation_unavailable", 404)
        from integrations.services.message_sync.device_recovery import (
            fanout_batch, device_only_row_eligible, _fanout_failure, _propagating_recovery_error,
        )
        if request.get("device_only") and fanout_batch() and row.kind in dm.DEVICE_ONLY_AUDIENCE_KINDS:
            mirror = current_grant.conversations.filter(slack_conversation_id=row.slack_conversation_id,
                status="live", mlai_channel_id__isnull=False).first()
            if mirror is not None and device_only_row_eligible(current_grant, row):
                mirror.grant = current_grant
                try:
                    _update(authority, key, request, stage="adding_device")
                    dm._provision_owner_conversation(mirror, required_owner_public_key=device.public_key,
                                                     device_only_statuses=frozenset({"live"}))
                except dm.SlackDmDeviceOnlyIneligible:
                    pass
                except Exception as exc:
                    if not _propagating_recovery_error(exc):
                        _fanout_failure(current_grant, authority, mirror, device.public_key, exc)
                    raise
                else:
                    _update(authority, key, request, state="importing", stage="importing_history")
                    log_open_latency("open_provisioned", request, authority.grant_id)
                    return True
        from integrations.services.message_sync.presentation import recover_presentation

        if not request.get("receipt_recovery_complete"):
            recovered, before_id = recover_presentation(
                current_grant, authority, device, row.slack_conversation_id,
                before_id=request.get("receipt_before_id"),
            )
            if recovered:
                _update(authority, key, request, state="importing", stage="importing_history")
                return True  # The next metadata-only open resolves the ready room.
            if before_id is not None:
                _update(authority, key, request, receipt_before_id=before_id, due=time.time() + 2)
                return True
            _update(authority, key, request, receipt_recovery_complete=True)
        from integrations.services.slack_chat_catalog import source_limited_without_pending
        if source_limited_without_pending(current_grant, row.slack_conversation_id):
            raise InventoryError("inventory_source_limited", 409)
        started_at = datetime.fromtimestamp(request["requested_at"], tz=dt_timezone.utc)
        with conversation_progress(authority, row.slack_conversation_id, row.kind, started_at) as progress:
            _update(authority, key, request, stage="validating")
            details, activity = _validated_source(current_grant, authority, row, progress)
            _update(authority, key, request, stage="provisioning")
            conversation = dm._discover_conversation(
                current_grant, authority, details, profile_cache={},
                force_backfill=False, reset_history=False,
                activity_seconds=activity, recent_activity=True,
                required_owner_public_key=device.public_key,
            )
        if conversation is None:
            raise InventoryError("inventory_conversation_unavailable", 409)
        _prioritize_history(conversation, authority, request)
        dm._drain_staged_events_for_conversation(authority, conversation.pk)
        _update(authority, key, request, state="importing", stage="importing_history")
        log_open_latency("open_provisioned", request, authority.grant_id)
    except LeaseLost:
        raise
    except (BudgetDeferred, dm.SlackDmMirrorRateLimited) as exc:
        _update(authority, key, request, due=time.time() + max(1, getattr(exc, "retry_after", 5)))
        if (getattr(settings, "MESSAGE_SYNC_OPEN_PRIORITY_ENABLED", True)
                and isinstance(exc, BudgetDeferred) and exc.before_request_method):
            exc.discovery_admission_deferred = True
            raise
    except InventoryError as exc:
        if exc.status_code == 429:
            _update(authority, key, request, due=time.time() + max(2, exc.retry_after_seconds or 30))
            if (getattr(settings, "MESSAGE_SYNC_OPEN_PRIORITY_ENABLED", True)
                    and isinstance(exc.__cause__, BudgetDeferred) and exc.__cause__.before_request_method):
                exc.__cause__.discovery_admission_deferred = True
                raise exc.__cause__
        elif exc.status_code >= 500:
            _retry_failure(authority, key, request, exc.code)
        else:
            _update(authority, key, request, state="error", error=exc.code,
                    status_code=exc.status_code, until=time.time() + 60)
    except dm.SlackDmMirrorAuthorizationError:
        raise
    except Exception as exc:
        # Retain durable metadata progress on transport/adapter failures. No
        # exception text or source payload enters the client-visible queue.
        _retry_failure(authority, key, request, type(exc).__name__)
    return True


def log_open_latency(kind, request, grant_id, *, now=None):
    """Log only opaque request/grant IDs and timing for a live open intent."""
    if not isinstance(request, dict) or not isinstance(request.get("id"), str):
        return
    requested_at = request.get("requested_at")
    if type(requested_at) not in (int, float) or not math.isfinite(requested_at):
        return
    elapsed = max(0, round(((time.time() if now is None else now) - requested_at) * 1000))
    telemetry.record_latency(kind, elapsed)
    logger.info("slack_%s grant_id=%s request_id=%s latency_ms=%s", kind, grant_id, request["id"], elapsed)


PREFETCH_KEY = "slack_unread_prefetch_v1"
PREFETCH_MAX_ENTRIES = 256


def _prefetch_state(cursor, now):
    """Keep only bounded timestamp/opaque-ID quota and retry scheduling data."""
    raw = (cursor or {}).get(PREFETCH_KEY) or {}
    if not isinstance(raw, dict):
        raw = {}
    hourly = [item for item in raw.get("hourly", []) if isinstance(item, dict)
              and isinstance(item.get("id"), str)
              and type(item.get("at")) in (int, float) and math.isfinite(item["at"])
              and now - 3600 < item["at"] <= now]
    sources = {key: value for key, value in (raw.get("sources") or {}).items()
               if isinstance(key, str) and type(value) in (int, float)
               and math.isfinite(value) and value > now}
    return {"hourly": hourly[-PREFETCH_MAX_ENTRIES:],
            "sources": dict(sorted(sources.items(), key=lambda item: item[1], reverse=True)[:PREFETCH_MAX_ENTRIES])}


def _save_prefetch(authority, state):
    from integrations.services import slack_dm_mirror as dm
    with transaction.atomic():
        _, connection = dm._lock_slack_grant_api_authority(authority, required_scopes={"im:read"})
        connection.sync_cursor = {**(connection.sync_cursor or {}), PREFETCH_KEY: _prefetch_state({PREFETCH_KEY: state}, time.time())}
        connection.save(update_fields=["sync_cursor", "updated_at"])


def prefetch_unread_source(grant, authority, *, profile_cache, cycle_started_at):
    """Pre-create at most one consented unread room per background discovery turn."""
    from django.core.cache import cache
    from integrations.services import slack_dm_mirror as dm, slack_owner_inventory as inventory
    from integrations.services.slack_chat_read_state import _cache_key
    from integrations.services.slack_discovery_progress import conversation_progress
    from integrations.services.slack_owner_inventory_api import InventoryError
    from integrations.services.message_sync.request_priority import request_priority

    if (not getattr(settings, "MESSAGE_SYNC_UNREAD_PREFETCH_ENABLED", False)
            or not inventory.enabled() or not inventory.has_metadata_consent(grant.connection, authority)):
        return False
    now = time.time()
    state = _prefetch_state(grant.connection.sync_cursor, now)
    cap = max(0, min(PREFETCH_MAX_ENTRIES, int(getattr(settings, "MESSAGE_SYNC_UNREAD_PREFETCH_PER_HOUR", 6))))
    if len(state["hourly"]) >= cap:
        return False
    kinds = ["im", "mpim"]
    if dm.private_channels_enabled(grant):
        kinds.append("private_channel")
    days = dm._grant_history_days(grant)
    floor = now - days * 86400 if days else 0
    rows = grant.owner_conversation_inventory.filter(kind__in=kinds, eligibility="eligible").exclude(
        source_is_open=False,
    ).exclude(slack_conversation_id__in=grant.conversations.values("slack_conversation_id"))
    if days:
        rows = rows.exclude(source_archived=True).filter(source_activity_ts__gte=str(int(floor)))
    rows = rows.order_by("-source_activity_ts", "id")[:50]
    candidates = []
    for row in rows:
        activity = dm._slack_conversation_activity_seconds({"latest": row.source_activity_ts})
        if (row.kind not in kinds or row.eligibility != "eligible" or row.source_is_open is False
                or activity is None or activity < floor or (days and row.source_archived is True)
                or state["sources"].get(row.slack_conversation_id, 0) > now):
            continue
        candidates.append(row)
    if not candidates:
        return False
    targets = {target.slack_id: target for target in inventory.source_read_targets(grant, authority, [])}
    keys = {row.slack_conversation_id: _cache_key(authority, targets[row.slack_conversation_id])
            for row in candidates if row.slack_conversation_id in targets}
    snapshots = cache.get_many(list(keys.values()))
    row = next((row for row in candidates if (
        (snapshots.get(keys.get(row.slack_conversation_id)) or {}).get("available") is True
        and (snapshots.get(keys.get(row.slack_conversation_id)) or {}).get("is_unread") is True)), None)
    if row is None:
        return False
    reservation = {"id": uuid.uuid4().hex, "at": now}
    state["hourly"].append(reservation)
    _save_prefetch(authority, state)
    created = False
    try:
        with request_priority("background"), conversation_progress(authority, row.slack_conversation_id, row.kind, cycle_started_at) as progress:
            details, activity = _validated_source(grant, authority, row, progress)
            # Directory metadata can predate an archive operation. Recheck the
            # fresh source result before spending a room-creation reservation.
            if days and details.get("is_archived") is True:
                raise InventoryError("inventory_history_consent_required", 409)
            conversation = dm._discover_conversation(
                grant, authority, details, profile_cache=profile_cache,
                force_backfill=False, reset_history=False, activity_seconds=activity, recent_activity=True,
            )
            if conversation is None:
                raise InventoryError("inventory_conversation_unavailable", 409)
            created = True
            dm._drain_staged_events_for_conversation(authority, conversation.pk)
        state["sources"][row.slack_conversation_id] = time.time() + 3600
    except BudgetDeferred:
        if not created:
            state["hourly"] = [item for item in state["hourly"] if item["id"] != reservation["id"]]
            _save_prefetch(authority, state)
        raise
    except InventoryError as exc:
        if not created:
            state["hourly"] = [item for item in state["hourly"] if item["id"] != reservation["id"]]
        if isinstance(exc.__cause__, BudgetDeferred):
            _save_prefetch(authority, state)
            raise exc.__cause__
        state["sources"][row.slack_conversation_id] = time.time() + (300 if exc.status_code >= 500 else 3600)
        _save_prefetch(authority, state)
        return True
    _save_prefetch(authority, state)
    return True
