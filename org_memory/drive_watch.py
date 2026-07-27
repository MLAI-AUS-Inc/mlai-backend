from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone as datetime_timezone
from typing import Mapping, Optional
from urllib.parse import urlparse

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .drive_inventory import DriveInventoryError, build_drive_service
from .models import (
    DriveWatchChannel,
    DriveWatchStatus,
    MemoryConnectionState,
    MemoryProvider,
)


KNOWN_RESOURCE_STATES = frozenset(
    {"sync", "change", "changed", "add", "remove", "update", "trash", "untrash"}
)


class DriveWatchError(ValueError):
    pass


@dataclass(frozen=True)
class DriveNotificationResult:
    status: str
    wake_scheduled: bool
    configuration_id: Optional[str] = None


def _token_hash(value: str) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def register_drive_watch(
    configuration,
    *,
    callback_url: Optional[str] = None,
    lifetime: timedelta = timedelta(days=6),
) -> DriveWatchChannel:
    if configuration.provider != MemoryProvider.GOOGLE_DRIVE:
        raise DriveWatchError("Watch registration requires a Google Drive configuration.")
    if configuration.lifecycle_state in {
        MemoryConnectionState.DELETE_PENDING,
        MemoryConnectionState.DELETED,
    }:
        raise DriveWatchError("Deleted Drive configurations cannot register watches.")
    if lifetime <= timedelta(0) or lifetime > timedelta(days=7):
        raise DriveWatchError("Drive change watches must expire within seven days.")
    callback_url = str(
        callback_url or settings.ORG_MEMORY_DRIVE_WATCH_CALLBACK_URL or ""
    ).strip()
    parsed = urlparse(callback_url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise DriveWatchError("Drive watch callback URL must be an absolute HTTPS URL.")

    try:
        service = build_drive_service(configuration.connection)
        page_token = str(configuration.sync_cursor or "")
        if not page_token:
            response = service.changes().getStartPageToken(
                supportsAllDrives=True,
            ).execute(num_retries=2)
            page_token = str(response.get("startPageToken") or "")
    except DriveInventoryError as exc:
        raise DriveWatchError(str(exc)) from exc
    if not page_token:
        raise DriveWatchError("Drive did not return a change token for watch registration.")

    channel_id = str(uuid.uuid4())
    channel_token = secrets.token_urlsafe(32)
    requested_expiration = timezone.now() + lifetime
    response = service.changes().watch(
        pageToken=page_token,
        supportsAllDrives=True,
        includeItemsFromAllDrives=True,
        body={
            "id": channel_id,
            "type": "web_hook",
            "address": callback_url,
            "token": channel_token,
            "expiration": str(int(requested_expiration.timestamp() * 1000)),
        },
    ).execute(num_retries=2)
    resource_id = str(response.get("resourceId") or "")
    if not resource_id:
        raise DriveWatchError("Drive watch response did not include a resource ID.")
    raw_expiration = response.get("expiration")
    try:
        expiration_at = datetime.fromtimestamp(
            int(raw_expiration) / 1000,
            tz=datetime_timezone.utc,
        )
    except (TypeError, ValueError, OSError):
        expiration_at = requested_expiration
    channel = DriveWatchChannel(
        configuration=configuration,
        channel_id=channel_id,
        resource_id=resource_id,
        resource_uri=str(response.get("resourceUri") or "")[:2048],
        token_hash=_token_hash(channel_token),
        expiration_at=expiration_at,
    )
    channel.full_clean()
    channel.save()
    return channel


def renew_drive_watch(configuration, *, force: bool = False) -> DriveWatchChannel:
    """Return a healthy channel or register a replacement before expiry."""

    now = timezone.now()
    try:
        renew_seconds = int(
            getattr(settings, "ORG_MEMORY_DRIVE_WATCH_RENEW_SECONDS", 86400)
        )
    except (TypeError, ValueError) as exc:
        raise DriveWatchError(
            "ORG_MEMORY_DRIVE_WATCH_RENEW_SECONDS must be an integer."
        ) from exc
    if renew_seconds < 1 or renew_seconds > 7 * 86400:
        raise DriveWatchError(
            "ORG_MEMORY_DRIVE_WATCH_RENEW_SECONDS must be between 1 and 604800."
        )
    configuration.drive_watch_channels.filter(
        status=DriveWatchStatus.ACTIVE,
        expiration_at__lte=now,
    ).update(status=DriveWatchStatus.EXPIRED, updated_at=now)
    active = configuration.drive_watch_channels.filter(
        status=DriveWatchStatus.ACTIVE,
        expiration_at__gt=now,
    ).order_by("-expiration_at").first()
    if (
        not force
        and active is not None
        and active.expiration_at > now + timedelta(seconds=renew_seconds)
    ):
        return active
    return register_drive_watch(configuration)


def _header(headers: Mapping, name: str) -> str:
    return str(headers.get(name) or headers.get(name.lower()) or "").strip()


@transaction.atomic
def receive_drive_notification(headers: Mapping) -> DriveNotificationResult:
    channel_id = _header(headers, "X-Goog-Channel-ID")
    resource_id = _header(headers, "X-Goog-Resource-ID")
    channel_token = _header(headers, "X-Goog-Channel-Token")
    resource_state = _header(headers, "X-Goog-Resource-State").lower()
    raw_message_number = _header(headers, "X-Goog-Message-Number")
    if not all((channel_id, resource_id, channel_token, resource_state, raw_message_number)):
        raise DriveWatchError("Drive notification headers are incomplete.")
    try:
        message_number = int(raw_message_number)
    except ValueError as exc:
        raise DriveWatchError("Drive notification message number is invalid.") from exc
    if message_number < 1:
        raise DriveWatchError("Drive notification message number is invalid.")

    channel = (
        DriveWatchChannel.objects.select_for_update()
        .select_related("configuration")
        .filter(channel_id=channel_id)
        .first()
    )
    now = timezone.now()
    if channel is None:
        raise DriveWatchError("Drive notification channel is unknown.")
    if channel.status != DriveWatchStatus.ACTIVE or channel.expiration_at <= now:
        if channel.status == DriveWatchStatus.ACTIVE:
            channel.status = DriveWatchStatus.EXPIRED
            channel.save(update_fields=("status", "updated_at"))
        raise DriveWatchError("Drive notification channel is inactive.")
    if not hmac.compare_digest(channel.resource_id, resource_id):
        raise DriveWatchError("Drive notification resource does not match its channel.")
    if not hmac.compare_digest(channel.token_hash, _token_hash(channel_token)):
        raise DriveWatchError("Drive notification token is invalid.")
    if message_number <= channel.last_message_number:
        return DriveNotificationResult(
            status="duplicate",
            wake_scheduled=False,
            configuration_id=str(channel.configuration_id),
        )
    if resource_state not in KNOWN_RESOURCE_STATES:
        return DriveNotificationResult(
            status="ignored",
            wake_scheduled=False,
            configuration_id=str(channel.configuration_id),
        )

    channel.last_message_number = message_number
    channel.last_resource_state = resource_state
    channel.last_notified_at = now
    channel.save(
        update_fields=(
            "last_message_number",
            "last_resource_state",
            "last_notified_at",
            "updated_at",
        )
    )
    wake = resource_state != "sync"
    if wake:
        configuration = channel.configuration
        if configuration.lifecycle_state == MemoryConnectionState.ACTIVE:
            configuration.next_scheduled_sync_at = now
            configuration.save(update_fields=("next_scheduled_sync_at", "updated_at"))
        else:
            wake = False
    return DriveNotificationResult(
        status="accepted",
        wake_scheduled=wake,
        configuration_id=str(channel.configuration_id),
    )
