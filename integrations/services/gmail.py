import base64
import datetime
import hashlib
import html
import logging
import re
import ssl
import time
from email.utils import getaddresses
from typing import Iterable, Optional

import httplib2
from django.conf import settings
from django.utils import timezone
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from integrations.models import (
    ArtifactProcessingStatus,
    GmailAttachmentArtifact,
    GmailMessageArtifact,
    GmailThreadArtifact,
    GoogleConnection,
)
from integrations.services.startup_updates import (
    DEFAULT_ATTACHMENT_BYTES_LIMIT,
    DEFAULT_BACKFILL_MONTHS,
    apply_profile_scoring,
)


logger = logging.getLogger(__name__)

METADATA_HEADERS = [
    "Subject",
    "From",
    "To",
    "Cc",
    "Bcc",
    "Reply-To",
    "Date",
    "Message-ID",
    "In-Reply-To",
    "References",
    "List-Id",
    "List-Unsubscribe",
    "Precedence",
    "Auto-Submitted",
]
REPLY_DELIMITER_PATTERNS = [
    re.compile(r"^on .+wrote:$", re.IGNORECASE),
    re.compile(r"^from:\s", re.IGNORECASE),
    re.compile(r"^sent:\s", re.IGNORECASE),
    re.compile(r"^subject:\s", re.IGNORECASE),
    re.compile(r"^to:\s", re.IGNORECASE),
]
SUPPORTED_ATTACHMENT_MIME_PREFIXES = (
    "text/",
    "image/",
)
SUPPORTED_ATTACHMENT_MIME_TYPES = {
    "application/pdf",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/rtf",
    "application/vnd.oasis.opendocument.text",
    "application/vnd.ms-powerpoint",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "application/vnd.ms-excel",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}


class StaleHistoryCursorError(Exception):
    pass


GMAIL_API_MAX_ATTEMPTS = 3
GMAIL_API_RETRYABLE_HTTP_STATUSES = {408, 429, 500, 502, 503, 504}
GMAIL_API_RETRYABLE_EXCEPTIONS = (
    ssl.SSLError,
    TimeoutError,
    httplib2.HttpLib2Error,
)


def get_refreshed_credentials(connection: GoogleConnection):
    """
    Constructs a Credentials object from the stored refresh_token.
    The library handles the refresh flow automatically when requests are made
    if we provide the token_uri, client_id, and client_secret.
    """
    creds = Credentials(
        token=None,
        refresh_token=connection.refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=settings.GOOGLE_OAUTH_CLIENT_ID,
        client_secret=settings.GOOGLE_OAUTH_CLIENT_SECRET,
        scopes=connection.scope.split(" ") if connection.scope else [],
    )
    return creds


def build_gmail_service(connection: GoogleConnection, *, cache_discovery: bool = False):
    creds = get_refreshed_credentials(connection)
    return build("gmail", "v1", credentials=creds, cache_discovery=cache_discovery)


def build_backfill_query(*, after_dt, before_dt) -> str:
    after_ts = int(after_dt.timestamp())
    before_ts = int(before_dt.timestamp())
    return (
        f"after:{after_ts} before:{before_ts} "
        "-in:spam -in:trash -category:promotions -category:social -category:forums"
    )


def default_backfill_window(
    *,
    now=None,
    window_months: int = DEFAULT_BACKFILL_MONTHS,
) -> tuple[datetime.datetime, datetime.datetime]:
    now = now or timezone.now()
    start = now - datetime.timedelta(days=30 * int(window_months))
    return start, now


def six_month_backfill_window(*, now=None) -> tuple[datetime.datetime, datetime.datetime]:
    return default_backfill_window(now=now, window_months=6)


def _execute_gmail_request(request_factory, *, description: str):
    for attempt in range(1, GMAIL_API_MAX_ATTEMPTS + 1):
        try:
            return request_factory().execute()
        except HttpError as exc:
            status_code = getattr(getattr(exc, "resp", None), "status", None)
            if status_code not in GMAIL_API_RETRYABLE_HTTP_STATUSES or attempt >= GMAIL_API_MAX_ATTEMPTS:
                raise
            logger.warning(
                "Retrying Gmail API request %s after HTTP %s (%s/%s).",
                description,
                status_code,
                attempt,
                GMAIL_API_MAX_ATTEMPTS,
            )
        except GMAIL_API_RETRYABLE_EXCEPTIONS as exc:
            if attempt >= GMAIL_API_MAX_ATTEMPTS:
                raise
            logger.warning(
                "Retrying Gmail API request %s after %s (%s/%s).",
                description,
                exc.__class__.__name__,
                attempt,
                GMAIL_API_MAX_ATTEMPTS,
            )
        time.sleep(min(2 ** (attempt - 1), 4))


def list_message_page(
    connection: GoogleConnection,
    *,
    query: str,
    page_token: Optional[str] = None,
    max_results: int = 500,
) -> dict:
    service = build_gmail_service(connection, cache_discovery=False)
    return _execute_gmail_request(
        lambda: (
            service.users()
            .messages()
            .list(
                userId="me",
                q=query,
                pageToken=page_token,
                maxResults=max_results,
                includeSpamTrash=False,
            )
        )
        ,
        description="messages.list",
    )


def get_message_metadata(connection: GoogleConnection, message_id: str) -> dict:
    service = build_gmail_service(connection, cache_discovery=False)
    return _execute_gmail_request(
        lambda: (
            service.users()
            .messages()
            .get(
                userId="me",
                id=message_id,
                format="metadata",
                metadataHeaders=METADATA_HEADERS,
            )
        )
        ,
        description=f"messages.get.metadata:{message_id}",
    )


def get_message_full(connection: GoogleConnection, message_id: str) -> dict:
    service = build_gmail_service(connection, cache_discovery=False)
    return _execute_gmail_request(
        lambda: service.users().messages().get(userId="me", id=message_id, format="full"),
        description=f"messages.get.full:{message_id}",
    )


def get_thread_full(connection: GoogleConnection, thread_id: str) -> dict:
    service = build_gmail_service(connection, cache_discovery=False)
    return _execute_gmail_request(
        lambda: service.users().threads().get(userId="me", id=thread_id, format="full"),
        description=f"threads.get.full:{thread_id}",
    )


def get_attachment_payload(connection: GoogleConnection, message_id: str, attachment_id: str) -> dict:
    service = build_gmail_service(connection, cache_discovery=False)
    return _execute_gmail_request(
        lambda: (
            service.users()
            .messages()
            .attachments()
            .get(userId="me", messageId=message_id, id=attachment_id)
        ),
        description=f"messages.attachments.get:{message_id}:{attachment_id}",
    )


def list_history_page(
    connection: GoogleConnection,
    *,
    start_history_id: str,
    page_token: Optional[str] = None,
    max_results: int = 250,
) -> dict:
    service = build_gmail_service(connection, cache_discovery=False)
    try:
        return _execute_gmail_request(
            lambda: (
                service.users()
                .history()
                .list(
                    userId="me",
                    startHistoryId=start_history_id,
                    pageToken=page_token,
                    maxResults=max_results,
                    historyTypes=["messageAdded"],
                )
            )
            ,
            description=f"history.list:{start_history_id}",
        )
    except HttpError as exc:
        status_code = getattr(getattr(exc, "resp", None), "status", None)
        if status_code == 404:
            raise StaleHistoryCursorError("Gmail history cursor is stale.") from exc
        raise


def _decode_base64url(value: str) -> bytes:
    if not value:
        return b""
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode((value + padding).encode("utf-8"))


def _encode_bytes(value: bytes) -> str:
    if not value:
        return ""
    return base64.b64encode(value).decode("utf-8")


def _strip_html(raw_html: str) -> str:
    if not raw_html:
        return ""
    text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", raw_html)
    text = re.sub(r"(?s)<br\s*/?>", "\n", text)
    text = re.sub(r"(?s)</p\s*>", "\n", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    return html.unescape(text)


def clean_email_text(raw_text: str) -> str:
    if not raw_text:
        return ""
    normalized = raw_text.replace("\r\n", "\n").replace("\r", "\n")
    lines = []
    for line in normalized.split("\n"):
        stripped = line.strip()
        if stripped.startswith(">"):
            continue
        if any(pattern.match(stripped) for pattern in REPLY_DELIMITER_PATTERNS):
            break
        if stripped == "--":
            break
        lines.append(line.rstrip())

    cleaned = "\n".join(lines)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def _header_map(headers: Iterable[dict]) -> dict:
    values = {}
    for header in headers or []:
        name = str(header.get("name") or "").strip()
        value = str(header.get("value") or "").strip()
        if not name:
            continue
        values[name.lower()] = value
    return values


def _extract_addresses(header_value: str) -> list[str]:
    parsed = []
    for display_name, email_address in getaddresses([header_value or ""]):
        candidate = email_address or display_name
        candidate = str(candidate or "").strip()
        if candidate:
            parsed.append(candidate.lower())
    return parsed


def _walk_parts(part: Optional[dict]) -> Iterable[dict]:
    if not part:
        return []
    parts = [part]
    walked = []
    while parts:
        current = parts.pop(0)
        walked.append(current)
        for child in current.get("parts", []) or []:
            parts.append(child)
    return walked


def _extract_attachment_manifest(payload: dict) -> list[dict]:
    manifest = []
    for part in _walk_parts(payload):
        filename = str(part.get("filename") or "").strip()
        body = part.get("body") or {}
        attachment_id = str(body.get("attachmentId") or "").strip()
        if not filename and not attachment_id:
            continue
        headers = _header_map(part.get("headers") or [])
        manifest.append(
            {
                "part_id": str(part.get("partId") or "").strip(),
                "filename": filename,
                "mime_type": str(part.get("mimeType") or "").strip(),
                "attachment_id": attachment_id,
                "size_bytes": int(body.get("size") or 0),
                "content_disposition": headers.get("content-disposition", ""),
            }
        )
    return manifest


def _extract_text_from_payload(payload: dict) -> str:
    text_parts = []
    html_parts = []
    for part in _walk_parts(payload):
        mime_type = str(part.get("mimeType") or "").lower()
        body = part.get("body") or {}
        data = body.get("data")
        if not data:
            continue
        decoded = _decode_base64url(data).decode("utf-8", errors="ignore")
        if mime_type == "text/plain":
            text_parts.append(decoded)
        elif mime_type == "text/html":
            html_parts.append(decoded)

    joined_text = "\n".join(item for item in text_parts if item).strip()
    if joined_text:
        return clean_email_text(joined_text)

    joined_html = "\n".join(item for item in html_parts if item).strip()
    return clean_email_text(_strip_html(joined_html))


def _extract_message_summary(message_data: dict) -> dict:
    payload = message_data.get("payload") or {}
    header_values = _header_map(payload.get("headers") or [])
    cleaned_text = _extract_text_from_payload(payload)
    body_preview = cleaned_text[:1000]
    internal_date_ms = int(message_data.get("internalDate") or 0)
    internal_date = datetime.datetime.fromtimestamp(
        internal_date_ms / 1000.0,
        tz=datetime.timezone.utc,
    )
    return {
        "gmail_thread_id": str(message_data.get("threadId") or "").strip(),
        "history_id": str(message_data.get("historyId") or "").strip(),
        "internal_date": internal_date,
        "subject": header_values.get("subject", ""),
        "from_address": (_extract_addresses(header_values.get("from", "")) or [header_values.get("from", "")])[0],
        "to_addresses": _extract_addresses(header_values.get("to", "")),
        "cc_addresses": _extract_addresses(header_values.get("cc", "")),
        "bcc_addresses": _extract_addresses(header_values.get("bcc", "")),
        "reply_to_addresses": _extract_addresses(header_values.get("reply-to", "")),
        "label_ids": message_data.get("labelIds") or [],
        "header_values": header_values,
        "snippet": str(message_data.get("snippet") or "").strip(),
        "cleaned_text": cleaned_text,
        "body_preview": body_preview,
        "attachment_manifest": _extract_attachment_manifest(payload),
    }


def upsert_message_artifact_from_message_data(*, organization, connection: GoogleConnection, message_data: dict, profile=None) -> GmailMessageArtifact:
    summary = _extract_message_summary(message_data)
    message_id = str(message_data.get("id") or "").strip()
    artifact, _ = GmailMessageArtifact.objects.update_or_create(
        organization=organization,
        google_connection=connection,
        gmail_message_id=message_id,
        defaults={
            **summary,
            "has_attachments": bool(summary["attachment_manifest"]),
            "metadata_hydrated_at": timezone.now(),
        },
    )

    if profile is not None:
        apply_profile_scoring(profile, artifact)

    return artifact


def _existing_metadata_artifact_map(
    *,
    organization,
    connection: GoogleConnection,
    message_ids: Iterable[str],
) -> dict[str, GmailMessageArtifact]:
    normalized_ids = [str(item or "").strip() for item in message_ids if str(item or "").strip()]
    if not normalized_ids:
        return {}

    queryset = GmailMessageArtifact.objects.filter(
        organization=organization,
        google_connection=connection,
        gmail_message_id__in=normalized_ids,
        metadata_hydrated_at__isnull=False,
    )
    return {artifact.gmail_message_id: artifact for artifact in queryset}


def sync_message_metadata_page(
    *,
    organization,
    connection: GoogleConnection,
    profile=None,
    after_dt,
    before_dt,
    page_token: Optional[str] = None,
    max_results: int = 500,
) -> dict:
    query = build_backfill_query(after_dt=after_dt, before_dt=before_dt)
    page = list_message_page(connection, query=query, page_token=page_token, max_results=max_results)
    message_ids = [
        str(item.get("id") or "").strip()
        for item in (page.get("messages", []) or [])
        if str(item.get("id") or "").strip()
    ]
    existing_artifacts = _existing_metadata_artifact_map(
        organization=organization,
        connection=connection,
        message_ids=message_ids,
    )
    artifacts = []
    reused_existing_count = 0
    for message_id in message_ids:
        existing_artifact = existing_artifacts.get(message_id)
        if existing_artifact is not None:
            artifacts.append(existing_artifact)
            reused_existing_count += 1
            continue

        metadata = get_message_metadata(connection, message_id)
        artifacts.append(
            upsert_message_artifact_from_message_data(
                organization=organization,
                connection=connection,
                message_data=metadata,
                profile=profile,
            )
        )
    return {
        "query": query,
        "mode": "backfill",
        "result_size_estimate": int(page.get("resultSizeEstimate") or 0),
        "next_page_token": page.get("nextPageToken"),
        "reused_existing_count": reused_existing_count,
        "artifacts": artifacts,
    }


def sync_history_metadata_page(
    *,
    organization,
    connection: GoogleConnection,
    profile=None,
    start_history_id: str,
    page_token: Optional[str] = None,
    max_results: int = 250,
) -> dict:
    page = list_history_page(
        connection,
        start_history_id=start_history_id,
        page_token=page_token,
        max_results=max_results,
    )

    message_ids = []
    seen_ids = set()
    for history_item in page.get("history", []) or []:
        for message_added in history_item.get("messagesAdded", []) or []:
            message = message_added.get("message") or {}
            message_id = str(message.get("id") or "").strip()
            if not message_id or message_id in seen_ids:
                continue
            seen_ids.add(message_id)
            message_ids.append(message_id)

    existing_artifacts = _existing_metadata_artifact_map(
        organization=organization,
        connection=connection,
        message_ids=message_ids,
    )
    artifacts = []
    reused_existing_count = 0
    for message_id in message_ids:
        existing_artifact = existing_artifacts.get(message_id)
        if existing_artifact is not None:
            artifacts.append(existing_artifact)
            reused_existing_count += 1
            continue

        metadata = get_message_metadata(connection, message_id)
        artifacts.append(
            upsert_message_artifact_from_message_data(
                organization=organization,
                connection=connection,
                message_data=metadata,
                profile=profile,
            )
        )

    return {
        "mode": "incremental",
        "start_history_id": start_history_id,
        "history_id": str(page.get("historyId") or "").strip(),
        "result_size_estimate": len(message_ids),
        "next_page_token": page.get("nextPageToken"),
        "reused_existing_count": reused_existing_count,
        "artifacts": artifacts,
    }


def _attachment_supported(mime_type: str) -> bool:
    mime_type = str(mime_type or "").lower()
    if not mime_type:
        return False
    if mime_type.startswith(SUPPORTED_ATTACHMENT_MIME_PREFIXES):
        return True
    return mime_type in SUPPORTED_ATTACHMENT_MIME_TYPES


def _hydrate_attachment(
    *,
    organization,
    connection: GoogleConnection,
    thread_artifact: GmailThreadArtifact,
    message_artifact: GmailMessageArtifact,
    manifest_item: dict,
    size_limit_bytes: int = DEFAULT_ATTACHMENT_BYTES_LIMIT,
) -> GmailAttachmentArtifact:
    attachment_id = str(manifest_item.get("attachment_id") or "").strip()
    part_id = str(manifest_item.get("part_id") or "").strip()
    mime_type = str(manifest_item.get("mime_type") or "").strip()
    filename = str(manifest_item.get("filename") or "").strip()
    content_disposition = str(manifest_item.get("content_disposition") or "").strip()
    size_bytes = int(manifest_item.get("size_bytes") or 0)

    defaults = {
        "organization": organization,
        "thread_artifact": thread_artifact,
        "mime_type": mime_type,
        "filename": filename,
        "content_disposition": content_disposition,
        "size_bytes": size_bytes,
        "is_inline": "inline" in content_disposition.lower(),
        "metadata": manifest_item,
        "hydrated_at": timezone.now(),
    }

    if not _attachment_supported(mime_type):
        defaults["extraction_status"] = ArtifactProcessingStatus.UNSUPPORTED
        defaults["parse_notes"] = "unsupported_mime_type"
        attachment, _ = GmailAttachmentArtifact.objects.update_or_create(
            message_artifact=message_artifact,
            part_id=part_id,
            gmail_attachment_id=attachment_id,
            defaults=defaults,
        )
        return attachment

    if size_bytes > size_limit_bytes:
        defaults["extraction_status"] = ArtifactProcessingStatus.UNSUPPORTED
        defaults["parse_notes"] = "attachment_too_large"
        attachment, _ = GmailAttachmentArtifact.objects.update_or_create(
            message_artifact=message_artifact,
            part_id=part_id,
            gmail_attachment_id=attachment_id,
            defaults=defaults,
        )
        return attachment

    raw_bytes = b""
    if attachment_id:
        payload = get_attachment_payload(connection, message_artifact.gmail_message_id, attachment_id)
        raw_bytes = _decode_base64url(str(payload.get("data") or ""))

    defaults["raw_content_base64"] = _encode_bytes(raw_bytes)
    defaults["sha256"] = hashlib.sha256(raw_bytes).hexdigest() if raw_bytes else ""
    defaults["extraction_status"] = ArtifactProcessingStatus.HYDRATED
    attachment, _ = GmailAttachmentArtifact.objects.update_or_create(
        message_artifact=message_artifact,
        part_id=part_id,
        gmail_attachment_id=attachment_id,
        defaults=defaults,
    )
    return attachment


def hydrate_thread_artifact(
    *,
    organization,
    connection: GoogleConnection,
    thread_id: str,
    profile=None,
    fetch_attachments: bool = True,
    size_limit_bytes: int = DEFAULT_ATTACHMENT_BYTES_LIMIT,
) -> GmailThreadArtifact:
    thread_data = get_thread_full(connection, thread_id)
    messages = thread_data.get("messages", []) or []
    payloads = []
    source_message_ids = []
    latest_internal_date = None

    thread_artifact, _ = GmailThreadArtifact.objects.get_or_create(
        organization=organization,
        google_connection=connection,
        gmail_thread_id=thread_id,
    )
    attachment_ids = [] if fetch_attachments else list(thread_artifact.attachment_ids or [])

    for message_data in messages:
        artifact = upsert_message_artifact_from_message_data(
            organization=organization,
            connection=connection,
            message_data=message_data,
            profile=profile,
        )
        source_message_ids.append(artifact.gmail_message_id)
        payloads.append(
            {
                "message_id": artifact.gmail_message_id,
                "internal_date": artifact.internal_date.isoformat(),
                "subject": artifact.subject,
                "from_address": artifact.from_address,
                "snippet": artifact.snippet,
                "cleaned_text": artifact.cleaned_text,
                "attachment_manifest": artifact.attachment_manifest or [],
            }
        )
        if latest_internal_date is None or artifact.internal_date > latest_internal_date:
            latest_internal_date = artifact.internal_date

        if fetch_attachments:
            for manifest_item in artifact.attachment_manifest or []:
                attachment = _hydrate_attachment(
                    organization=organization,
                    connection=connection,
                    thread_artifact=thread_artifact,
                    message_artifact=artifact,
                    manifest_item=manifest_item,
                    size_limit_bytes=size_limit_bytes,
                )
                attachment_ids.append(attachment.id)

    payloads.sort(key=lambda item: item["internal_date"])
    cleaned_text = "\n\n".join(
        item["cleaned_text"] for item in payloads if str(item.get("cleaned_text") or "").strip()
    )

    participant_summary = {
        "senders": list({item["from_address"] for item in payloads if item.get("from_address")}),
        "subjects": list({item["subject"] for item in payloads if item.get("subject")}),
    }

    thread_artifact.source_message_ids = source_message_ids
    thread_artifact.message_payloads = payloads
    thread_artifact.participant_summary = participant_summary
    thread_artifact.cleaned_text = cleaned_text
    thread_artifact.attachment_ids = attachment_ids
    thread_artifact.source_message_count = len(source_message_ids)
    thread_artifact.latest_message_internal_date = latest_internal_date
    thread_artifact.hydration_status = ArtifactProcessingStatus.HYDRATED
    thread_artifact.hydrated_at = timezone.now()
    thread_artifact.last_error = ""
    thread_artifact.save(
        update_fields=[
            "source_message_ids",
            "message_payloads",
            "participant_summary",
            "cleaned_text",
            "attachment_ids",
            "source_message_count",
            "latest_message_internal_date",
            "hydration_status",
            "hydrated_at",
            "last_error",
            "updated_at",
        ]
    )
    return thread_artifact


def ensure_thread_attachments_hydrated(
    *,
    organization,
    connection: GoogleConnection,
    thread_artifact: GmailThreadArtifact,
    size_limit_bytes: int = DEFAULT_ATTACHMENT_BYTES_LIMIT,
) -> list[GmailAttachmentArtifact]:
    source_message_ids = [
        str(message_id or "").strip()
        for message_id in (
            thread_artifact.source_message_ids
            or [item.get("message_id") for item in (thread_artifact.message_payloads or [])]
        )
        if str(message_id or "").strip()
    ]
    if not source_message_ids:
        return list(
            GmailAttachmentArtifact.objects.filter(thread_artifact=thread_artifact).order_by("id")
        )

    message_artifacts = list(
        GmailMessageArtifact.objects.filter(
            organization=organization,
            google_connection=connection,
            gmail_message_id__in=source_message_ids,
        ).order_by("internal_date", "id")
    )
    existing_attachments = {
        (
            attachment.message_artifact_id,
            attachment.part_id,
            attachment.gmail_attachment_id,
        ): attachment
        for attachment in GmailAttachmentArtifact.objects.filter(message_artifact__in=message_artifacts)
    }
    existing_attachments_by_message: dict[int, list[GmailAttachmentArtifact]] = {}
    for attachment in existing_attachments.values():
        existing_attachments_by_message.setdefault(attachment.message_artifact_id, []).append(attachment)

    hydrated_attachments: list[GmailAttachmentArtifact] = []
    attachment_ids: list[int] = []
    seen_attachment_ids: set[int] = set()
    for message_artifact in message_artifacts:
        manifest_items = message_artifact.attachment_manifest or []
        for manifest_item in manifest_items:
            part_id = str(manifest_item.get("part_id") or "").strip()
            attachment_id = str(manifest_item.get("attachment_id") or "").strip()
            key = (message_artifact.id, part_id, attachment_id)
            attachment = existing_attachments.get(key)
            if attachment is None:
                attachment = _hydrate_attachment(
                    organization=organization,
                    connection=connection,
                    thread_artifact=thread_artifact,
                    message_artifact=message_artifact,
                    manifest_item=manifest_item,
                    size_limit_bytes=size_limit_bytes,
                )
                existing_attachments[key] = attachment
            elif attachment.thread_artifact_id != thread_artifact.id:
                attachment.thread_artifact = thread_artifact
                attachment.save(update_fields=["thread_artifact", "updated_at"])

            if attachment.id not in seen_attachment_ids:
                hydrated_attachments.append(attachment)
                attachment_ids.append(attachment.id)
                seen_attachment_ids.add(attachment.id)

        if manifest_items:
            continue

        for attachment in existing_attachments_by_message.get(message_artifact.id, []):
            if attachment.thread_artifact_id != thread_artifact.id:
                attachment.thread_artifact = thread_artifact
                attachment.save(update_fields=["thread_artifact", "updated_at"])
            if attachment.id not in seen_attachment_ids:
                hydrated_attachments.append(attachment)
                attachment_ids.append(attachment.id)
                seen_attachment_ids.add(attachment.id)

    if thread_artifact.attachment_ids != attachment_ids:
        thread_artifact.attachment_ids = attachment_ids
        thread_artifact.save(update_fields=["attachment_ids", "updated_at"])

    return hydrated_attachments


def fetch_last_month_emails(connection: GoogleConnection):
    """
    Crawls Gmail for the last 30 days of messages.
    Returns a list of full message objects.
    """
    service = build_gmail_service(connection, cache_discovery=False)
    query = "newer_than:30d -label:TRASH -label:SPAM"

    messages = []
    page_token = None
    while True:
        results = service.users().messages().list(
            userId="me",
            q=query,
            pageToken=page_token,
            maxResults=500,
        ).execute()

        messages.extend(results.get("messages", []))
        page_token = results.get("nextPageToken")
        if not page_token:
            break

    detailed_messages = []
    for msg in messages:
        try:
            full_msg = service.users().messages().get(
                userId="me",
                id=msg["id"],
                format="full",
            ).execute()
            detailed_messages.append(full_msg)
        except Exception as exc:
            print(f"Failed to fetch message {msg['id']}: {exc}")

    return detailed_messages


def fetch_recent_subject_lines(user, days=30):
    try:
        conn = user.google_connection
    except GoogleConnection.DoesNotExist:
        return []

    service = build_gmail_service(conn, cache_discovery=False)
    query = f"newer_than:{days}d -in:spam -in:trash"

    results = service.users().messages().list(userId="me", q=query, maxResults=50).execute()
    messages = results.get("messages", [])

    subjects = []
    for msg in messages:
        try:
            metadata = get_message_metadata(conn, msg["id"])
            header_values = _header_map((metadata.get("payload") or {}).get("headers") or [])
            subject = header_values.get("subject") or "(No Subject)"
            subjects.append(subject)
        except Exception as exc:
            print(f"Failed to fetch message {msg['id']}: {exc}")

    return subjects
