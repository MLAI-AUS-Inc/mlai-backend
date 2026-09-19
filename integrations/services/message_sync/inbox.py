"""Acknowledge Slack only after durable encrypted receipt; process asynchronously."""

import hashlib
import json
import uuid
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from integrations.models import BridgeSyncInbox
from .scheduler import BudgetDeferred, LeaseLost, safe_error_code
from .authorizations import expand_authorization_page


def enabled():
    return bool(getattr(settings, "MESSAGE_SYNC_ENABLED", False))


def enqueue_slack_callback(payload):
    """Persist verified callback once; signature validation remains at the view."""
    payload = {key: value for key, value in payload.items() if not key.startswith("_sync_")}
    app_id = str(payload.get("api_app_id") or "").strip()
    workspace_id = str(payload.get("team_id") or "").strip()
    source_id = str(payload.get("event_id") or "").strip()
    if not source_id and payload.get("type") == "app_rate_limited":
        source_id = "rate:" + hashlib.sha256(json.dumps(
            [app_id, workspace_id, payload.get("minute_rate_limited")],
            separators=(",", ":"),
        ).encode()).hexdigest()
    if not app_id or not workspace_id or not source_id:
        raise ValueError("missing_slack_event_identity")
    if max(len(app_id), len(workspace_id)) > 100 or len(source_id) > 255:
        raise ValueError("invalid_slack_event_identity")
    # No message body is stored in public tables or logs on this path.
    row, created = BridgeSyncInbox.objects.get_or_create(
        app_id=app_id, workspace_id=workspace_id, source_event_id=source_id,
        defaults={"encrypted_payload": json.dumps(payload, separators=(",", ":"))},
    )
    return {"status": "queued" if created else "duplicate", "receipt_id": row.pk}


def claim_inbox(*, lease_seconds=120):
    now = timezone.now()
    with transaction.atomic():
        row = BridgeSyncInbox.objects.filter(
            status__in=["pending", "processing"], available_at__lte=now,
        ).filter(Q(lease_expires_at__isnull=True) | Q(lease_expires_at__lte=now)).select_for_update(
            skip_locked=True,
        ).order_by("available_at", "id").first()
        if row is None:
            return None
        row.lease_token = uuid.uuid4()
        row.lease_expires_at = now + timedelta(seconds=lease_seconds)
        row.attempts += 1
        row.status = "processing"
        row.save(update_fields=["lease_token", "lease_expires_at", "attempts", "status", "updated_at"])
        return row.pk, row.lease_token


def process_inbox_once():
    """Receipt completion and resulting delivery writes commit together."""
    claim = claim_inbox()
    if claim is None:
        return 0
    row_id, token = claim
    row = None
    try:
        with transaction.atomic():
            row = BridgeSyncInbox.objects.select_for_update().filter(
                pk=row_id, lease_token=token, lease_expires_at__gt=timezone.now(),
            ).first()
            if row is None:
                raise LeaseLost("inbox_lease_lost")
            payload, authorizations_complete = expand_authorization_page(json.loads(row.encrypted_payload))
            if not authorizations_complete:
                row.encrypted_payload = json.dumps(payload, separators=(",", ":"))
                row.status = "pending"
                row.available_at = timezone.now() + timedelta(seconds=1)
                row.lease_token = None
                row.lease_expires_at = None
                row.save()
                return 0
            from integrations.services.community_bridge.store import ingest_slack_event
            from integrations.services.slack_dm_mirror import ingest_slack_dm_event
            ingest_slack_dm_event(payload) or ingest_slack_event(payload)
            from .read_priority import invalidate_event
            invalidate_event(payload)
            if row.lease_expires_at <= timezone.now():
                raise LeaseLost("inbox_lease_expired")
            row.status = "completed"
            row.completed_at = timezone.now()
            row.lease_token = None
            row.lease_expires_at = None
            row.last_error_code = ""
            # Keep the receipt identity for deduplication; dispose of the body
            # once the downstream encrypted/public outbox is committed.
            row.encrypted_payload = ""
            row.save()
        return 1
    except Exception as exc:
        # No finite retry count silently discards a verified source event.
        # Operators can observe the bounded code and age without message text.
        BridgeSyncInbox.objects.filter(pk=row_id, lease_token=token).update(
            status="pending", lease_token=None, lease_expires_at=None,
            available_at=timezone.now() + timedelta(seconds=exc.retry_after if isinstance(exc, BudgetDeferred) else (min(3600, 2 ** min(row.attempts, 12)) if row else 5)),
            last_error_code=safe_error_code(type(exc).__name__), updated_at=timezone.now(),
        )
        return 0
