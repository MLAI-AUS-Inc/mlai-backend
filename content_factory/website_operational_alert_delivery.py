"""Durable ops alerts through the existing configured Content Factory channel."""
from copy import deepcopy
from datetime import timedelta
import logging
import re
from uuid import NAMESPACE_URL, uuid5
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from . import website_goal as goals, website_operational_alerts as alerts

ACTION = "goal-alert"
logger = logging.getLogger(__name__)


def channel():
    """Use only the server's existing ops destination, never a worker/owner URL."""
    value = str(getattr(settings, "CONTENT_FACTORY_OPS_SLACK_CHANNEL_ID", "") or "").strip()
    return value if re.fullmatch(r"[CG][A-Z0-9]{8,99}", value) else None


def reserve(op, alert, *, now):
    """Capture the first incident snapshot with an accepted prepare transition."""
    if alert is None:
        return None
    from .website_models import WebsiteConnectionOperation
    scope = deepcopy(op.receipt["goal"]["scope"])
    if not alerts.enabled(op.connection) or not alerts.valid(alert, scope) or alert["goal_id"] != str(op.pk):
        return None
    row, _ = WebsiteConnectionOperation.objects.get_or_create(idempotency_key="articles:ops-alert:" + alert["id"], defaults={
        "connection": op.connection, "generation": op.generation, "action": ACTION, "state": "pending",
        "payload": {"version": 1, "scope": scope, "goal_operation_id": str(op.pk), "alert": deepcopy(alert)},
        "receipt": {"status": "pending", "client_msg_id": str(uuid5(NAMESPACE_URL, "mlai-articles-ops:" + alert["id"]))},
        "next_attempt_at": now})
    if row.action != ACTION or row.connection_id != op.connection_id or goals.mapping(goals.mapping(row.payload).get("alert")).get("id") != alert["id"]:
        raise ValueError("Operational incident attribution changed")
    return row


def advance(identifier, *, now=None):
    """Claim, send outside locks, and save only a matching provider acknowledgement."""
    if not getattr(settings, "ARTICLES_OPERATIONAL_ALERTS_ENABLED", False):
        return None
    from .website_models import WebsiteConnection, WebsiteConnectionOperation
    from .website_operations import deletion_epoch
    now = now or timezone.now()
    candidate = WebsiteConnectionOperation.objects.filter(pk=identifier, action=ACTION).first()
    if candidate is None:
        return None
    destination = channel()
    with transaction.atomic():
        connection = WebsiteConnection.objects.select_for_update().filter(pk=candidate.connection_id).first()
        row = WebsiteConnectionOperation.objects.select_for_update().filter(pk=identifier, action=ACTION).first()
        if row is None or row.state not in {"pending", "running", "needs_attention"}:
            return None
        receipt, payload = goals.mapping(row.receipt), goals.mapping(row.payload)
        if row.state == "needs_attention" and receipt.get("status") != "unconfigured":
            return None
        if row.next_attempt_at and row.next_attempt_at > now:
            return None
        scope, alert = goals.mapping(payload.get("scope")), payload.get("alert")
        if (connection is None or not alerts.enabled(connection) or not alerts.valid(alert, scope)
                or type(payload.get("version")) is not int or payload["version"] != 1
                or payload.get("goal_operation_id") != alert["goal_id"] or row.generation != scope["connection_generation"]
                or scope["connection_id"] != str(connection.pk) or scope["organization_id"] != str(connection.organization_id)
                or scope["deletion_epoch"] != deletion_epoch(connection)):
            row.state, row.next_attempt_at = "cancelled", None
            row.receipt = {"status": "incident_fenced"}
            row.save(update_fields=["state", "next_attempt_at", "receipt", "updated_at"])
            return row.state
        if not destination:
            row.state, row.next_attempt_at = "needs_attention", None
            row.receipt = {**receipt, "status": "unconfigured"}
            row.save(update_fields=["state", "next_attempt_at", "receipt", "updated_at"])
            return row.state
        if receipt.get("channel_id") not in (None, destination):
            row.state, row.next_attempt_at = "needs_attention", None
            row.receipt = {**receipt, "status": "channel_changed"}
            row.save(update_fields=["state", "next_attempt_at", "receipt", "updated_at"])
            return row.state
        if row.attempts >= 3:
            row.state, row.next_attempt_at = "needs_attention", None
            row.receipt = {**receipt, "status": "delivery_exhausted"}
            row.save(update_fields=["state", "next_attempt_at", "receipt", "updated_at"])
            return row.state
        row.state, row.attempts = "running", row.attempts + 1
        row.next_attempt_at = now + timedelta(minutes=5)
        row.receipt = {**receipt, "status": "delivering", "channel_id": destination,
            "client_msg_id": str(uuid5(NAMESPACE_URL, "mlai-articles-ops:" + alert["id"]))}
        row.save(update_fields=["state", "attempts", "next_attempt_at", "receipt", "updated_at"])
        claim, attempt = row.updated_at, row.attempts
    # Erasure or a replaced lease also wins between claim and network admission.
    if not WebsiteConnectionOperation.objects.filter(pk=row.pk, action=ACTION, state="running", attempts=attempt, updated_at=claim).exists():
        return None
    outcome, message_id = "pending", None
    try:
        from integrations.services.slack import SlackService
        response = SlackService.get_client().chat_postMessage(channel=destination, text=alerts.text(alert),
            client_msg_id=row.receipt["client_msg_id"], unfurl_links=False, unfurl_media=False)
        message_id = response.get("ts")
        if (response.get("ok") is not True or not isinstance(message_id, str)
                or not re.fullmatch(r"[0-9]{8,16}\.[0-9]{1,12}", message_id)
                or response.get("channel", destination) != destination):
            raise ValueError("Operational alert was not acknowledged")
        outcome = "completed"
    except Exception:
        message_id = None
        outcome = "needs_attention" if attempt >= 3 else "pending"
        logger.warning("articles_operational_alert_delivery_pending")
    receipt = {**row.receipt, "status": "sent" if outcome == "completed" else "delivery_exhausted" if outcome == "needs_attention" else "pending"}
    if message_id:
        receipt["message_id"] = message_id
    changed = WebsiteConnectionOperation.objects.filter(pk=row.pk, action=ACTION, state="running", attempts=attempt, updated_at=claim).update(
        state=outcome, receipt=receipt, next_attempt_at=row.next_attempt_at if outcome == "pending" else None, updated_at=now)
    return outcome if changed == 1 else None
