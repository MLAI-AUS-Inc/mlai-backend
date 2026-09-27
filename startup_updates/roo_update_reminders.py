"""Renewal reminders from verified Public Roo through the owner's Slack mirror.

Channel state lives in the existing reminder ledger's provider_response JSON.
Email and Roo can therefore be enabled independently without a schema change.
"""
from __future__ import annotations

import html
import uuid
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlencode

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from integrations.models import (
    ExternalServiceConnectionStatus,
    SlackDmMirrorGrant,
    SlackDmMirrorGrantStatus,
)
from integrations.services.slack import SlackService
from integrations.services.slack_roo import public_roo_target

from .models import MonthlyUpdateReminderDelivery, MonthlyUpdateReminderKind

CHAT_STATE_KEY = "roo_chat"
MAX_ATTEMPTS = 5
TERMINAL_STATES = {"sent", "sending", "unknown", "suppressed"}


def _chat_url(company_id: str) -> str:
    origin = str(settings.COMMUNITY_CHAT_FRONTEND_URL).rstrip("/")
    query = urlencode({"startup": company_id, "startupView": "new"})
    return f"{origin}/pulse?{query}"


def _message(targets) -> str:
    lines = ["Your next monthly startup update is due tomorrow. 🦘"]
    for target in targets:
        name = html.escape(target.company_name, quote=False)
        due = target.expires_at.strftime("%d %B %Y at %I:%M %p %Z")
        lines.append(
            f"• *{name}*: your current 4-point coworking rate expires {due}. "
            f"<{_chat_url(target.company_id)}|Write your next monthly update>."
        )
    lines.append(
        "Approve your next monthly update to earn 20 Roo points once per startup/month "
        "and renew 30 days of coworking at 4 points instead of 8, while your Australian "
        "startup has a verified active ABN."
    )
    return "\n\n".join(lines)


def _roo_client():
    # The SDK default can retry a connection failure after Slack accepted a
    # post. Only this delivery ledger may decide whether a retry is safe.
    return WebClient(token=SlackService.get_client().token, timeout=30, retry_handlers=[])


def _active_grant(user_id, workspace_id):
    return SlackDmMirrorGrant.objects.filter(
        user_id=user_id,
        user__is_active=True,
        slack_workspace_id=workspace_id,
        status=SlackDmMirrorGrantStatus.ACTIVE,
        revoked_at__isnull=True,
        connection__status=ExternalServiceConnectionStatus.CONNECTED,
    ).first()


def _set_state(delivery_id, claim, state, **values):
    """Merge one channel's result without losing a concurrent email result."""
    with transaction.atomic():
        delivery = MonthlyUpdateReminderDelivery.objects.select_for_update().get(pk=delivery_id)
        current = delivery.provider_response.get(CHAT_STATE_KEY, {})
        if current.get("claim") != claim:
            return False
        delivery.provider_response = {
            **delivery.provider_response,
            CHAT_STATE_KEY: {**current, "status": state, **values},
        }
        delivery.save(update_fields=["provider_response", "updated_at"])
    return True


def _retry(delivery_id, claim, now, attempt, reason):
    delay = min(3600, 60 * (2 ** max(0, attempt - 1)))
    _set_state(
        delivery_id, claim, "retry", reason=reason,
        retry_at=(now + timedelta(seconds=delay)).isoformat(),
    )
    return {"status": "retry", "delivery_id": delivery_id, "reason": reason}


def _fresh_targets(targets):
    from .monthly_update_reminders import collect_monthly_update_reminder_targets

    first = targets[0]
    current = collect_monthly_update_reminder_targets(first.reminder_date)
    expected = {(target.company_id, target.source_update_id, target.expires_at) for target in targets}
    return [
        target for target in current
        if target.user_id == first.user_id
        and target.reminder_kind == MonthlyUpdateReminderKind.ONE_DAY
        and (target.company_id, target.source_update_id, target.expires_at) in expected
    ]


def dispatch_roo_reminder(targets, *, now: datetime | None = None) -> dict[str, Any]:
    """Send once per founder/day; retry safe preflight and rejected rate limits.

    An ambiguous failure after posting begins is quarantined, never blindly
    retried. A preparing lease can be reclaimed because it cannot post until its
    claim is checked again under a row lock.
    """
    now = now or timezone.now()
    first = targets[0]
    if first.reminder_kind != MonthlyUpdateReminderKind.ONE_DAY:
        return {"status": "skipped", "reason": "not_due_tomorrow"}
    roo = public_roo_target()
    if roo is None:
        return {"status": "skipped", "reason": "public_roo_not_configured"}
    grant = _active_grant(first.user_id, roo[0])
    if grant is None:
        return {"status": "skipped", "reason": "no_active_chat_connection"}

    from .monthly_update_reminders import _snapshot, _template_id

    key = f"monthly-update-reminder:{first.user_id}:{first.reminder_kind}:{first.reminder_date.isoformat()}"
    claim = str(uuid.uuid4())
    with transaction.atomic():
        delivery, _ = MonthlyUpdateReminderDelivery.objects.get_or_create(
            idempotency_key=key,
            defaults={
                "user_id": first.user_id,
                "reminder_kind": first.reminder_kind,
                "reminder_date": first.reminder_date,
                "recipient_email": first.recipient_email,
                "template_id": _template_id(first.reminder_kind),
                "target_snapshot": _snapshot(targets),
            },
        )
        delivery = MonthlyUpdateReminderDelivery.objects.select_for_update().get(pk=delivery.pk)
        current = delivery.provider_response.get(CHAT_STATE_KEY, {})
        status = current.get("status")
        if status in TERMINAL_STATES:
            return {"status": "skipped", "reason": f"already_{status}", "delivery_id": delivery.pk}
        retry_at = current.get("retry_at")
        if retry_at and datetime.fromisoformat(retry_at) > now:
            return {"status": "skipped", "reason": "retry_backoff", "delivery_id": delivery.pk}
        attempt = int(current.get("attempt_count", 0)) + 1
        if attempt > MAX_ATTEMPTS:
            return {"status": "skipped", "reason": "retry_exhausted", "delivery_id": delivery.pk}
        delivery.provider_response = {
            **delivery.provider_response,
            CHAT_STATE_KEY: {
                "status": "preparing", "claim": claim, "attempt_count": attempt,
                "retry_at": (now + timedelta(minutes=5)).isoformat(),
                "target_snapshot": _snapshot(targets),
            },
        }
        delivery.save(update_fields=["provider_response", "updated_at"])

    # These operations cannot post a message and may safely be retried.
    try:
        client = _roo_client()
        identity = client.auth_test()
        if (identity.get("team_id"), identity.get("user_id")) != roo or not identity.get("ok"):
            return _retry(delivery.pk, claim, now, attempt, "public_roo_identity_mismatch")
        opened = client.conversations_open(users=[grant.slack_user_id])
        channel = str((opened.get("channel") or {}).get("id") or "")
        if not opened.get("ok") or not channel.startswith("D"):
            return _retry(delivery.pk, claim, now, attempt, "dm_open_failed")
        fresh_grant = _active_grant(first.user_id, roo[0])
        targets = _fresh_targets(targets)
        if (
            fresh_grant is None or fresh_grant.pk != grant.pk
            or fresh_grant.slack_user_id != grant.slack_user_id or not targets
        ):
            _set_state(delivery.pk, claim, "suppressed", reason="eligibility_or_chat_connection_changed")
            return {"status": "suppressed", "delivery_id": delivery.pk}
    except Exception:
        return _retry(delivery.pk, claim, now, attempt, "preflight_unavailable")

    if not _set_state(delivery.pk, claim, "sending", channel_id=channel):
        return {"status": "skipped", "reason": "claim_replaced", "delivery_id": delivery.pk}
    try:
        response = client.chat_postMessage(
            channel=channel,
            text=_message(targets),
            # Stable across rejected requests; local ledger remains authoritative.
            client_msg_id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"{key}:roo")),
            unfurl_links=False,
            unfurl_media=False,
        )
        if not response.get("ok") or not response.get("ts"):
            raise RuntimeError("Unconfirmed Slack delivery")
    except SlackApiError as exc:
        if str(exc.response.get("error") or "") == "ratelimited":
            return _retry(delivery.pk, claim, now, attempt, "slack_rate_limited")
        _set_state(delivery.pk, claim, "unknown", reason="slack_delivery_unconfirmed")
        return {"status": "unknown", "delivery_id": delivery.pk}
    except Exception:
        _set_state(delivery.pk, claim, "unknown", reason="slack_delivery_unconfirmed")
        return {"status": "unknown", "delivery_id": delivery.pk}

    _set_state(
        delivery.pk, claim, "sent", message_ts=str(response["ts"]),
        dispatched_at=now.isoformat(),
    )
    return {"status": "sent", "delivery_id": delivery.pk}
