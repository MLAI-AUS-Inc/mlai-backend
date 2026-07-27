from __future__ import annotations

import base64
import json
from collections import deque
from datetime import date
from typing import Mapping, Optional
from zoneinfo import ZoneInfo

from django.conf import settings
from django.utils import timezone

from org_memory.drive_artifacts import (
    drive_selection_fingerprint,
    persist_drive_inventory_manifest,
)
from org_memory.drive_inventory import (
    DEFAULT_ALLOWED_MIME_TYPES,
    DRIVE_FILE_FIELDS,
    FOLDER_MIME_TYPE,
    DriveInventoryLimits,
    GoogleDriveMetadataClient,
    _metadata_item,
    build_drive_service,
    inventory_drive_metadata,
    local_cutoff_to_utc,
)
from org_memory.drive_processing import prepare_drive_processing_record
from org_memory.governance import assert_provider_inventory_allowed
from org_memory.models import (
    DriveArtifactState,
    DriveDocumentArtifact,
    DriveExtractionStatus,
    DriveInventoryManifest,
    DriveWatchStatus,
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


DRIVE_SCOPE_TYPES = frozenset({"folder", "shared_drive"})
SCOPE_FILE_FIELDS = "id,name,mimeType,driveId,parents,trashed,webViewLink"
CHANGE_FIELDS = (
    "nextPageToken,newStartPageToken,changes("
    "fileId,removed,time,changeType,driveId,"
    f"file({DRIVE_FILE_FIELDS})"
    ")"
)


def _encode_cursor(value: Mapping) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_cursor(value: Optional[str]) -> dict:
    if not value:
        return {"files": None, "drives": None, "root_emitted": False}
    try:
        padding = "=" * (-len(value) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(value + padding).decode("utf-8"))
    except Exception as exc:
        raise ValueError("Google Drive scope cursor is invalid.") from exc
    if not isinstance(decoded, dict):
        raise ValueError("Google Drive scope cursor is invalid.")
    return {
        "files": str(decoded.get("files") or "") or None,
        "drives": str(decoded.get("drives") or "") or None,
        "root_emitted": bool(decoded.get("root_emitted")),
    }


def _selected_scopes(selected_scopes):
    scopes = [scope for scope in selected_scopes if scope.scope_type in DRIVE_SCOPE_TYPES]
    if len(scopes) != len(selected_scopes) or not scopes:
        raise ValueError("Google Drive requires at least one folder or Shared Drive scope.")
    return scopes


def _cutoff_date(configuration, scopes) -> tuple[date, bool]:
    values = [configuration.historical_cutoff]
    values.extend(
        scope.policy.historical_cutoff
        for scope in scopes
        if scope.policy_id and scope.policy.historical_cutoff
    )
    values = [value for value in values if value]
    if not values:
        return date(2000, 1, 1), True
    cutoff = min(values)
    return cutoff.astimezone(ZoneInfo("Australia/Sydney")).date(), False


def _inventory_limits() -> DriveInventoryLimits:
    return DriveInventoryLimits(
        max_files=int(settings.ORG_MEMORY_DRIVE_INVENTORY_MAX_FILES),
        max_pages=int(settings.ORG_MEMORY_DRIVE_INVENTORY_MAX_PAGES),
        max_seconds=int(settings.ORG_MEMORY_DRIVE_INVENTORY_MAX_SECONDS),
    )


def _start_page_token(service) -> str:
    response = service.changes().getStartPageToken(
        supportsAllDrives=True,
    ).execute(num_retries=2)
    token = str(response.get("startPageToken") or "")
    if not token:
        raise ValueError("Google Drive did not return a start page token.")
    return token


def _inventory(configuration, scopes):
    roots = [scope.external_id for scope in scopes]
    limits = _inventory_limits()
    selectors = {
        f"connection:{configuration.connection.pk}",
        f"organization:{configuration.organization_id}",
        *(f"{scope.scope_type}:{scope.external_id}" for scope in scopes),
    }
    assert_provider_inventory_allowed(
        "google_drive",
        selectors,
        requested_max_files=limits.max_files,
    )
    cutoff, cutoff_defaulted = _cutoff_date(configuration, scopes)
    service = build_drive_service(configuration.connection)
    start_token = _start_page_token(service)
    result = inventory_drive_metadata(
        GoogleDriveMetadataClient(service),
        organization_id=str(configuration.organization_id),
        connection_id=str(configuration.connection.pk),
        folder_ids=roots,
        modified_after=cutoff,
        allowed_mime_types=DEFAULT_ALLOWED_MIME_TYPES,
        limits=limits,
    )
    if cutoff_defaulted:
        result["warnings"] = sorted(
            set(result.get("warnings") or []) | {"historical_cutoff_defaulted_to_2000-01-01"}
        )
    manifest, _created = persist_drive_inventory_manifest(
        configuration=configuration,
        scopes=scopes,
        result=result,
        start_page_token=start_token,
    )
    return result, manifest


def _manifest_summary(result, manifest):
    return {
        "manifest_id": str(manifest.pk),
        "inventory_id": str(manifest.inventory_id),
        "scope_count": len(result.get("selected_roots") or []),
        "counts": dict(result.get("counts") or {}),
        "formats": dict(result.get("formats") or {}),
        "owners": dict(result.get("owners") or {}),
        "date_range": dict(result.get("date_range") or {}),
        "estimated": dict(result.get("estimated") or {}),
        "partial": bool(result.get("partial")),
        "ceiling_reason": result.get("ceiling_reason"),
        "content_activated": False,
    }


def _selected_root_membership(service, file: Mapping, scopes) -> tuple[list[str], list[list[str]]]:
    roots = {scope.external_id: scope.scope_type for scope in scopes}
    file_id = str(file.get("id") or "")
    drive_id = str(file.get("driveId") or "")
    matched = set()
    lineages = []
    if drive_id and roots.get(drive_id) == "shared_drive":
        matched.add(drive_id)
        lineages.append([drive_id, file_id])
    if file_id in roots:
        matched.add(file_id)
        lineages.append([file_id])

    queue = deque(
        (str(parent_id), [str(parent_id), file_id])
        for parent_id in file.get("parents") or []
        if str(parent_id)
    )
    visited = set()
    while queue and len(visited) < 256:
        parent_id, lineage = queue.popleft()
        if parent_id in visited:
            continue
        visited.add(parent_id)
        if parent_id in roots:
            matched.add(parent_id)
            lineages.append(lineage)
            continue
        try:
            parent = service.files().get(
                fileId=parent_id,
                supportsAllDrives=True,
                fields=SCOPE_FILE_FIELDS,
            ).execute(num_retries=2)
        except Exception:
            continue
        for grandparent_id in parent.get("parents") or []:
            grandparent_id = str(grandparent_id)
            if grandparent_id:
                queue.append((grandparent_id, [grandparent_id, *lineage]))
    return sorted(matched), sorted(lineages)


def _change_to_item(service, file, scopes, cutoff):
    matched, lineages = _selected_root_membership(service, file, scopes)
    if not matched:
        return None
    item = _metadata_item(
        file,
        selected_root_id=matched[0],
        lineage=tuple(lineages[0] if lineages else [matched[0], file.get("id")]),
        allowed_mime_types=DEFAULT_ALLOWED_MIME_TYPES,
        cutoff=cutoff,
    )
    item["selected_root_ids"] = matched
    item["lineages"] = lineages
    return item


class GoogleDriveMemoryConnector:
    provider = "google_drive"

    def discover_scopes(self, configuration, cursor=None) -> ScopePage:
        service = build_drive_service(configuration.connection)
        state = _decode_cursor(cursor)
        scopes = []
        if not state["root_emitted"]:
            root = service.files().get(
                fileId="root",
                supportsAllDrives=True,
                fields=SCOPE_FILE_FIELDS,
            ).execute(num_retries=2)
            scopes.append(
                ScopeDescriptor(
                    scope_type="folder",
                    external_id=str(root.get("id") or "root"),
                    name=str(root.get("name") or "My Drive"),
                    canonical_url=str(root.get("webViewLink") or "https://drive.google.com/drive/my-drive"),
                    metadata={"container": "my_drive", "root": True},
                )
            )

        folders_response = service.files().list(
            q=f"mimeType = '{FOLDER_MIME_TYPE}' and trashed = false",
            spaces="drive",
            pageSize=100,
            pageToken=state["files"],
            orderBy="name_natural",
            includeItemsFromAllDrives=True,
            supportsAllDrives=True,
            fields=f"nextPageToken,files({SCOPE_FILE_FIELDS})",
        ).execute(num_retries=2)
        for folder in folders_response.get("files") or []:
            scopes.append(
                ScopeDescriptor(
                    scope_type="folder",
                    external_id=str(folder.get("id") or ""),
                    name=str(folder.get("name") or "Unnamed folder"),
                    canonical_url=str(folder.get("webViewLink") or ""),
                    metadata={
                        "container": "shared_drive" if folder.get("driveId") else "my_drive",
                        "drive_id": str(folder.get("driveId") or ""),
                        "parent_ids": list(folder.get("parents") or []),
                    },
                )
            )

        drives_response = service.drives().list(
            pageSize=100,
            pageToken=state["drives"],
            fields="nextPageToken,drives(id,name,createdTime,hidden)",
        ).execute(num_retries=2)
        for drive in drives_response.get("drives") or []:
            scopes.append(
                ScopeDescriptor(
                    scope_type="shared_drive",
                    external_id=str(drive.get("id") or ""),
                    name=str(drive.get("name") or "Unnamed Shared Drive"),
                    canonical_url=f"https://drive.google.com/drive/folders/{drive.get('id')}",
                    metadata={
                        "container": "shared_drive",
                        "created_at": drive.get("createdTime"),
                        "hidden": bool(drive.get("hidden")),
                    },
                )
            )

        next_state = {
            "files": folders_response.get("nextPageToken"),
            "drives": drives_response.get("nextPageToken"),
            "root_emitted": True,
        }
        has_more = bool(next_state["files"] or next_state["drives"])
        return ScopePage(
            scopes=tuple(scope for scope in scopes if scope.external_id),
            next_cursor=_encode_cursor(next_state) if has_more else None,
            warnings=(
                "Discovery returns metadata only; folders are not selected until an operator approves them.",
            ),
        )

    def preview(self, configuration, selected_scopes, policy) -> SourcePreview:
        scopes = _selected_scopes(selected_scopes)
        result, manifest = _inventory(configuration, scopes)
        return SourcePreview(
            summary=_manifest_summary(result, manifest),
            warnings=tuple(result.get("warnings") or []),
        )

    def dry_run(self, configuration, selected_scopes, policy) -> DryRunResult:
        scopes = _selected_scopes(selected_scopes)
        fingerprint = drive_selection_fingerprint(configuration, scopes)
        manifest = configuration.drive_inventory_manifests.filter(
            selection_fingerprint=fingerprint,
        ).order_by("-created_at").first()
        if manifest is None:
            _result, manifest = _inventory(configuration, scopes)
        sample = [
            {
                "file_id": item.get("id"),
                "name": item.get("name"),
                "mime_type": item.get("mime_type"),
                "modified_at": item.get("modified_at"),
                "supported": item.get("supported"),
                "transcript_candidate": item.get("transcript_candidate"),
                "exclusion_reason": item.get("exclusion_reason"),
            }
            for item in manifest.snapshot
            if item.get("kind") != "folder"
        ][:10]
        return DryRunResult(
            summary={
                "manifest_id": str(manifest.pk),
                "sample_artifacts": len(sample),
                "samples": sample,
                "candidate_transcripts": manifest.counts.get("candidate_transcripts", 0),
                "estimated": manifest.estimated,
                "approval_ready": not manifest.is_partial,
                "active_memory_created": False,
            },
            warnings=(
                "Dry-run stores only the immutable metadata inventory; no file body, chunk, embedding, or claim is created.",
            ),
        )

    def backfill(self, configuration, selected_scopes, checkpoint) -> SyncPage:
        scopes = _selected_scopes(selected_scopes)
        fingerprint = drive_selection_fingerprint(configuration, scopes)
        manifest_id = str((checkpoint or {}).get("manifest_id") or "")
        manifests = DriveInventoryManifest.objects.filter(
            configuration=configuration,
            selection_fingerprint=fingerprint,
        )
        if manifest_id:
            manifests = manifests.filter(pk=manifest_id)
        elif configuration.approved_preview_id:
            approved_manifest = str(
                (configuration.approved_preview.summary or {}).get("manifest_id") or ""
            )
            if approved_manifest:
                manifests = manifests.filter(pk=approved_manifest)
        manifest = manifests.order_by("-created_at").first()
        if manifest is None or manifest.is_partial:
            raise ValueError("A complete approved Drive inventory is required for backfill.")
        items = sorted(
            (item for item in manifest.snapshot if item.get("kind") != "folder"),
            key=lambda item: (
                str(item.get("modified_at") or item.get("created_at") or ""),
                str(item.get("id") or ""),
            ),
        )
        offset = max(int((checkpoint or {}).get("offset") or 0), 0)
        page_size = max(
            min(int(settings.ORG_MEMORY_DRIVE_PROCESSING_PAGE_SIZE), 100),
            1,
        )
        page = items[offset : offset + page_size]
        service = build_drive_service(configuration.connection) if page else None
        records = [
            prepare_drive_processing_record(service, configuration, item)
            for item in page
        ]
        next_offset = offset + len(page)
        has_more = next_offset < len(items)
        removals = []
        if not has_more:
            current_ids = {str(item.get("id")) for item in items}
            removals = [
                {"file_id": file_id, "reason": "missing_from_selected_inventory"}
                for file_id in configuration.drive_document_artifacts.filter(
                    lifecycle_state=DriveArtifactState.ACTIVE,
                ).exclude(file_id__in=current_ids).values_list("file_id", flat=True)
            ]
        return SyncPage(
            records=tuple(records),
            removals=tuple(removals),
            next_cursor=manifest.start_page_token if not has_more else None,
            checkpoint={
                "manifest_id": str(manifest.pk),
                "offset": next_offset,
                "total": len(items),
                "mode": "oldest_first_content_processing",
                "ordering": "modified_at,id",
            },
            has_more=has_more,
        )

    def incremental_sync(self, configuration, cursor) -> SyncPage:
        scopes = _selected_scopes(
            list(
                configuration.source_scopes.filter(
                    selected=True,
                    status="selected",
                ).select_related("policy")
            )
        )
        service = build_drive_service(configuration.connection)
        if not cursor:
            return SyncPage(records=(), next_cursor=_start_page_token(service))
        response = service.changes().list(
            pageToken=str(cursor),
            pageSize=max(
                min(int(settings.ORG_MEMORY_DRIVE_PROCESSING_PAGE_SIZE), 1000),
                1,
            ),
            spaces="drive",
            includeItemsFromAllDrives=True,
            supportsAllDrives=True,
            includeRemoved=True,
            fields=CHANGE_FIELDS,
        ).execute(num_retries=2)
        cutoff_date, _defaulted = _cutoff_date(configuration, scopes)
        cutoff = local_cutoff_to_utc(cutoff_date)
        records = []
        removals = []
        existing_ids = set(
            DriveDocumentArtifact.objects.filter(configuration=configuration).values_list(
                "file_id", flat=True
            )
        )
        for change in response.get("changes") or []:
            file_id = str(change.get("fileId") or "")
            file = change.get("file") if isinstance(change.get("file"), Mapping) else None
            if change.get("removed") or file is None:
                if file_id in existing_ids:
                    removals.append(
                        {
                            "file_id": file_id,
                            "reason": "access_lost",
                            "change_time": change.get("time"),
                        }
                    )
                continue
            if file.get("trashed"):
                if file_id in existing_ids:
                    removals.append(
                        {
                            "file_id": file_id,
                            "reason": "trashed",
                            "change_time": change.get("time"),
                        }
                    )
                continue
            item = _change_to_item(service, file, scopes, cutoff)
            if item is None:
                if file_id in existing_ids:
                    removals.append(
                        {
                            "file_id": file_id,
                            "reason": "moved_out_of_selected_scope",
                            "change_time": change.get("time"),
                        }
                    )
                continue
            if item.get("kind") != "folder":
                records.append(
                    prepare_drive_processing_record(
                        service,
                        configuration,
                        item,
                    )
                )
        next_token = str(
            response.get("nextPageToken")
            or response.get("newStartPageToken")
            or cursor
        )
        return SyncPage(
            records=tuple(records),
            removals=tuple(removals),
            next_cursor=next_token,
            checkpoint={"mode": "changes", "change_count": len(response.get("changes") or [])},
            has_more=bool(response.get("nextPageToken")),
        )

    def refresh_permissions(self, configuration, checkpoint) -> SyncPage:
        return self.incremental_sync(configuration, configuration.sync_cursor or None)

    def fetch_version(self, configuration, external_id) -> SourceVersionPayload:
        service = build_drive_service(configuration.connection)
        file = service.files().get(
            fileId=str(external_id),
            supportsAllDrives=True,
            fields=DRIVE_FILE_FIELDS,
        ).execute(num_retries=2)
        scopes = _selected_scopes(
            list(configuration.source_scopes.filter(selected=True, status="selected"))
        )
        cutoff_date, _defaulted = _cutoff_date(configuration, scopes)
        item = _change_to_item(service, file, scopes, local_cutoff_to_utc(cutoff_date))
        if item is None:
            raise ValueError("Drive file is outside the selected roots.")
        return SourceVersionPayload(
            external_id=str(file.get("id")),
            canonical_url=str(file.get("webViewLink") or ""),
            version_key=str(file.get("version") or file.get("modifiedTime") or "metadata"),
            source_times={
                "created_at": file.get("createdTime"),
                "modified_at": file.get("modifiedTime"),
            },
            metadata=item,
            acl=dict(item.get("permission_class") or {}),
            content=None,
        )

    def tombstone_missing(self, configuration, sync_run) -> TombstoneResult:
        return TombstoneResult(tombstoned_external_ids=())

    def health(self, configuration) -> ConnectorHealth:
        now = timezone.now()
        artifacts = configuration.drive_document_artifacts
        active_watch = configuration.drive_watch_channels.filter(
            status=DriveWatchStatus.ACTIVE,
            expiration_at__gt=now,
        ).order_by("-expiration_at").first()
        last_sync = configuration.last_successful_sync_at
        last_report = configuration.drive_reconciliation_reports.order_by(
            "-started_at"
        ).first()
        return ConnectorHealth(
            status=configuration.lifecycle_state,
            credential_status=str(getattr(configuration.connection, "status", "connected")),
            last_successful_sync_at=last_sync.isoformat() if last_sync else None,
            source_lag_seconds=(
                max(int((now - last_sync).total_seconds()), 0) if last_sync else None
            ),
            details={
                "artifacts": artifacts.count(),
                "active_artifacts": artifacts.filter(
                    lifecycle_state=DriveArtifactState.ACTIVE
                ).count(),
                "ready_for_parsing": artifacts.filter(
                    extraction_status=DriveExtractionStatus.READY_FOR_PARSING
                ).count(),
                "unsupported": artifacts.filter(
                    extraction_status=DriveExtractionStatus.UNSUPPORTED
                ).count(),
                "failed": artifacts.filter(
                    extraction_status=DriveExtractionStatus.FAILED
                ).count(),
                "duplicate_suppressed": artifacts.filter(
                    extraction_status=DriveExtractionStatus.DUPLICATE
                ).count(),
                "meetings": configuration.drive_meetings.count(),
                "change_cursor_configured": bool(configuration.sync_cursor),
                "watch_active": bool(active_watch),
                "watch_expires_at": (
                    active_watch.expiration_at.isoformat() if active_watch else None
                ),
                "last_reconciliation": (
                    {
                        "report_id": str(last_report.pk),
                        "counts": dict(last_report.counts or {}),
                        "completed_at": (
                            last_report.completed_at.isoformat()
                            if last_report.completed_at
                            else None
                        ),
                    }
                    if last_report
                    else None
                ),
            },
        )
