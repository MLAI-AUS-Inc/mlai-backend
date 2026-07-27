from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from datetime import timedelta, timezone as datetime_timezone
from typing import Mapping

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2 import id_token as google_id_token

from .connectors.slack import is_slack_dm_scope
from .models import (
    MemoryConnectionConfiguration,
    MemoryConnectionState,
    MemoryProvider,
    MemoryProviderEventReceipt,
    MemoryScopeStatus,
    GmailMailboxWatch,
)


class ProviderEventError(ValueError):
    pass


@dataclass(frozen=True)
class ProviderEventResult:
    status: str
    wake_scheduled: int = 0
    challenge: str = ""


def _header(headers: Mapping, name: str) -> str:
    return str(headers.get(name) or headers.get(name.lower()) or "").strip()


def _body_hash(raw_body: bytes) -> str:
    return hashlib.sha256(raw_body).hexdigest()


def _json_body(raw_body: bytes) -> dict:
    if not raw_body or len(raw_body) > 1_000_000:
        raise ProviderEventError("Provider event body is missing or too large.")
    try:
        value = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProviderEventError("Provider event body is invalid JSON.") from exc
    if not isinstance(value, dict):
        raise ProviderEventError("Provider event body must be an object.")
    return value


def _positive_setting(name: str, default: int) -> int:
    try:
        return max(int(getattr(settings, name, default)), 1)
    except (TypeError, ValueError) as exc:
        raise ProviderEventError(f"{name} must be an integer.") from exc


def _required_secret(name: str) -> str:
    value = str(getattr(settings, name, "") or "").strip()
    if not value:
        raise ProviderEventError(f"{name} is not configured.")
    return value


def verify_linear_event(headers: Mapping, raw_body: bytes, payload: Mapping) -> None:
    secret = _required_secret("ORG_MEMORY_LINEAR_WEBHOOK_SECRET")
    supplied = _header(headers, "Linear-Signature").lower()
    expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    if not supplied or not hmac.compare_digest(expected, supplied):
        raise ProviderEventError("Linear event signature is invalid.")
    try:
        timestamp_ms = int(payload.get("webhookTimestamp"))
    except (TypeError, ValueError) as exc:
        raise ProviderEventError("Linear event timestamp is invalid.") from exc
    max_age = _positive_setting("ORG_MEMORY_LINEAR_WEBHOOK_MAX_AGE_SECONDS", 60)
    if abs(time.time() - (timestamp_ms / 1000)) > max_age:
        raise ProviderEventError("Linear event timestamp is outside the accepted window.")


def verify_slack_event(headers: Mapping, raw_body: bytes) -> None:
    secret = _required_secret("ORG_MEMORY_SLACK_SIGNING_SECRET")
    supplied = _header(headers, "X-Slack-Signature")
    raw_timestamp = _header(headers, "X-Slack-Request-Timestamp")
    try:
        timestamp = int(raw_timestamp)
    except (TypeError, ValueError) as exc:
        raise ProviderEventError("Slack event timestamp is invalid.") from exc
    max_age = _positive_setting("ORG_MEMORY_SLACK_WEBHOOK_MAX_AGE_SECONDS", 300)
    if abs(time.time() - timestamp) > max_age:
        raise ProviderEventError("Slack event timestamp is outside the accepted window.")
    base = b"v0:" + str(timestamp).encode("ascii") + b":" + raw_body
    digest = hmac.new(secret.encode("utf-8"), base, hashlib.sha256).hexdigest()
    expected = f"v0={digest}"
    if not supplied or not hmac.compare_digest(expected, supplied):
        raise ProviderEventError("Slack event signature is invalid.")


def verify_notion_event(headers: Mapping, raw_body: bytes, payload: Mapping) -> None:
    secret = _required_secret("ORG_MEMORY_NOTION_WEBHOOK_VERIFICATION_TOKEN")
    supplied = _header(headers, "X-Notion-Signature").lower()
    digest = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    expected = f"sha256={digest}"
    if not supplied or not hmac.compare_digest(expected, supplied):
        raise ProviderEventError("Notion event signature is invalid.")
    timestamp = parse_datetime(str(payload.get("timestamp") or ""))
    if timestamp is None:
        raise ProviderEventError("Notion event timestamp is invalid.")
    if timezone.is_naive(timestamp):
        timestamp = timezone.make_aware(timestamp, datetime_timezone.utc)
    max_age = _positive_setting("ORG_MEMORY_NOTION_WEBHOOK_MAX_AGE_SECONDS", 90000)
    if abs((timezone.now() - timestamp).total_seconds()) > max_age:
        raise ProviderEventError("Notion event timestamp is outside the accepted window.")


def verify_xero_event(headers: Mapping, raw_body: bytes) -> None:
    secret = _required_secret("ORG_MEMORY_XERO_WEBHOOK_KEY")
    supplied = _header(headers, "X-Xero-Signature")
    expected = base64.b64encode(
        hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).digest()
    ).decode("ascii")
    if not supplied or not hmac.compare_digest(expected, supplied):
        raise ProviderEventError("Xero event signature is invalid.")


def verify_gmail_pubsub_token(headers: Mapping) -> Mapping:
    audience = _required_secret("ORG_MEMORY_GMAIL_PUBSUB_AUDIENCE")
    expected_email = _required_secret("ORG_MEMORY_GMAIL_PUBSUB_SERVICE_ACCOUNT_EMAIL").lower()
    authorization = _header(headers, "Authorization")
    if not authorization.startswith("Bearer "):
        raise ProviderEventError("Gmail Pub/Sub authorization is missing.")
    token = authorization[len("Bearer ") :].strip()
    if not token:
        raise ProviderEventError("Gmail Pub/Sub authorization is missing.")
    try:
        claims = google_id_token.verify_oauth2_token(
            token,
            GoogleAuthRequest(),
            audience=audience,
        )
    except Exception as exc:
        raise ProviderEventError("Gmail Pub/Sub identity token is invalid.") from exc
    email = str(claims.get("email") or "").lower()
    email_verified = claims.get("email_verified")
    if email != expected_email or email_verified not in {True, "true", "True"}:
        raise ProviderEventError("Gmail Pub/Sub identity is not approved.")
    return claims


def _receipt_key(provider: str, event_id: str, payload_hash: str) -> str:
    stable = event_id or payload_hash
    return hashlib.sha256(f"{provider}:{stable}".encode("utf-8")).hexdigest()


def _active_configurations(provider: str, external_account_id: str, external_scope_id: str):
    if not external_account_id:
        return MemoryConnectionConfiguration.objects.none()
    query = MemoryConnectionConfiguration.objects.select_for_update().filter(
        provider=provider,
        lifecycle_state=MemoryConnectionState.ACTIVE,
        external_connection__external_account_id=external_account_id,
        source_scopes__selected=True,
        source_scopes__status=MemoryScopeStatus.SELECTED,
    )
    if external_scope_id:
        query = query.filter(source_scopes__external_id=external_scope_id)
    return query.distinct()


def _schedule_configurations(
    *,
    provider: str,
    external_account_id: str,
    external_scope_id: str = "",
    debounce_seconds: int,
) -> int:
    now = timezone.now()
    target = now + timedelta(seconds=max(int(debounce_seconds), 0))
    scheduled = 0
    for configuration in _active_configurations(
        provider,
        external_account_id,
        external_scope_id,
    ):
        due = configuration.next_scheduled_sync_at
        if due is not None and due <= now:
            continue
        configuration.next_scheduled_sync_at = target
        configuration.save(update_fields=("next_scheduled_sync_at", "updated_at"))
        scheduled += 1
    return scheduled


@transaction.atomic
def schedule_artifact_wake(
    *,
    provider: str,
    external_account_id: str,
    external_scope_id: str = "",
) -> int:
    if provider == MemoryProvider.SLACK and is_slack_dm_scope(external_scope_id):
        return 0
    if provider == MemoryProvider.SLACK:
        setting_name, default = "ORG_MEMORY_SLACK_THREAD_QUIET_SECONDS", 900
    elif provider == MemoryProvider.GMAIL:
        setting_name, default = "ORG_MEMORY_GMAIL_DEBOUNCE_SECONDS", 60
    elif provider in {MemoryProvider.STRIPE, MemoryProvider.XERO, MemoryProvider.LUMA}:
        setting_name, default = "ORG_MEMORY_STRUCTURED_DEBOUNCE_SECONDS", 60
    else:
        setting_name, default = "ORG_MEMORY_LINEAR_DEBOUNCE_SECONDS", 60
    if provider == MemoryProvider.GMAIL:
        return _schedule_gmail_configurations(
            email_address=str(external_account_id or ""),
            debounce_seconds=max(int(getattr(settings, setting_name, default)), 0),
        )
    return _schedule_configurations(
        provider=provider,
        external_account_id=str(external_account_id or ""),
        external_scope_id=str(external_scope_id or ""),
        debounce_seconds=max(int(getattr(settings, setting_name, default)), 0),
    )


def _active_gmail_configurations(email_address: str):
    if not email_address:
        return MemoryConnectionConfiguration.objects.none()
    return (
        MemoryConnectionConfiguration.objects.select_for_update()
        .filter(
            provider=MemoryProvider.GMAIL,
            lifecycle_state=MemoryConnectionState.ACTIVE,
            google_connection__google_email__iexact=email_address,
            source_scopes__selected=True,
            source_scopes__status=MemoryScopeStatus.SELECTED,
        )
        .distinct()
    )


def _schedule_gmail_configurations(*, email_address: str, debounce_seconds: int) -> int:
    now = timezone.now()
    target = now + timedelta(seconds=max(int(debounce_seconds), 0))
    scheduled = 0
    for configuration in _active_gmail_configurations(email_address):
        due = configuration.next_scheduled_sync_at
        if due is not None and due <= now:
            continue
        configuration.next_scheduled_sync_at = target
        configuration.save(update_fields=("next_scheduled_sync_at", "updated_at"))
        scheduled += 1
    return scheduled


def _linear_project_id(payload: Mapping) -> str:
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    project = data.get("project") if isinstance(data.get("project"), dict) else {}
    project_id = str(data.get("projectId") or project.get("id") or "").strip()
    if not project_id and str(payload.get("type") or "").lower() == "project":
        project_id = str(data.get("id") or "").strip()
    return project_id


@transaction.atomic
def receive_linear_event(headers: Mapping, raw_body: bytes) -> ProviderEventResult:
    payload = _json_body(raw_body)
    verify_linear_event(headers, raw_body, payload)
    payload_hash = _body_hash(raw_body)
    organization_id = str(payload.get("organizationId") or "").strip()
    project_id = _linear_project_id(payload)
    event_id = str(payload.get("webhookId") or payload.get("id") or "").strip()
    event_type = ":".join(
        value
        for value in (
            str(payload.get("type") or "").strip(),
            str(payload.get("action") or "").strip(),
        )
        if value
    )[:128]
    receipt, created = MemoryProviderEventReceipt.objects.get_or_create(
        provider=MemoryProvider.LINEAR,
        receipt_key=_receipt_key(
            MemoryProvider.LINEAR,
            f"{organization_id}:{event_id}" if event_id else "",
            payload_hash,
        ),
        defaults={
            "external_account_id": organization_id[:512],
            "external_scope_id": project_id[:512],
            "event_type": event_type,
            "payload_hash": payload_hash,
            "metadata": {
                "event_id_present": bool(event_id),
                "scope_resolved": bool(project_id),
            },
        },
    )
    if not created:
        return ProviderEventResult(status="duplicate")
    scheduled = _schedule_configurations(
        provider=MemoryProvider.LINEAR,
        external_account_id=organization_id,
        external_scope_id=project_id,
        debounce_seconds=max(
            int(getattr(settings, "ORG_MEMORY_LINEAR_DEBOUNCE_SECONDS", 60)),
            0,
        ),
    )
    receipt.scheduled_configuration_count = scheduled
    receipt.save(update_fields=("scheduled_configuration_count",))
    return ProviderEventResult(
        status="accepted" if scheduled else "ignored",
        wake_scheduled=scheduled,
    )


@transaction.atomic
def receive_slack_event(headers: Mapping, raw_body: bytes) -> ProviderEventResult:
    payload = _json_body(raw_body)
    verify_slack_event(headers, raw_body)
    if payload.get("type") == "url_verification":
        challenge = str(payload.get("challenge") or "")
        if not challenge:
            raise ProviderEventError("Slack URL-verification challenge is missing.")
        return ProviderEventResult(status="challenge", challenge=challenge)
    if payload.get("type") != "event_callback":
        return ProviderEventResult(status="ignored")

    event = payload.get("event") if isinstance(payload.get("event"), dict) else {}
    previous_message = (
        event.get("previous_message")
        if isinstance(event.get("previous_message"), dict)
        else {}
    )
    channel_id = str(
        event.get("channel")
        or previous_message.get("channel")
        or ""
    ).strip()
    team_id = str(payload.get("team_id") or payload.get("teamId") or "").strip()
    event_id = str(payload.get("event_id") or "").strip()
    event_type = ":".join(
        value
        for value in (
            str(event.get("type") or "").strip(),
            str(event.get("subtype") or "").strip(),
        )
        if value
    )[:128]
    payload_hash = _body_hash(raw_body)
    receipt, created = MemoryProviderEventReceipt.objects.get_or_create(
        provider=MemoryProvider.SLACK,
        receipt_key=_receipt_key(
            MemoryProvider.SLACK,
            f"{team_id}:{event_id}" if event_id else "",
            payload_hash,
        ),
        defaults={
            "external_account_id": team_id[:512],
            "external_scope_id": channel_id[:512],
            "event_type": event_type,
            "payload_hash": payload_hash,
            "metadata": {
                "event_id_present": bool(event_id),
                "scope_resolved": bool(channel_id),
                "dm_excluded": is_slack_dm_scope(channel_id),
            },
        },
    )
    if not created:
        return ProviderEventResult(status="duplicate")
    scheduled = 0
    if channel_id and not is_slack_dm_scope(channel_id):
        scheduled = _schedule_configurations(
            provider=MemoryProvider.SLACK,
            external_account_id=team_id,
            external_scope_id=channel_id,
            debounce_seconds=max(
                int(getattr(settings, "ORG_MEMORY_SLACK_THREAD_QUIET_SECONDS", 900)),
                0,
            ),
        )
    receipt.scheduled_configuration_count = scheduled
    receipt.save(update_fields=("scheduled_configuration_count",))
    return ProviderEventResult(
        status="accepted" if scheduled else "ignored",
        wake_scheduled=scheduled,
    )


@transaction.atomic
def receive_notion_event(headers: Mapping, raw_body: bytes) -> ProviderEventResult:
    payload = _json_body(raw_body)
    verification_token = str(payload.get("verification_token") or "").strip()
    if verification_token and not _header(headers, "X-Notion-Signature"):
        configured = str(
            getattr(settings, "ORG_MEMORY_NOTION_WEBHOOK_VERIFICATION_TOKEN", "") or ""
        ).strip()
        if configured and not hmac.compare_digest(configured, verification_token):
            raise ProviderEventError("Notion verification token does not match configuration.")
        return ProviderEventResult(status="verification_received")

    verify_notion_event(headers, raw_body, payload)
    workspace_id = str(payload.get("workspace_id") or "").strip()
    event_id = str(payload.get("id") or "").strip()
    event_type = str(payload.get("type") or "")[:128]
    entity = payload.get("entity") if isinstance(payload.get("entity"), dict) else {}
    entity_id = str(entity.get("id") or "").strip()
    payload_hash = _body_hash(raw_body)
    receipt, created = MemoryProviderEventReceipt.objects.get_or_create(
        provider=MemoryProvider.NOTION,
        receipt_key=_receipt_key(
            MemoryProvider.NOTION,
            f"{workspace_id}:{event_id}" if event_id else "",
            payload_hash,
        ),
        defaults={
            "external_account_id": workspace_id[:512],
            "external_scope_id": entity_id[:512],
            "event_type": event_type,
            "payload_hash": payload_hash,
            "metadata": {
                "event_id_present": bool(event_id),
                "entity_type": str(entity.get("type") or "")[:64],
                "content_in_receipt": False,
            },
        },
    )
    if not created:
        return ProviderEventResult(status="duplicate")
    scheduled = _schedule_configurations(
        provider=MemoryProvider.NOTION,
        external_account_id=workspace_id,
        # A changed page may be a descendant of a selected root, so wake every
        # active configuration for this connected workspace and reconcile there.
        external_scope_id="",
        debounce_seconds=max(
            int(getattr(settings, "ORG_MEMORY_NOTION_DEBOUNCE_SECONDS", 60)),
            0,
        ),
    )
    receipt.scheduled_configuration_count = scheduled
    receipt.save(update_fields=("scheduled_configuration_count",))
    return ProviderEventResult(
        status="accepted" if scheduled else "ignored",
        wake_scheduled=scheduled,
    )


@transaction.atomic
def receive_xero_event(headers: Mapping, raw_body: bytes) -> ProviderEventResult:
    # Xero signs the exact raw request body, so authenticate before parsing or
    # deriving any wake metadata from untrusted input.
    verify_xero_event(headers, raw_body)
    payload = _json_body(raw_body)
    events = payload.get("events")
    if not isinstance(events, list) or len(events) > 100:
        raise ProviderEventError("Xero event list is invalid.")

    tenant_ids = set()
    categories = set()
    event_types = set()
    sequence_numbers = []
    for event in events:
        if not isinstance(event, dict):
            raise ProviderEventError("Xero event is invalid.")
        tenant_id = str(
            event.get("tenantId")
            or event.get("TenantId")
            or event.get("TenantID")
            or ""
        ).strip()
        if tenant_id:
            tenant_ids.add(tenant_id)
        category = str(event.get("eventCategory") or event.get("EventCategory") or "").strip()
        event_type = str(event.get("eventType") or event.get("EventType") or "").strip()
        if category:
            categories.add(category[:64])
        if event_type:
            event_types.add(event_type[:64])
        raw_sequence = event.get("eventSequence") or event.get("EventSequence")
        try:
            sequence_numbers.append(int(raw_sequence))
        except (TypeError, ValueError):
            pass

    payload_hash = _body_hash(raw_body)
    account_id = next(iter(tenant_ids)) if len(tenant_ids) == 1 else ""
    receipt, created = MemoryProviderEventReceipt.objects.get_or_create(
        provider=MemoryProvider.XERO,
        receipt_key=_receipt_key(MemoryProvider.XERO, "", payload_hash),
        defaults={
            "external_account_id": account_id[:512],
            "event_type": "xero_delivery",
            "payload_hash": payload_hash,
            "metadata": {
                "event_count": len(events),
                "tenant_count": len(tenant_ids),
                "categories": sorted(categories),
                "event_types": sorted(event_types),
                "first_sequence": min(sequence_numbers) if sequence_numbers else None,
                "last_sequence": max(sequence_numbers) if sequence_numbers else None,
                "content_in_receipt": False,
            },
        },
    )
    if not created:
        return ProviderEventResult(status="duplicate")

    debounce = max(
        int(getattr(settings, "ORG_MEMORY_STRUCTURED_DEBOUNCE_SECONDS", 60)),
        0,
    )
    scheduled = sum(
        _schedule_configurations(
            provider=MemoryProvider.XERO,
            external_account_id=tenant_id,
            debounce_seconds=debounce,
        )
        for tenant_id in tenant_ids
    )
    receipt.scheduled_configuration_count = scheduled
    receipt.save(update_fields=("scheduled_configuration_count",))
    return ProviderEventResult(
        status="accepted" if scheduled else "ignored",
        wake_scheduled=scheduled,
    )


@transaction.atomic
def receive_gmail_push(headers: Mapping, raw_body: bytes) -> ProviderEventResult:
    verify_gmail_pubsub_token(headers)
    payload = _json_body(raw_body)
    message = payload.get("message") if isinstance(payload.get("message"), dict) else {}
    message_id = str(message.get("messageId") or message.get("message_id") or "").strip()
    publish_time = parse_datetime(str(message.get("publishTime") or ""))
    if publish_time is None:
        raise ProviderEventError("Gmail Pub/Sub publish time is invalid.")
    if timezone.is_naive(publish_time):
        publish_time = timezone.make_aware(publish_time, datetime_timezone.utc)
    max_age = _positive_setting("ORG_MEMORY_GMAIL_PUBSUB_MAX_AGE_SECONDS", 86400)
    if abs((timezone.now() - publish_time).total_seconds()) > max_age:
        raise ProviderEventError("Gmail Pub/Sub notification is outside the accepted window.")
    encoded = str(message.get("data") or "")
    try:
        padding = "=" * (-len(encoded) % 4)
        decoded = base64.urlsafe_b64decode((encoded + padding).encode("ascii"))
        notification = json.loads(decoded.decode("utf-8"))
    except Exception as exc:
        raise ProviderEventError("Gmail Pub/Sub notification data is invalid.") from exc
    if not isinstance(notification, dict) or len(decoded) > 10000:
        raise ProviderEventError("Gmail Pub/Sub notification data is invalid.")
    email_address = str(notification.get("emailAddress") or "").strip().lower()
    history_id = str(notification.get("historyId") or "").strip()
    if not email_address or not history_id:
        raise ProviderEventError("Gmail Pub/Sub notification identity is missing.")
    payload_hash = _body_hash(raw_body)
    receipt, created = MemoryProviderEventReceipt.objects.get_or_create(
        provider=MemoryProvider.GMAIL,
        receipt_key=_receipt_key(
            MemoryProvider.GMAIL,
            f"{email_address}:{message_id}" if message_id else "",
            payload_hash,
        ),
        defaults={
            "external_account_id": email_address[:512],
            "event_type": "mailbox_changed",
            "payload_hash": payload_hash,
            "metadata": {
                "pubsub_message_id_present": bool(message_id),
                "history_id_present": True,
                "content_in_receipt": False,
            },
        },
    )
    if not created:
        return ProviderEventResult(status="duplicate")
    scheduled = _schedule_gmail_configurations(
        email_address=email_address,
        debounce_seconds=max(
            int(getattr(settings, "ORG_MEMORY_GMAIL_DEBOUNCE_SECONDS", 60)),
            0,
        ),
    )
    receipt.scheduled_configuration_count = scheduled
    receipt.save(update_fields=("scheduled_configuration_count",))
    GmailMailboxWatch.objects.filter(
        configuration__provider=MemoryProvider.GMAIL,
        configuration__lifecycle_state=MemoryConnectionState.ACTIVE,
        configuration__google_connection__google_email__iexact=email_address,
    ).update(
        last_notification_at=timezone.now(),
        last_notification_history_id=history_id[:255],
        last_pubsub_message_id=message_id[:255],
    )
    return ProviderEventResult(
        status="accepted" if scheduled else "ignored",
        wake_scheduled=scheduled,
    )
