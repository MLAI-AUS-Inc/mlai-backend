from __future__ import annotations

import base64
import json
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone as datetime_timezone
from typing import Mapping, Optional

from django.conf import settings
from django.db.models import Max
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from googleapiclient.errors import HttpError

from integrations.services.gmail import (
    StaleHistoryCursorError,
    get_gmail_profile,
    get_message_full,
    get_message_metadata,
    list_gmail_labels,
    list_history_page,
    list_label_message_page,
    upsert_message_artifact_from_message_data,
    watch_gmail_mailbox,
)
from integrations.services.gmail_scopes import has_gmail_read_scope
from org_memory.models import (
    GmailMailboxWatch,
    GmailScopedArtifactState,
    GmailScopedMessageArtifact,
    GmailWatchStatus,
    MemoryClassification,
    MemorySource,
)
from startup_updates.models import (
    ArtifactProcessingStatus,
    GmailAttachmentArtifact,
    GmailMessageArtifact,
    GmailSyncCursor,
)

from .artifact_utils import (
    bounded_text,
    canonical_hash,
    content_hash,
    estimate_tokens,
    source_acl,
    version_key,
)
from .base import (
    ConnectorHealth,
    DryRunResult,
    ScopeDescriptor,
    ScopePage,
    SourcePreview,
    SourceVersionPayload,
    SyncPage,
    TombstoneResult,
)


UNSAFE_SYSTEM_LABEL_IDS = frozenset(
    {
        "CHAT",
        "SENT",
        "INBOX",
        "IMPORTANT",
        "TRASH",
        "DRAFT",
        "SPAM",
        "CATEGORY_FORUMS",
        "CATEGORY_UPDATES",
        "CATEGORY_PERSONAL",
        "CATEGORY_PROMOTIONS",
        "CATEGORY_SOCIAL",
        "STARRED",
        "UNREAD",
    }
)
HISTORY_TYPES = ("messageAdded", "messageDeleted", "labelAdded", "labelRemoved")


class GmailProviderError(RuntimeError):
    pass


def _setting(name: str, default: int, *, maximum: Optional[int] = None) -> int:
    value = max(int(getattr(settings, name, default)), 1)
    return min(value, maximum) if maximum else value


def _page_size() -> int:
    return _setting("ORG_MEMORY_GMAIL_PAGE_SIZE", 10, maximum=100)


def _selected_scope_map(configuration, selected_scopes=None):
    connection = configuration.connection
    if not has_gmail_read_scope(connection):
        raise GmailProviderError("Gmail connection is missing the readonly Gmail scope.")
    if not str(getattr(connection, "refresh_token", "") or "").strip():
        raise GmailProviderError("Gmail connection is missing its refresh token.")
    if not str(getattr(connection, "google_email", "") or "").strip():
        raise GmailProviderError("Gmail connection is missing its mailbox identity.")
    scopes = list(
        selected_scopes
        if selected_scopes is not None
        else configuration.source_scopes.filter(selected=True, status="selected")
    )
    result = {}
    for scope in scopes:
        label_id = str(scope.external_id or "").strip()
        label_type = str((scope.metadata or {}).get("label_type") or "").lower()
        if scope.scope_type != "label" or not label_id:
            raise ValueError("Gmail memory supports explicit label scopes only.")
        if label_id.upper() in UNSAFE_SYSTEM_LABEL_IDS or label_type == "system":
            raise ValueError("Broad Gmail system labels cannot be selected for memory.")
        result[label_id] = scope
    if not result:
        raise ValueError("Gmail memory requires at least one selected user label.")
    if len(result) > 100:
        raise ValueError("Gmail memory supports at most 100 selected labels per mailbox.")
    return result


def _connection_ready(configuration) -> bool:
    return bool(
        has_gmail_read_scope(configuration.connection)
        and str(getattr(configuration.connection, "refresh_token", "") or "").strip()
        and str(getattr(configuration.connection, "google_email", "") or "").strip()
    )


def _classification(scope) -> str:
    value = str(scope.default_classification or MemoryClassification.INTERNAL)
    if value == MemoryClassification.INTERNAL:
        return MemoryClassification.EXECUTIVE
    return value


def _encode_state(value: Mapping) -> str:
    raw = json.dumps(dict(value), sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_state(value: Optional[str]) -> dict:
    if not value:
        return {"version": 1, "mode": "idle"}
    try:
        padding = "=" * (-len(value) % 4)
        result = json.loads(base64.urlsafe_b64decode((value + padding).encode("ascii")).decode("utf-8"))
    except Exception as exc:
        raise ValueError("Gmail memory cursor is invalid.") from exc
    if not isinstance(result, dict) or result.get("version") != 1:
        raise ValueError("Gmail memory cursor is invalid.")
    return result


def _initial_full_state(*, last_full_scan_at="") -> dict:
    return {
        "version": 1,
        "mode": "full_scan",
        "phase": "scan",
        "scan_id": str(uuid.uuid4()),
        "label_index": 0,
        "page_token": "",
        "last_thread_id": "",
        "last_full_scan_at": str(last_full_scan_at or ""),
        "cursor_recovered": False,
    }


def _history_state(history_id: str, *, last_full_scan_at="") -> dict:
    return {
        "version": 1,
        "mode": "history",
        "start_history_id": str(history_id),
        "page_token": "",
        "last_full_scan_at": str(last_full_scan_at or ""),
    }


def _cutoff(configuration):
    if configuration.historical_cutoff:
        return configuration.historical_cutoff
    return timezone.now() - timedelta(
        days=_setting("ORG_MEMORY_GMAIL_BACKFILL_DAYS", 365, maximum=3650)
    )


def _backfill_query(configuration) -> str:
    return (
        f"after:{int(_cutoff(configuration).timestamp())} "
        "-in:spam -in:trash -category:promotions -category:social -category:forums"
    )


def _is_not_found(exc: Exception) -> bool:
    if not isinstance(exc, HttpError):
        return False
    try:
        return int(getattr(getattr(exc, "resp", None), "status", 0)) == 404
    except (TypeError, ValueError):
        return False


def _active_mappings(configuration):
    return GmailScopedMessageArtifact.objects.filter(
        configuration=configuration,
        lifecycle_state=GmailScopedArtifactState.ACTIVE,
        message_artifact__isnull=False,
    ).select_related("message_artifact", "source_scope")


def _selected_labels(message: GmailMessageArtifact, scopes) -> list[str]:
    labels = {str(value) for value in (message.label_ids or [])}
    return [label_id for label_id in scopes if label_id in labels]


def _scope_for_labels(scopes, label_ids):
    for label_id in scopes:
        if label_id in label_ids:
            return scopes[label_id]
    return None


def _mark_mapping_removed(configuration, message_id, state):
    mapping = GmailScopedMessageArtifact.objects.filter(
        configuration=configuration,
        gmail_message_id=message_id,
    ).first()
    if mapping is None:
        return ""
    mapping.lifecycle_state = state
    mapping.selected_label_ids = []
    mapping.removed_at = timezone.now()
    mapping.last_seen_at = timezone.now()
    mapping.save(
        update_fields=(
            "lifecycle_state",
            "selected_label_ids",
            "removed_at",
            "last_seen_at",
            "updated_at",
        )
    )
    return mapping.gmail_thread_id


def _upsert_current_message(configuration, scopes, payload, *, scan_id=None):
    message = upsert_message_artifact_from_message_data(
        organization=configuration.organization,
        connection=configuration.connection,
        message_data=payload,
    )
    label_ids = _selected_labels(message, scopes)
    if message.internal_date < _cutoff(configuration):
        label_ids = []
    if not label_ids:
        thread_id = _mark_mapping_removed(
            configuration,
            message.gmail_message_id,
            GmailScopedArtifactState.LABEL_REMOVED,
        )
        return None, thread_id or message.gmail_thread_id
    scope = _scope_for_labels(scopes, label_ids)
    mapping, _created = GmailScopedMessageArtifact.objects.update_or_create(
        configuration=configuration,
        gmail_message_id=message.gmail_message_id,
        defaults={
            "organization": configuration.organization,
            "source_scope": scope,
            "message_artifact": message,
            "gmail_thread_id": message.gmail_thread_id,
            "selected_label_ids": label_ids,
            "lifecycle_state": GmailScopedArtifactState.ACTIVE,
            "history_id": message.history_id,
            "internal_date": message.internal_date,
            "scan_generation": scan_id,
            "last_seen_at": timezone.now(),
            "removed_at": None,
        },
    )
    return mapping, mapping.gmail_thread_id


def _message_chunk_text(message: GmailMessageArtifact) -> str:
    recipients = ", ".join(str(value) for value in (message.to_addresses or [])[:25])
    copied = ", ".join(str(value) for value in (message.cc_addresses or [])[:25])
    return bounded_text(
        "\n".join(
            value
            for value in (
                f"Subject: {message.subject}" if message.subject else "",
                f"From: {message.from_address}" if message.from_address else "",
                f"To: {recipients}" if recipients else "",
                f"Cc: {copied}" if copied else "",
                f"Date: {message.internal_date.isoformat()}",
                str(message.cleaned_text or message.body_preview or message.snippet or ""),
            )
            if value
        ),
        _setting("ORG_MEMORY_GMAIL_MAX_MESSAGE_CHARS", 50000, maximum=200000),
    )


def _thread_chunks(messages, selected_labels_by_message=None):
    selected_labels_by_message = selected_labels_by_message or {}
    target = _setting("ORG_MEMORY_GMAIL_CHUNK_TARGET_CHARS", 6000, maximum=20000)
    chunks = []
    for message in messages:
        text = _message_chunk_text(message)
        if not text:
            continue
        start = 0
        while start < len(text):
            end = min(start + target, len(text))
            if end < len(text):
                split = text.rfind("\n", start, end)
                if split > start + target // 2:
                    end = split
            part = text[start:end].strip()
            if part:
                chunks.append(
                    {
                        "ordinal": len(chunks),
                        "chunk_kind": "gmail_message",
                        "text": part,
                        "token_count": estimate_tokens(part),
                        "source_locator": {
                            "thread_id": message.gmail_thread_id,
                            "message_id": message.gmail_message_id,
                            "start_char": start,
                            "end_char": end,
                            "label_ids": list(
                                selected_labels_by_message.get(
                                    message.gmail_message_id,
                                    (),
                                )
                            ),
                        },
                        "occurred_at": message.internal_date,
                    }
                )
            start = max(end, start + 1)
    return tuple(chunks)


def _gmail_acl(configuration, scope, *, revision):
    acl = source_acl(
        configuration,
        scope,
        revision_payload=revision,
    )
    acl["is_accessible"] = bool(
        acl["is_accessible"]
        and has_gmail_read_scope(configuration.connection)
        and str(configuration.connection.refresh_token or "").strip()
    )
    acl["metadata"] = {
        **dict(acl.get("metadata") or {}),
        "mailbox": str(configuration.connection.google_email or "")[:254],
        "restricted_email": True,
    }
    return acl


def _thread_record(configuration, thread_id):
    mappings = list(
        _active_mappings(configuration)
        .filter(gmail_thread_id=thread_id)
        .order_by("internal_date", "gmail_message_id")
    )
    if not mappings:
        return None
    scope = next((row.source_scope for row in mappings if row.source_scope_id), None)
    if scope is None:
        return None
    seen = set()
    messages = []
    for mapping in mappings:
        message = mapping.message_artifact
        if message and message.pk not in seen:
            seen.add(message.pk)
            messages.append(message)
    if not messages:
        return None
    chunks = _thread_chunks(
        messages,
        {
            mapping.gmail_message_id: tuple(mapping.selected_label_ids or ())
            for mapping in mappings
        },
    )
    if not chunks:
        return None
    label_ids = list(
        dict.fromkeys(
            str(label_id)
            for mapping in mappings
            for label_id in (mapping.selected_label_ids or [])
        )
    )
    latest = max(messages, key=lambda row: row.internal_date)
    earliest = min(messages, key=lambda row: row.internal_date)
    title = bounded_text(latest.subject or earliest.subject or "Gmail thread", 512)
    normalized_text = "\n\n".join(chunk["text"] for chunk in chunks)
    revision = {
        "thread_id": thread_id,
        "message_revisions": [
            {
                "message_id": message.gmail_message_id,
                "history_id": message.history_id,
                "updated_at": message.updated_at,
                "content_hash": content_hash(_message_chunk_text(message)),
            }
            for message in messages
        ],
        "selected_label_ids": label_ids,
    }
    acl = _gmail_acl(configuration, scope, revision=revision)
    payload = {
        "content_hash": content_hash(normalized_text),
        "revision": revision,
        "acl": acl,
        "adapter": "gmail-thread-v1",
    }
    return {
        "source_scope_id": scope.pk,
        "source_type": "gmail_thread",
        "external_id": f"gmail_thread:{thread_id}",
        "version_key": version_key(payload),
        "content_hash": payload["content_hash"],
        "classification": _classification(scope),
        "acl": acl,
        "chunks": chunks,
        "canonical_url": f"https://mail.google.com/mail/#all/{thread_id}",
        "title": title,
        "author_external_id": bounded_text(latest.from_address, 512),
        "source_created_at": earliest.internal_date,
        "source_updated_at": latest.internal_date,
        "occurred_at": latest.internal_date,
        "bounded_excerpt": normalized_text[:4096],
        "metadata": {
            "record_type": "gmail_thread",
            "thread_id": thread_id,
            "message_ids": [message.gmail_message_id for message in messages],
            "message_count": len(messages),
            "selected_label_ids": label_ids,
            "restricted_email": True,
            "authority_fields": [],
            "requires_review_for": [
                "external_commitment",
                "commercial_term",
                "contact_detail",
                "relationship_change",
            ],
        },
        "restore_access": bool(acl["is_accessible"]),
    }


def _attachment_external_id(attachment: GmailAttachmentArtifact) -> str:
    identity = canonical_hash(
        {
            "message_id": attachment.message_artifact.gmail_message_id,
            "part_id": attachment.part_id,
            "attachment_id": attachment.gmail_attachment_id,
        }
    )
    return f"gmail_attachment:{identity}"


def _eligible_attachments(configuration, *, thread_id=None):
    query = GmailAttachmentArtifact.objects.filter(
        organization=configuration.organization,
        message_artifact__google_connection=configuration.connection,
        message_artifact__memory_scope_artifacts__configuration=configuration,
        message_artifact__memory_scope_artifacts__lifecycle_state=GmailScopedArtifactState.ACTIVE,
        is_inline=False,
        extraction_status__in=(
            ArtifactProcessingStatus.HYDRATED,
            ArtifactProcessingStatus.PROCESSED,
        ),
    ).exclude(extracted_text="").select_related("message_artifact")
    if thread_id:
        query = query.filter(message_artifact__gmail_thread_id=thread_id)
    return query.distinct().order_by("pk")


def _attachment_record(configuration, attachment):
    mapping = (
        _active_mappings(configuration)
        .filter(message_artifact=attachment.message_artifact)
        .order_by("internal_date")
        .first()
    )
    if mapping is None or mapping.source_scope is None:
        return None
    scope = mapping.source_scope
    text = bounded_text(
        attachment.extracted_text,
        _setting("ORG_MEMORY_GMAIL_MAX_ATTACHMENT_CHARS", 100000, maximum=500000),
    )
    if not text:
        return None
    acl = _gmail_acl(
        configuration,
        scope,
        revision={
            "attachment_id": attachment.pk,
            "sha256": attachment.sha256,
            "updated_at": attachment.updated_at,
            "labels": mapping.selected_label_ids,
        },
    )
    external_id = _attachment_external_id(attachment)
    payload = {
        "content_hash": content_hash(text),
        "sha256": attachment.sha256,
        "updated_at": attachment.updated_at,
        "acl": acl,
        "adapter": "gmail-attachment-v1",
    }
    return {
        "source_scope_id": scope.pk,
        "source_type": "gmail_attachment",
        "external_id": external_id,
        "version_key": version_key(payload),
        "content_hash": payload["content_hash"],
        "classification": _classification(scope),
        "acl": acl,
        "chunks": (
            {
                "ordinal": 0,
                "chunk_kind": "gmail_attachment",
                "text": text,
                "token_count": estimate_tokens(text),
                "source_locator": {
                    "thread_id": attachment.message_artifact.gmail_thread_id,
                    "message_id": attachment.message_artifact.gmail_message_id,
                    "part_id": attachment.part_id,
                    "attachment_id_hash": canonical_hash(attachment.gmail_attachment_id),
                    "filename": bounded_text(attachment.filename, 500),
                },
                "occurred_at": attachment.message_artifact.internal_date,
            },
        ),
        "canonical_url": (
            f"https://mail.google.com/mail/#all/{attachment.message_artifact.gmail_thread_id}"
        ),
        "title": bounded_text(attachment.filename or "Gmail attachment", 512),
        "author_external_id": bounded_text(attachment.message_artifact.from_address, 512),
        "source_created_at": attachment.message_artifact.internal_date,
        "source_updated_at": attachment.updated_at,
        "occurred_at": attachment.message_artifact.internal_date,
        "bounded_excerpt": text[:4096],
        "metadata": {
            "record_type": "gmail_attachment",
            "thread_id": attachment.message_artifact.gmail_thread_id,
            "message_id": attachment.message_artifact.gmail_message_id,
            "filename": bounded_text(attachment.filename, 500),
            "mime_type": bounded_text(attachment.mime_type, 255),
            "selected_label_ids": mapping.selected_label_ids,
            "restricted_email": True,
            "requires_review_for": ["external_commitment", "commercial_term"],
        },
        "restore_access": bool(acl["is_accessible"]),
    }


def _records_for_threads(configuration, thread_ids):
    records = []
    for thread_id in sorted({str(value) for value in thread_ids if value}):
        record = _thread_record(configuration, thread_id)
        if record:
            records.append(record)
        for attachment in _eligible_attachments(configuration, thread_id=thread_id):
            attachment_record = _attachment_record(configuration, attachment)
            if attachment_record:
                records.append(attachment_record)
    return tuple(records)


def _expected_sources(configuration):
    expected = {
        ("gmail_thread", f"gmail_thread:{thread_id}")
        for thread_id in _active_mappings(configuration)
        .values_list("gmail_thread_id", flat=True)
        .distinct()
    }
    expected.update(
        ("gmail_attachment", _attachment_external_id(attachment))
        for attachment in _eligible_attachments(configuration)
    )
    return expected


def _access_removals(configuration):
    expected = _expected_sources(configuration)
    removals = []
    for source_type, external_id in (
        MemorySource.objects.filter(configuration=configuration)
        .exclude(lifecycle_state="tombstoned")
        .values_list("source_type", "external_id")
    ):
        if (str(source_type), str(external_id)) not in expected:
            removals.append(
                {
                    "source_type": str(source_type),
                    "external_id": str(external_id),
                    "reason": "gmail_label_removed_deleted_or_outside_approved_scope",
                    "revoke_access": True,
                }
            )
    return tuple(removals)


def _access_lost_page(configuration, *, cursor=None) -> SyncPage:
    now = timezone.now()
    GmailScopedMessageArtifact.objects.filter(
        configuration=configuration,
        lifecycle_state=GmailScopedArtifactState.ACTIVE,
    ).update(
        lifecycle_state=GmailScopedArtifactState.ACCESS_LOST,
        selected_label_ids=[],
        removed_at=now,
        last_seen_at=now,
        updated_at=now,
    )
    return SyncPage(
        records=(),
        removals=_access_removals(configuration),
        next_cursor=cursor,
        checkpoint={"mode": "access_lost", "reconciled_at": now.isoformat()},
        has_more=False,
    )


def _update_gmail_cursor(configuration, history_id, *, full_scan=False):
    now = timezone.now()
    latest = _active_mappings(configuration).aggregate(value=Max("internal_date"))["value"]
    defaults = {
        "last_history_id": str(history_id or ""),
        "last_synced_internal_date": now,
        "last_message_internal_date": latest,
    }
    if full_scan:
        defaults.update(
            {
                "backfill_window_start": _cutoff(configuration),
                "backfill_window_end": now,
                "backfill_completed_at": now,
            }
        )
    GmailSyncCursor.objects.update_or_create(
        organization=configuration.organization,
        google_connection=configuration.connection,
        defaults=defaults,
    )


def renew_gmail_watch(configuration, scopes, *, force=False):
    topic = str(getattr(settings, "ORG_MEMORY_GMAIL_PUBSUB_TOPIC", "") or "").strip()
    audience = str(getattr(settings, "ORG_MEMORY_GMAIL_PUBSUB_AUDIENCE", "") or "").strip()
    service_account = str(
        getattr(settings, "ORG_MEMORY_GMAIL_PUBSUB_SERVICE_ACCOUNT_EMAIL", "") or ""
    ).strip()
    if not topic or not audience or not service_account:
        return None
    now = timezone.now()
    watch = GmailMailboxWatch.objects.filter(configuration=configuration).first()
    renew_seconds = _setting("ORG_MEMORY_GMAIL_WATCH_RENEW_SECONDS", 86400, maximum=604800)
    labels = list(scopes)
    if (
        not force
        and watch
        and watch.status == GmailWatchStatus.ACTIVE
        and watch.last_renewed_at
        and watch.last_renewed_at > now - timedelta(seconds=renew_seconds)
        and watch.topic_name == topic
        and list(watch.label_ids or []) == labels
        and (
            watch.expiration_at is None
            or watch.expiration_at > now + timedelta(seconds=renew_seconds)
        )
    ):
        return watch
    try:
        response = watch_gmail_mailbox(
            configuration.connection,
            topic_name=topic,
            label_ids=labels,
        )
        raw_expiration = str(response.get("expiration") or "")
        expiration = None
        if raw_expiration.isdigit():
            expiration = datetime.fromtimestamp(
                int(raw_expiration) / 1000,
                tz=datetime_timezone.utc,
            )
        watch, _created = GmailMailboxWatch.objects.update_or_create(
            configuration=configuration,
            defaults={
                "email_address": configuration.connection.google_email,
                "topic_name": topic,
                "label_ids": labels,
                "history_id": str(response.get("historyId") or ""),
                "expiration_at": expiration,
                "status": GmailWatchStatus.ACTIVE,
                "last_renewed_at": now,
                "last_error": "",
            },
        )
        return watch
    except Exception as exc:
        safe_error = f"{exc.__class__.__name__}: {' '.join(str(exc).split())[:500]}"
        watch, _created = GmailMailboxWatch.objects.update_or_create(
            configuration=configuration,
            defaults={
                "email_address": configuration.connection.google_email,
                "topic_name": topic,
                "label_ids": labels,
                "status": GmailWatchStatus.ERROR,
                "last_renewed_at": now,
                "last_error": safe_error,
            },
        )
        return watch


class GmailMemoryConnector:
    provider = "gmail"

    def discover_scopes(self, configuration, cursor=None) -> ScopePage:
        if cursor:
            raise ValueError("Gmail label discovery is not paginated.")
        if not has_gmail_read_scope(configuration.connection):
            raise GmailProviderError("Gmail connection is missing the readonly Gmail scope.")
        labels = list_gmail_labels(configuration.connection)
        user_labels = [row for row in labels if str(row.get("type") or "").lower() == "user"]
        return ScopePage(
            scopes=tuple(
                ScopeDescriptor(
                    scope_type="label",
                    external_id=str(row.get("id") or ""),
                    name=bounded_text(row.get("name") or row.get("id"), 512),
                    canonical_url="https://mail.google.com/mail/#label/"
                    + str(row.get("name") or row.get("id") or ""),
                    metadata={
                        "label_type": "user",
                        "mailbox": str(configuration.connection.google_email or "")[:254],
                        "metadata_only": True,
                    },
                )
                for row in user_labels
                if str(row.get("id") or "").strip()
            ),
            warnings=(
                "Only explicit user-created labels are discoverable; system labels and unlabelled mail are excluded.",
            ),
        )

    def preview(self, configuration, selected_scopes, policy) -> SourcePreview:
        scopes = _selected_scope_map(configuration, selected_scopes)
        message_count = 0
        thread_ids = set()
        for message in GmailMessageArtifact.objects.filter(
            organization=configuration.organization,
            google_connection=configuration.connection,
        ).only("gmail_thread_id", "label_ids"):
            if set(str(value) for value in (message.label_ids or [])) & set(scopes):
                message_count += 1
                thread_ids.add(message.gmail_thread_id)
        return SourcePreview(
            summary={
                "mailbox": str(configuration.connection.google_email or "")[:254],
                "scope_count": len(scopes),
                "durable_matching_message_count": message_count,
                "durable_matching_thread_count": len(thread_ids),
                "record_count": None,
                "content_activated": False,
                "unlabelled_mail_included": False,
            },
            warnings=(
                "Email defaults to Executive classification when a scope is configured as internal.",
                "The authoritative count is established by the selected-label backfill.",
            ),
        )

    def dry_run(self, configuration, selected_scopes, policy) -> DryRunResult:
        scopes = _selected_scope_map(configuration, selected_scopes)
        samples = []
        for mapping in _active_mappings(configuration).order_by("internal_date")[:10]:
            samples.append(
                {
                    "source_type": "gmail_thread",
                    "external_id": f"gmail_thread:{mapping.gmail_thread_id}",
                    "selected_label_ids": mapping.selected_label_ids,
                }
            )
        return DryRunResult(
            summary={
                "scope_count": len(scopes),
                "sample_artifacts": len(samples),
                "samples": samples,
                "active_memory_created": False,
            },
            warnings=("Dry-run exposes identifiers and labels only; it creates no memory sources.",),
        )

    def _execute_full(self, configuration, scopes, state) -> SyncPage:
        labels = list(scopes)
        phase = str(state.get("phase") or "scan")
        scan_id = str(state.get("scan_id") or uuid.uuid4())
        if phase == "scan":
            index = max(int(state.get("label_index") or 0), 0)
            if index < len(labels):
                label_id = labels[index]
                page = list_label_message_page(
                    configuration.connection,
                    label_id=label_id,
                    query=_backfill_query(configuration),
                    page_token=str(state.get("page_token") or "") or None,
                    max_results=_page_size(),
                )
                for value in page.get("messages") or []:
                    message_id = str((value or {}).get("id") or "")
                    if not message_id:
                        continue
                    try:
                        payload = get_message_full(configuration.connection, message_id)
                    except Exception as exc:
                        if _is_not_found(exc):
                            _mark_mapping_removed(
                                configuration,
                                message_id,
                                GmailScopedArtifactState.DELETED,
                            )
                            continue
                        raise
                    _upsert_current_message(
                        configuration,
                        scopes,
                        payload,
                        scan_id=scan_id,
                    )
                next_token = str(page.get("nextPageToken") or "")
                next_state = {
                    **state,
                    "version": 1,
                    "mode": "full_scan",
                    "phase": "scan",
                    "scan_id": scan_id,
                    "label_index": index if next_token else index + 1,
                    "page_token": next_token,
                }
                if next_state["label_index"] >= len(labels) and not next_token:
                    next_state.update(phase="reconcile", page_token="")
                return SyncPage(
                    records=(),
                    next_cursor=_encode_state(next_state),
                    checkpoint=next_state,
                    has_more=True,
                )
            state = {**state, "phase": "reconcile", "scan_id": scan_id}
            phase = "reconcile"

        if phase == "reconcile":
            stale = list(
                _active_mappings(configuration)
                .exclude(scan_generation=scan_id)
                .order_by("updated_at")[: _page_size()]
            )
            affected_threads = []
            for mapping in stale:
                affected_threads.append(mapping.gmail_thread_id)
                mapping.lifecycle_state = GmailScopedArtifactState.LABEL_REMOVED
                mapping.selected_label_ids = []
                mapping.removed_at = timezone.now()
                mapping.save(
                    update_fields=(
                        "lifecycle_state",
                        "selected_label_ids",
                        "removed_at",
                        "updated_at",
                    )
                )
            records = _records_for_threads(configuration, affected_threads)
            if len(stale) >= _page_size():
                next_state = {**state, "phase": "reconcile", "scan_id": scan_id}
                return SyncPage(
                    records=records,
                    removals=_access_removals(configuration),
                    next_cursor=_encode_state(next_state),
                    checkpoint=next_state,
                    has_more=True,
                )
            state = {
                **state,
                "phase": "emit",
                "scan_id": scan_id,
                "last_thread_id": "",
            }
            if records:
                return SyncPage(
                    records=records,
                    removals=_access_removals(configuration),
                    next_cursor=_encode_state(state),
                    checkpoint=state,
                    has_more=True,
                )
            phase = "emit"

        if phase == "emit":
            last_thread_id = str(state.get("last_thread_id") or "")
            thread_ids = list(
                _active_mappings(configuration)
                .filter(gmail_thread_id__gt=last_thread_id)
                .order_by("gmail_thread_id")
                .values_list("gmail_thread_id", flat=True)
                .distinct()[: _page_size()]
            )
            records = _records_for_threads(configuration, thread_ids)
            if len(thread_ids) >= _page_size():
                next_state = {**state, "last_thread_id": str(thread_ids[-1])}
                return SyncPage(
                    records=records,
                    next_cursor=_encode_state(next_state),
                    checkpoint=next_state,
                    has_more=True,
                )
            profile = get_gmail_profile(configuration.connection)
            history_id = str(profile.get("historyId") or "")
            now = timezone.now()
            _update_gmail_cursor(configuration, history_id, full_scan=True)
            renew_gmail_watch(configuration, scopes)
            idle = {
                "version": 1,
                "mode": "idle",
                "history_id": history_id,
                "last_full_scan_at": now.isoformat(),
            }
            return SyncPage(
                records=records,
                removals=_access_removals(configuration),
                next_cursor=_encode_state(idle),
                checkpoint={
                    "mode": "completed",
                    "scan_id": scan_id,
                    "cursor_recovered": bool(state.get("cursor_recovered")),
                },
                has_more=False,
            )
        raise ValueError("Gmail full-scan phase is invalid.")

    def _execute_history(self, configuration, scopes, state) -> SyncPage:
        start_history_id = str(state.get("start_history_id") or "")
        if not start_history_id:
            return self._execute_full(configuration, scopes, _initial_full_state())
        try:
            page = list_history_page(
                configuration.connection,
                start_history_id=start_history_id,
                page_token=str(state.get("page_token") or "") or None,
                max_results=_page_size(),
                history_types=HISTORY_TYPES,
            )
        except StaleHistoryCursorError:
            recovered = _initial_full_state(last_full_scan_at=state.get("last_full_scan_at"))
            recovered["cursor_recovered"] = True
            return self._execute_full(configuration, scopes, recovered)

        message_events = {}
        for history in page.get("history") or []:
            if not isinstance(history, dict):
                continue
            for field in ("messagesAdded", "messagesDeleted", "labelsAdded", "labelsRemoved"):
                for item in history.get(field) or []:
                    message = item.get("message") if isinstance(item, dict) else {}
                    message_id = str((message or {}).get("id") or "")
                    if not message_id:
                        continue
                    event = message_events.setdefault(
                        message_id,
                        {
                            "deleted": False,
                            "labels_known": False,
                            "label_ids": [],
                        },
                    )
                    event["deleted"] = bool(
                        event["deleted"] or field == "messagesDeleted"
                    )
                    if "labelIds" in (message or {}):
                        event["labels_known"] = True
                        event["label_ids"] = [
                            str(value) for value in ((message or {}).get("labelIds") or [])
                        ]

        affected_threads = set()
        for message_id, event in message_events.items():
            if event["deleted"]:
                thread_id = _mark_mapping_removed(
                    configuration,
                    message_id,
                    GmailScopedArtifactState.DELETED,
                )
                if thread_id:
                    affected_threads.add(thread_id)
                continue
            if not event["labels_known"]:
                try:
                    metadata = get_message_metadata(configuration.connection, message_id)
                except Exception as exc:
                    if _is_not_found(exc):
                        thread_id = _mark_mapping_removed(
                            configuration,
                            message_id,
                            GmailScopedArtifactState.DELETED,
                        )
                        if thread_id:
                            affected_threads.add(thread_id)
                        continue
                    raise
                event["label_ids"] = [
                    str(value) for value in (metadata.get("labelIds") or [])
                ]
            if not (set(event["label_ids"]) & set(scopes)):
                thread_id = _mark_mapping_removed(
                    configuration,
                    message_id,
                    GmailScopedArtifactState.LABEL_REMOVED,
                )
                if thread_id:
                    affected_threads.add(thread_id)
                continue
            try:
                payload = get_message_full(configuration.connection, message_id)
            except Exception as exc:
                if _is_not_found(exc):
                    thread_id = _mark_mapping_removed(
                        configuration,
                        message_id,
                        GmailScopedArtifactState.DELETED,
                    )
                    if thread_id:
                        affected_threads.add(thread_id)
                    continue
                raise
            _mapping, thread_id = _upsert_current_message(configuration, scopes, payload)
            if thread_id:
                affected_threads.add(thread_id)

        records = _records_for_threads(configuration, affected_threads)
        next_token = str(page.get("nextPageToken") or "")
        if next_token:
            next_state = {**state, "page_token": next_token}
            return SyncPage(
                records=records,
                removals=_access_removals(configuration),
                next_cursor=_encode_state(next_state),
                checkpoint=next_state,
                has_more=True,
            )
        history_id = str(page.get("historyId") or start_history_id)
        _update_gmail_cursor(configuration, history_id)
        renew_gmail_watch(configuration, scopes)
        idle = {
            "version": 1,
            "mode": "idle",
            "history_id": history_id,
            "last_full_scan_at": str(state.get("last_full_scan_at") or ""),
        }
        return SyncPage(
            records=records,
            removals=_access_removals(configuration),
            next_cursor=_encode_state(idle),
            checkpoint={"mode": "completed", "history_id": history_id},
            has_more=False,
        )

    def backfill(self, configuration, selected_scopes, checkpoint) -> SyncPage:
        scopes = _selected_scope_map(configuration, selected_scopes)
        state = checkpoint if (checkpoint or {}).get("mode") == "full_scan" else _initial_full_state()
        return self._execute_full(configuration, scopes, state)

    def incremental_sync(self, configuration, cursor) -> SyncPage:
        if not _connection_ready(configuration):
            return _access_lost_page(configuration, cursor=cursor)
        scopes = _selected_scope_map(configuration)
        state = _decode_state(cursor)
        if state.get("mode") == "full_scan":
            return self._execute_full(configuration, scopes, state)
        if state.get("mode") == "history":
            return self._execute_history(configuration, scopes, state)
        last_full = parse_datetime(str(state.get("last_full_scan_at") or ""))
        full_interval = _setting("ORG_MEMORY_GMAIL_FULL_RECONCILE_SECONDS", 86400)
        if last_full is None or (timezone.now() - last_full).total_seconds() >= full_interval:
            return self._execute_full(
                configuration,
                scopes,
                _initial_full_state(last_full_scan_at=state.get("last_full_scan_at")),
            )
        return self._execute_history(
            configuration,
            scopes,
            _history_state(
                str(state.get("history_id") or ""),
                last_full_scan_at=state.get("last_full_scan_at"),
            ),
        )

    def refresh_permissions(self, configuration, checkpoint) -> SyncPage:
        if not _connection_ready(configuration):
            return _access_lost_page(
                configuration,
                cursor=configuration.sync_cursor or None,
            )
        scopes = _selected_scope_map(configuration)
        state = checkpoint if (checkpoint or {}).get("mode") == "full_scan" else _initial_full_state()
        page = self._execute_full(configuration, scopes, state)
        return replace(page, next_cursor=None)

    def fetch_version(self, configuration, external_id) -> SourceVersionPayload:
        raw = str(external_id or "")
        record = None
        if raw.startswith("gmail_thread:"):
            record = _thread_record(configuration, raw[len("gmail_thread:") :])
        elif raw.startswith("gmail_attachment:"):
            record = next(
                (
                    _attachment_record(configuration, attachment)
                    for attachment in _eligible_attachments(configuration)
                    if _attachment_external_id(attachment) == raw
                ),
                None,
            )
        if record is None:
            raise ValueError("Gmail source is outside the active selected-label inventory.")
        return SourceVersionPayload(
            external_id=record["external_id"],
            canonical_url=record["canonical_url"],
            version_key=record["version_key"],
            source_times={
                "created_at": record["source_created_at"],
                "modified_at": record["source_updated_at"],
            },
            metadata=record["metadata"],
            acl=record["acl"],
            content=record["bounded_excerpt"],
        )

    def tombstone_missing(self, configuration, sync_run) -> TombstoneResult:
        removals = _access_removals(configuration)
        return TombstoneResult(
            tombstoned_external_ids=tuple(row["external_id"] for row in removals)
        )

    def health(self, configuration) -> ConnectorHealth:
        last_sync = configuration.last_successful_sync_at
        lag = max(int((timezone.now() - last_sync).total_seconds()), 0) if last_sync else None
        watch = GmailMailboxWatch.objects.filter(configuration=configuration).first()
        watch_status = "disabled"
        if watch:
            watch_status = watch.status
            if watch.expiration_at and watch.expiration_at <= timezone.now():
                watch_status = GmailWatchStatus.EXPIRED
        return ConnectorHealth(
            status=configuration.lifecycle_state,
            credential_status=(
                "connected"
                if has_gmail_read_scope(configuration.connection)
                and str(configuration.connection.refresh_token or "").strip()
                else "error"
            ),
            last_successful_sync_at=last_sync.isoformat() if last_sync else None,
            source_lag_seconds=lag,
            details={
                "mailbox": str(configuration.connection.google_email or "")[:254],
                "active_scoped_messages": _active_mappings(configuration).count(),
                "active_threads": _active_mappings(configuration)
                .values("gmail_thread_id")
                .distinct()
                .count(),
                "watch_status": watch_status,
                "watch_expiration_at": watch.expiration_at.isoformat()
                if watch and watch.expiration_at
                else None,
                "watch_last_error": watch.last_error if watch else "",
                "daily_fallback_seconds": int(
                    getattr(settings, "ORG_MEMORY_GMAIL_FULL_RECONCILE_SECONDS", 86400)
                ),
            },
        )
