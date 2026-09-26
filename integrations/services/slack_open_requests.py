"""Bounded owner/device open intents; provider work belongs to discovery."""

from datetime import datetime, timedelta, timezone as dt_timezone
import math
import logging
import time
import uuid

from django.db import transaction
from django.utils import timezone

from integrations.services.message_sync.scheduler import BudgetDeferred, LeaseLost

KEY = "slack_conversation_open_requests_v1"
MAX_REQUESTS = 16
REQUEST_TTL = 900
SOURCE_VALIDATION_TTL = 60
logger = logging.getLogger(__name__)


def _live_requests(cursor, now):
    return {key: value for key, value in (cursor.get(KEY) or {}).items()
            if isinstance(value, dict) and value.get("until", 0) > now}


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


def enqueue_open_locked(grant, connection, authority, device, row):
    """Coalesce opens under the existing authority lock without provider I/O."""
    from integrations.services.slack_owner_inventory import device_epoch, state_for
    from integrations.services.slack_owner_inventory_api import InventoryError
    from integrations.services.message_sync.discovery import KEY as DISCOVERY_KEY

    now = time.time()
    cursor = dict(connection.sync_cursor or {})
    requests = _live_requests(cursor, now)
    key = f"{device.pk}:{row.slack_conversation_id}"
    epoch = device_epoch(grant, authority, device, state=state_for(connection, authority))
    previous = requests.get(key)
    if previous and previous.get("epoch") == epoch:
        if previous.get("error"):
            raise InventoryError(previous["error"], previous["status_code"])
        # Polling must not bypass Retry-After or restart successful provisioning.
        return {"state": "importing", "mlai_channel_id": None,
                "retry_after_seconds": max(2, math.ceil(previous.get("due", now) - now))}
    if key not in requests and len(requests) >= MAX_REQUESTS:
        raise InventoryError("inventory_rate_limited", 429, retry_after_seconds=10)
    requests[key] = {
        "id": uuid.uuid4().hex, "epoch": epoch, "source_id": row.slack_conversation_id,
        "public_key": device.public_key, "requested_at": now,
        "until": now + REQUEST_TTL, "due": now, "state": "pending",
    }
    cursor[KEY] = requests
    discovery = dict(cursor.get(DISCOVERY_KEY) or {})
    # Preserve an active lease and this owner's fairness position.
    discovery["due"] = min(float(discovery.get("due") or 0), now)
    cursor[DISCOVERY_KEY] = discovery
    connection.sync_cursor = cursor
    connection.save(update_fields=["sync_cursor", "updated_at"])
    grant.last_discovery_at = None
    grant.save(update_fields=["last_discovery_at", "updated_at"])
    return {"state": "importing", "mlai_channel_id": None, "retry_after_seconds": 2}


def _update(authority, key, request, **changes):
    from integrations.services import slack_dm_mirror as dm

    with transaction.atomic():
        _, connection = dm._lock_slack_grant_api_authority(authority, required_scopes={"im:read"})
        cursor = dict(connection.sync_cursor or {})
        requests = _live_requests(cursor, time.time())
        current = requests.get(key)
        if current is None or current.get("id") != request["id"]:
            return
        requests[key] = {**current, **changes}
        cursor[KEY] = requests
        connection.sync_cursor = cursor
        connection.save(update_fields=["sync_cursor", "updated_at"])


def _prioritize_history(conversation, authority):
    from integrations.services import slack_dm_mirror as dm
    from integrations.services.slack_chat_refresh import FOREGROUND_STATE_ID

    with transaction.atomic():
        dm._lock_slack_grant_api_authority(authority, required_scopes={"im:read"})
        marker = dm._ensure_history_state(
            conversation, source_message_id=FOREGROUND_STATE_ID,
            metadata={"history_scan_state": "foreground-refresh"},
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
    key, request = min(pending, key=lambda item: item[1]["requested_at"])
    if request.get("due", 0) > now:
        raise BudgetDeferred(request["due"] - now)
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
        started_at = datetime.fromtimestamp(request["requested_at"], tz=dt_timezone.utc)
        with conversation_progress(authority, row.slack_conversation_id, row.kind, started_at) as progress:
            details, activity = _validated_source(current_grant, authority, row, progress)
            conversation = dm._discover_conversation(
                current_grant, authority, details, profile_cache={},
                force_backfill=False, reset_history=False,
                activity_seconds=activity, recent_activity=True,
                required_owner_public_key=device.public_key,
            )
        if conversation is None:
            raise InventoryError("inventory_conversation_unavailable", 409)
        _prioritize_history(conversation, authority)
        dm._drain_staged_events_for_conversation(authority, conversation.pk)
        _update(authority, key, request, state="importing")
    except LeaseLost:
        raise
    except (BudgetDeferred, dm.SlackDmMirrorRateLimited) as exc:
        _update(authority, key, request, due=time.time() + max(1, getattr(exc, "retry_after", 5)))
    except InventoryError as exc:
        if exc.status_code == 429:
            _update(authority, key, request, due=time.time() + max(2, exc.retry_after_seconds or 30))
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
