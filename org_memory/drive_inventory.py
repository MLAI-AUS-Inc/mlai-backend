from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from collections import Counter, defaultdict, deque
from dataclasses import dataclass
from datetime import date, datetime, time as datetime_time, timedelta, timezone as datetime_timezone
from pathlib import PurePath
from typing import Any, Callable, Iterable, Mapping, Protocol
from zoneinfo import ZoneInfo

from django.conf import settings
from django.utils import timezone


FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"
SHORTCUT_MIME_TYPE = "application/vnd.google-apps.shortcut"
GOOGLE_DOC_MIME_TYPE = "application/vnd.google-apps.document"
DOCX_MIME_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
PDF_MIME_TYPE = "application/pdf"
TEXT_MIME_TYPE = "text/plain"
MARKDOWN_MIME_TYPE = "text/markdown"
VTT_MIME_TYPE = "text/vtt"
SRT_MIME_TYPES = frozenset({"application/x-subrip", "text/srt"})

DEFAULT_ALLOWED_MIME_TYPES = frozenset(
    {
        GOOGLE_DOC_MIME_TYPE,
        DOCX_MIME_TYPE,
        PDF_MIME_TYPE,
        TEXT_MIME_TYPE,
        MARKDOWN_MIME_TYPE,
        VTT_MIME_TYPE,
        *SRT_MIME_TYPES,
    }
)
DEFAULT_ALLOWED_EXTENSIONS = frozenset({".docx", ".pdf", ".txt", ".md", ".vtt", ".srt"})

REQUIRED_DRIVE_SCOPES = frozenset(
    {
        "https://www.googleapis.com/auth/drive.readonly",
        "https://www.googleapis.com/auth/drive.metadata.readonly",
    }
)

DRIVE_FILE_FIELDS = ",".join(
    (
        "id",
        "name",
        "mimeType",
        "size",
        "createdTime",
        "modifiedTime",
        "version",
        "md5Checksum",
        "sha1Checksum",
        "sha256Checksum",
        "trashed",
        "driveId",
        "parents",
        "ownedByMe",
        "shared",
        "permissionIds",
        "capabilities(canDownload)",
        "shortcutDetails(targetId,targetMimeType)",
        "fileExtension",
        "webViewLink",
    )
)

DRIVE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{3,256}$")
TRANSCRIPT_HINT_PATTERN = re.compile(
    r"\b(transcript|minutes|meeting|stand[ -]?up|town[ -]?hall|all[ -]?hands|retro|retrospective|"
    r"workshop|committee|board|sync|catch[ -]?up|otter|fireflies|granola)\b",
    re.IGNORECASE,
)
COPY_SUFFIX_PATTERN = re.compile(r"(?:\s+-\s+copy|\s+copy|\s*\(\d+\))$", re.IGNORECASE)


class DriveInventoryError(RuntimeError):
    """Raised when an inventory cannot be completed safely."""


class DriveMetadataClientProtocol(Protocol):
    def get_file(self, file_id: str) -> Mapping[str, Any]: ...

    def list_children(
        self,
        folder_id: str,
        *,
        modified_after_rfc3339: str,
        page_token: str | None,
        page_size: int,
    ) -> tuple[list[Mapping[str, Any]], str | None]: ...


@dataclass(frozen=True)
class DriveInventoryLimits:
    max_files: int = 10_000
    max_pages: int = 1_000
    max_seconds: int = 300

    def validate(self) -> None:
        for name, value in (
            ("max_files", self.max_files),
            ("max_pages", self.max_pages),
            ("max_seconds", self.max_seconds),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise DriveInventoryError(f"{name} must be a positive integer.")


class GoogleDriveMetadataClient:
    """Small metadata-only adapter over the Google Drive v3 client."""

    def __init__(self, service):
        self.service = service

    def get_file(self, file_id: str) -> Mapping[str, Any]:
        validate_drive_id(file_id)
        return (
            self.service.files()
            .get(
                fileId=file_id,
                supportsAllDrives=True,
                fields=DRIVE_FILE_FIELDS,
            )
            .execute(num_retries=2)
        )

    def list_children(
        self,
        folder_id: str,
        *,
        modified_after_rfc3339: str,
        page_token: str | None,
        page_size: int,
    ) -> tuple[list[Mapping[str, Any]], str | None]:
        validate_drive_id(folder_id)
        if page_size <= 0 or page_size > 1_000:
            raise DriveInventoryError("Drive page_size must be between 1 and 1000.")

        query = (
            f"'{folder_id}' in parents and trashed = false and "
            f"(mimeType = '{FOLDER_MIME_TYPE}' or "
            f"mimeType = '{SHORTCUT_MIME_TYPE}' or "
            f"modifiedTime >= '{modified_after_rfc3339}')"
        )
        response = (
            self.service.files()
            .list(
                q=query,
                spaces="drive",
                pageSize=page_size,
                pageToken=page_token,
                orderBy="folder,name_natural",
                includeItemsFromAllDrives=True,
                supportsAllDrives=True,
                fields=f"nextPageToken,files({DRIVE_FILE_FIELDS})",
            )
            .execute(num_retries=2)
        )
        files = response.get("files") or []
        if not isinstance(files, list):
            raise DriveInventoryError("Drive list response did not contain a file list.")
        return files, str(response.get("nextPageToken") or "") or None


def validate_drive_id(value: str) -> str:
    clean = str(value or "").strip()
    if not DRIVE_ID_PATTERN.fullmatch(clean):
        raise DriveInventoryError(f"Invalid Google Drive ID: {clean!r}.")
    return clean


def local_cutoff_to_utc(value: date, timezone_name: str = "Australia/Sydney") -> datetime:
    local_midnight = datetime.combine(value, datetime_time.min, tzinfo=ZoneInfo(timezone_name))
    return local_midnight.astimezone(datetime_timezone.utc)


def rfc3339(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=datetime_timezone.utc)
    return value.astimezone(datetime_timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_rfc3339(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime_timezone.utc)
    return parsed.astimezone(datetime_timezone.utc)


def _http_status(exc: Exception) -> int | None:
    response = getattr(exc, "resp", None)
    status = getattr(response, "status", None)
    if status is None:
        status = getattr(exc, "status_code", None)
    try:
        return int(status) if status is not None else None
    except (TypeError, ValueError):
        return None


def _permission_summary(file: Mapping[str, Any]) -> dict[str, Any]:
    permission_ids = file.get("permissionIds")
    permission_count = len(permission_ids) if isinstance(permission_ids, list) else None
    capabilities = file.get("capabilities") if isinstance(file.get("capabilities"), Mapping) else {}
    return {
        "container": "shared_drive" if file.get("driveId") else "my_drive",
        "shared": bool(file.get("shared")),
        "permission_count": permission_count,
        "owned_by_connection": bool(file.get("ownedByMe")),
        "download_allowed": capabilities.get("canDownload"),
        "link_sharing": "not_requested",
    }


def _normalised_name(name: str) -> str:
    suffixes = {".docx", ".pdf", ".txt", ".md", ".vtt", ".srt"}
    stem = name.strip()
    while PurePath(stem).suffix.lower() in suffixes:
        stem = stem[: -len(PurePath(stem).suffix)]
    stem = COPY_SUFFIX_PATTERN.sub("", stem)
    return re.sub(r"[^a-z0-9]+", " ", stem.lower()).strip()


def _is_transcript_candidate(name: str, mime_type: str) -> bool:
    if mime_type in {VTT_MIME_TYPE, *SRT_MIME_TYPES}:
        return True
    extension = PurePath(name).suffix.lower()
    if extension in {".vtt", ".srt"}:
        return True
    return bool(TRANSCRIPT_HINT_PATTERN.search(name))


def _metadata_item(
    file: Mapping[str, Any],
    *,
    selected_root_id: str,
    lineage: tuple[str, ...],
    allowed_mime_types: frozenset[str],
    cutoff: datetime,
) -> dict[str, Any]:
    file_id = str(file.get("id") or "").strip()
    name = str(file.get("name") or "").strip()
    mime_type = str(file.get("mimeType") or "application/octet-stream").strip()
    kind = (
        "folder"
        if mime_type == FOLDER_MIME_TYPE
        else "shortcut"
        if mime_type == SHORTCUT_MIME_TYPE
        else "file"
    )
    modified_at = _parse_rfc3339(file.get("modifiedTime"))
    older_than_cutoff = bool(modified_at and modified_at < cutoff)
    extension = PurePath(name).suffix.lower()
    format_allowed = mime_type in allowed_mime_types or extension in DEFAULT_ALLOWED_EXTENSIONS
    supported = kind == "file" and format_allowed and not older_than_cutoff
    transcript_candidate = supported and _is_transcript_candidate(name, mime_type)

    exclusion_reason = None
    if kind == "shortcut":
        exclusion_reason = "shortcut_not_followed"
    elif kind == "file" and older_than_cutoff:
        exclusion_reason = "older_than_cutoff"
    elif kind == "file" and not format_allowed:
        exclusion_reason = "unsupported_mime_type"
    elif kind == "file" and supported and not transcript_candidate:
        exclusion_reason = "supported_non_transcript_name"

    size = file.get("size")
    try:
        size_bytes = int(size) if size is not None else None
    except (TypeError, ValueError):
        size_bytes = None

    shortcut_details = file.get("shortcutDetails")
    if not isinstance(shortcut_details, Mapping):
        shortcut_details = {}

    return {
        "id": file_id,
        "name": name,
        "normalised_name": _normalised_name(name),
        "kind": kind,
        "mime_type": mime_type,
        "file_extension": str(file.get("fileExtension") or ""),
        "size_bytes": size_bytes,
        "created_at": file.get("createdTime"),
        "modified_at": file.get("modifiedTime"),
        "version": str(file.get("version") or ""),
        "checksums": {
            "md5": file.get("md5Checksum"),
            "sha1": file.get("sha1Checksum"),
            "sha256": file.get("sha256Checksum"),
        },
        "drive_id": str(file.get("driveId") or ""),
        "parent_ids": [
            str(value)[:256]
            for value in (file.get("parents") if isinstance(file.get("parents"), list) else [])[:100]
            if str(value).strip()
        ],
        # Deliberately coarse: inventory approval does not need owner identities.
        "owners": [
            {
                "class": (
                    "shared_drive"
                    if file.get("driveId")
                    else "connection_owned"
                    if file.get("ownedByMe")
                    else "shared_with_connection"
                    if file.get("shared")
                    else "unknown"
                )
            }
        ],
        "selected_root_ids": [selected_root_id],
        "lineages": [list(lineage)],
        "permission_class": _permission_summary(file),
        "web_view_url": file.get("webViewLink"),
        "supported": supported,
        "transcript_candidate": transcript_candidate,
        "exclusion_reason": exclusion_reason,
        "shortcut": {
            "target_id": shortcut_details.get("targetId"),
            "target_mime_type": shortcut_details.get("targetMimeType"),
        }
        if kind == "shortcut"
        else None,
        "duplicate_kind": None,
        "duplicate_of": None,
    }


def _merge_item(existing: dict[str, Any], incoming: Mapping[str, Any]) -> None:
    existing["selected_root_ids"] = sorted(
        set(existing["selected_root_ids"]) | set(incoming.get("selected_root_ids", []))
    )
    lineages = {tuple(value) for value in existing["lineages"]}
    lineages.update(tuple(value) for value in incoming.get("lineages", []))
    existing["lineages"] = [list(value) for value in sorted(lineages)]


def _mark_duplicates(items: list[dict[str, Any]]) -> int:
    exact_groups: dict[tuple[str, int | None], list[dict[str, Any]]] = defaultdict(list)
    likely_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)

    for item in items:
        if not item["transcript_candidate"]:
            continue
        checksum = item["checksums"].get("sha256") or item["checksums"].get("md5")
        if checksum:
            exact_groups[(str(checksum), item["size_bytes"])].append(item)
        elif item["normalised_name"]:
            likely_groups[item["normalised_name"]].append(item)

    duplicate_count = 0
    for group in exact_groups.values():
        if len(group) < 2:
            continue
        canonical = sorted(group, key=lambda value: value["id"])[0]
        for duplicate in sorted(group, key=lambda value: value["id"])[1:]:
            duplicate["duplicate_kind"] = "exact_checksum"
            duplicate["duplicate_of"] = canonical["id"]
            duplicate_count += 1

    for group in likely_groups.values():
        available = [item for item in group if item["duplicate_of"] is None]
        if len(available) < 2:
            continue
        canonical = sorted(available, key=lambda value: value["id"])[0]
        for duplicate in sorted(available, key=lambda value: value["id"])[1:]:
            duplicate["duplicate_kind"] = "likely_same_name"
            duplicate["duplicate_of"] = canonical["id"]
            duplicate_count += 1

    return duplicate_count


def _estimate_work(items: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    candidates = [item for item in items if item.get("transcript_candidate")]
    known_bytes = sum(item.get("size_bytes") or 0 for item in candidates)
    unknown_sizes = sum(1 for item in candidates if item.get("size_bytes") is None)
    estimated_characters = known_bytes if known_bytes else None
    estimated_tokens = round(known_bytes / 4) if known_bytes else None
    count = len(candidates)
    processing_time_band = "small" if count <= 100 else "medium" if count <= 1_000 else "large"
    embedding_rate = float(
        getattr(settings, "ORG_MEMORY_DRIVE_EMBEDDING_COST_AUD_PER_MILLION_TOKENS", 0)
        or 0
    )
    extraction_rate = float(
        getattr(settings, "ORG_MEMORY_DRIVE_EXTRACTION_COST_AUD_PER_MILLION_TOKENS", 0)
        or 0
    )
    embedding_cost = (
        round((estimated_tokens / 1_000_000) * embedding_rate, 4)
        if estimated_tokens is not None and embedding_rate > 0
        else None
    )
    extraction_cost = (
        round((estimated_tokens / 1_000_000) * extraction_rate, 4)
        if estimated_tokens is not None and extraction_rate > 0
        else None
    )
    return {
        "known_size_bytes": known_bytes,
        "unknown_size_candidate_count": unknown_sizes,
        "characters": estimated_characters,
        "tokens": estimated_tokens,
        "embedding_cost_aud": embedding_cost,
        "extraction_cost_aud": extraction_cost,
        "pricing_configured": bool(embedding_rate > 0 and extraction_rate > 0),
        "processing_time_band": processing_time_band,
        "review_items": {
            "low": round(count * 0.05),
            "high": round(count * 0.25),
            "basis": "heuristic 5-25% of candidate transcripts until pilot calibration",
        },
        "basis": "metadata_size_only; Google-native exported sizes remain unknown",
    }


def inventory_drive_metadata(
    client: DriveMetadataClientProtocol,
    *,
    organization_id: str,
    connection_id: str,
    folder_ids: Iterable[str],
    modified_after: date,
    allowed_mime_types: Iterable[str] = DEFAULT_ALLOWED_MIME_TYPES,
    limits: DriveInventoryLimits = DriveInventoryLimits(),
    now: Callable[[], datetime] = lambda: datetime.now(datetime_timezone.utc),
    monotonic: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    limits.validate()
    roots = sorted({validate_drive_id(value) for value in folder_ids})
    if not roots:
        raise DriveInventoryError("At least one explicit Google Drive folder ID is required.")
    allowed = frozenset(str(value).strip() for value in allowed_mime_types if str(value).strip())
    if not allowed:
        raise DriveInventoryError("At least one allowed MIME type is required.")

    started_at = now()
    start_monotonic = monotonic()
    cutoff = local_cutoff_to_utc(modified_after)
    cutoff_rfc3339 = rfc3339(cutoff)
    queue: deque[tuple[str, str, tuple[str, ...]]] = deque()
    visited_folders: set[tuple[str, str]] = set()
    items_by_id: dict[str, dict[str, Any]] = {}
    warnings: list[str] = []
    inaccessible_count = 0
    records_seen = 0
    page_count = 0
    partial = False
    ceiling_reason: str | None = None
    valid_root_count = 0

    for root_id in roots:
        try:
            root = client.get_file(root_id)
        except Exception as exc:
            if _http_status(exc) in {403, 404}:
                inaccessible_count += 1
                partial = True
                warnings.append(f"selected_root_inaccessible:{root_id}")
                continue
            raise DriveInventoryError(f"Unable to inspect selected root {root_id}: {exc}") from exc
        if root.get("trashed"):
            raise DriveInventoryError(f"Selected root {root_id} is trashed.")
        if root.get("mimeType") != FOLDER_MIME_TYPE:
            raise DriveInventoryError(f"Selected root {root_id} is not a Google Drive folder.")
        valid_root_count += 1
        queue.append((root_id, root_id, (root_id,)))

    if valid_root_count == 0:
        raise DriveInventoryError("No selected Google Drive roots were accessible.")

    stop_requested = False
    while queue and not stop_requested:
        folder_id, selected_root_id, lineage = queue.popleft()
        visit_key = (selected_root_id, folder_id)
        if visit_key in visited_folders:
            warnings.append(f"folder_cycle_or_repeat_skipped:{selected_root_id}:{folder_id}")
            continue
        visited_folders.add(visit_key)
        page_token: str | None = None

        while True:
            if monotonic() - start_monotonic >= limits.max_seconds:
                partial = True
                ceiling_reason = "max_seconds"
                stop_requested = True
                break
            if page_count >= limits.max_pages:
                partial = True
                ceiling_reason = "max_pages"
                stop_requested = True
                break
            remaining = limits.max_files - records_seen
            if remaining <= 0:
                partial = True
                ceiling_reason = "max_files"
                stop_requested = True
                break

            try:
                page, next_page_token = client.list_children(
                    folder_id,
                    modified_after_rfc3339=cutoff_rfc3339,
                    page_token=page_token,
                    page_size=min(1_000, remaining),
                )
            except Exception as exc:
                if _http_status(exc) in {403, 404}:
                    inaccessible_count += 1
                    partial = True
                    warnings.append(f"folder_inaccessible:{selected_root_id}:{folder_id}")
                    break
                raise DriveInventoryError(f"Unable to list folder {folder_id}: {exc}") from exc

            page_count += 1
            for raw_file in page:
                if records_seen >= limits.max_files:
                    partial = True
                    ceiling_reason = "max_files"
                    stop_requested = True
                    break
                records_seen += 1
                file_id = str(raw_file.get("id") or "").strip()
                if not file_id or not DRIVE_ID_PATTERN.fullmatch(file_id):
                    warnings.append(f"invalid_file_id_skipped:{selected_root_id}:{file_id or 'missing'}")
                    continue
                item_lineage = (*lineage, file_id)
                item = _metadata_item(
                    raw_file,
                    selected_root_id=selected_root_id,
                    lineage=item_lineage,
                    allowed_mime_types=allowed,
                    cutoff=cutoff,
                )
                existing = items_by_id.get(file_id)
                if existing:
                    _merge_item(existing, item)
                else:
                    items_by_id[file_id] = item

                if item["kind"] == "folder":
                    if file_id in lineage:
                        warnings.append(f"folder_cycle_skipped:{selected_root_id}:{file_id}")
                    else:
                        queue.append((file_id, selected_root_id, item_lineage))
                elif item["kind"] == "shortcut":
                    target = item.get("shortcut") or {}
                    warnings.append(
                        f"shortcut_not_followed:{selected_root_id}:{file_id}:{target.get('target_id') or 'unknown'}"
                    )

            if stop_requested:
                break
            if not next_page_token:
                break
            page_token = next_page_token

    items = sorted(items_by_id.values(), key=lambda value: (value["name"].lower(), value["id"]))
    duplicate_count = _mark_duplicates(items)
    candidate_items = [item for item in items if item["transcript_candidate"]]
    file_items = [item for item in items if item["kind"] == "file"]
    unsupported_count = sum(
        1
        for item in items
        if item["kind"] == "shortcut"
        or (item["kind"] == "file" and item["exclusion_reason"] == "unsupported_mime_type")
    )
    formats = Counter(item["mime_type"] for item in items)
    owners = Counter(
        owner.get("class") or "unknown"
        for item in items
        for owner in item.get("owners", [])
    )
    candidate_dates = [
        parsed
        for parsed in (_parse_rfc3339(item.get("modified_at")) for item in candidate_items)
        if parsed is not None
    ]
    completed_at = now()

    identity_payload = {
        "organization_id": str(organization_id),
        "connection_id": str(connection_id),
        "selected_roots": roots,
        "historical_cutoff": cutoff_rfc3339,
        "snapshot": [
            [item["id"], item["version"], item["modified_at"], item["checksums"]]
            for item in items
        ],
    }
    identity_digest = hashlib.sha256(
        json.dumps(identity_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    inventory_id = str(uuid.uuid5(uuid.NAMESPACE_URL, identity_digest))

    if any(item["size_bytes"] is None for item in candidate_items):
        warnings.append("candidate_size_estimates_incomplete")
    warnings.append("link_sharing_not_inferred_from_metadata_only_inventory")
    warnings.append("model_cost_estimates_unavailable_until_models_and_prices_are_approved")

    return {
        "schema_version": 1,
        "inventory_id": inventory_id,
        "mode": "metadata_only_dry_run",
        "organization_id": str(organization_id),
        "connection_id": str(connection_id),
        "started_at": rfc3339(started_at),
        "completed_at": rfc3339(completed_at),
        "selected_roots": roots,
        "historical_cutoff": cutoff_rfc3339,
        "allowed_mime_types": sorted(allowed),
        "limits": {
            "max_files": limits.max_files,
            "max_pages": limits.max_pages,
            "max_seconds": limits.max_seconds,
        },
        "partial": partial,
        "ceiling_reason": ceiling_reason,
        "counts": {
            "seen": len(items),
            "records_seen": records_seen,
            "files": len(file_items),
            "candidate_transcripts": len(candidate_items),
            "duplicates": duplicate_count,
            "unsupported": unsupported_count,
            "inaccessible": inaccessible_count,
            "folders_visited": len(visited_folders),
            "pages": page_count,
        },
        "formats": dict(sorted(formats.items())),
        "owners": dict(sorted(owners.items())),
        "date_range": {
            "oldest": rfc3339(min(candidate_dates)) if candidate_dates else None,
            "newest": rfc3339(max(candidate_dates)) if candidate_dates else None,
        },
        "estimated": _estimate_work(items),
        "items": items,
        "warnings": sorted(set(warnings)),
    }


def build_drive_service(connection):
    """Build a Drive v3 client and persist a refreshed OAuth access token when needed."""

    raw_scopes = connection.scopes or []
    if isinstance(raw_scopes, str):
        raw_scopes = raw_scopes.replace(",", " ").split()
    scopes = {str(value).strip() for value in raw_scopes if str(value).strip()}
    if not scopes.intersection(REQUIRED_DRIVE_SCOPES):
        raise DriveInventoryError("Google Drive connection lacks a read-only Drive scope.")

    access_token = str(connection.access_token or "").strip()
    if not access_token:
        raise DriveInventoryError("Google Drive connection has no access token.")

    from google.auth.transport.requests import Request as GoogleAuthRequest
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    credentials = Credentials(
        token=access_token,
        refresh_token=str(connection.refresh_token or "").strip() or None,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=str(getattr(settings, "GOOGLE_OAUTH_CLIENT_ID", "") or "").strip() or None,
        client_secret=str(getattr(settings, "GOOGLE_OAUTH_CLIENT_SECRET", "") or "").strip() or None,
        scopes=sorted(scopes),
        expiry=connection.token_expires_at,
    )

    expires_at = connection.token_expires_at
    refresh_required = bool(expires_at and expires_at <= timezone.now() + timedelta(minutes=2))
    if refresh_required:
        if not credentials.refresh_token or not credentials.client_id or not credentials.client_secret:
            raise DriveInventoryError("Google Drive connection needs to be reauthorised.")
        try:
            credentials.refresh(GoogleAuthRequest())
        except Exception as exc:
            raise DriveInventoryError(f"Google Drive access-token refresh failed: {exc}") from exc
        connection.access_token = credentials.token or ""
        connection.token_expires_at = credentials.expiry
        connection.last_error = ""
        connection.save(update_fields=["access_token", "token_expires_at", "last_error", "updated_at"])

    return build("drive", "v3", credentials=credentials, cache_discovery=False)
