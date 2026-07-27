from __future__ import annotations

import hashlib
import json
import uuid
from typing import Iterable, Mapping, Optional

from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .models import (
    DriveArtifactState,
    DriveDocumentArtifact,
    DriveDocumentArtifactVersion,
    DriveExtractionStatus,
    DriveInventoryManifest,
    MemoryProvider,
    MemoryScopeStatus,
)


class DriveArtifactError(ValueError):
    pass


def _canonical_hash(value) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _datetime(value):
    if not value:
        return None
    parsed = parse_datetime(str(value))
    return parsed


def drive_selection_fingerprint(configuration, scopes) -> str:
    return _canonical_hash(
        {
            "configuration_id": str(configuration.pk),
            "historical_cutoff": (
                configuration.historical_cutoff.isoformat()
                if configuration.historical_cutoff
                else None
            ),
            "scopes": [
                {
                    "scope_type": scope.scope_type,
                    "external_id": scope.external_id,
                    "classification": scope.default_classification,
                    "policy_id": scope.policy_id,
                }
                for scope in sorted(
                    scopes,
                    key=lambda row: (row.scope_type, row.external_id),
                )
            ],
        }
    )


@transaction.atomic
def persist_drive_inventory_manifest(
    *,
    configuration,
    scopes,
    result: Mapping,
    start_page_token: str,
) -> tuple[DriveInventoryManifest, bool]:
    if configuration.provider != MemoryProvider.GOOGLE_DRIVE:
        raise DriveArtifactError("Inventory requires a Google Drive configuration.")
    snapshot = list(result.get("items") or [])
    snapshot_hash = _canonical_hash(snapshot)
    inventory_id = uuid.UUID(str(result["inventory_id"]))
    defaults = {
        "organization": configuration.organization,
        "selection_fingerprint": drive_selection_fingerprint(configuration, scopes),
        "selected_roots": list(result.get("selected_roots") or []),
        "historical_cutoff": _datetime(result.get("historical_cutoff")),
        "allowed_mime_types": list(result.get("allowed_mime_types") or []),
        "is_partial": bool(result.get("partial")),
        "ceiling_reason": str(result.get("ceiling_reason") or "")[:64],
        "counts": dict(result.get("counts") or {}),
        "formats": dict(result.get("formats") or {}),
        "owners": dict(result.get("owners") or {}),
        "date_range": dict(result.get("date_range") or {}),
        "estimated": dict(result.get("estimated") or {}),
        "warnings": list(result.get("warnings") or []),
        "start_page_token": str(start_page_token or ""),
        "snapshot": snapshot,
        "snapshot_hash": snapshot_hash,
    }
    manifest, created = DriveInventoryManifest.objects.get_or_create(
        configuration=configuration,
        inventory_id=inventory_id,
        defaults=defaults,
    )
    if not created and (
        manifest.snapshot_hash != snapshot_hash
        or manifest.selection_fingerprint != defaults["selection_fingerprint"]
    ):
        raise DriveArtifactError("A Drive inventory identity collision was detected.")
    if created:
        manifest.full_clean()
    return manifest, created


def _selected_scope_map(configuration):
    return {
        scope.external_id: scope
        for scope in configuration.source_scopes.filter(
            selected=True,
            status=MemoryScopeStatus.SELECTED,
            scope_type__in=("folder", "shared_drive"),
        )
    }


def _artifact_snapshot(item: Mapping, *, lifecycle_state: str, removal_reason: str = ""):
    return {
        "file_id": str(item.get("id") or item.get("file_id") or ""),
        "drive_id": str(item.get("drive_id") or ""),
        "shortcut_target_id": str((item.get("shortcut") or {}).get("target_id") or ""),
        "parent_ids": list(item.get("parent_ids") or []),
        "selected_root_ids": list(item.get("selected_root_ids") or []),
        "lineages": list(item.get("lineages") or []),
        "name": str(item.get("name") or "")[:512],
        "mime_type": str(item.get("mime_type") or "application/octet-stream")[:255],
        "size_bytes": item.get("size_bytes"),
        "web_view_url": str(item.get("web_view_url") or "")[:2048],
        "created_at": item.get("created_at"),
        "modified_at": item.get("modified_at"),
        "provider_version": str(item.get("version") or "")[:512],
        "checksums": dict(item.get("checksums") or {}),
        "owners": list(item.get("owners") or [])[:20],
        "permission_class": dict(item.get("permission_class") or {}),
        "supported": bool(item.get("supported")),
        "transcript_candidate": bool(item.get("transcript_candidate")),
        "exclusion_reason": str(item.get("exclusion_reason") or "")[:128],
        "lifecycle_state": lifecycle_state,
        "removal_reason": str(removal_reason or "")[:512],
    }


def _checksum(item: Mapping) -> str:
    checksums = item.get("checksums") if isinstance(item.get("checksums"), Mapping) else {}
    return str(checksums.get("sha256") or checksums.get("md5") or checksums.get("sha1") or "")[:128]


def _extraction_status(item: Mapping) -> str:
    if item.get("transcript_candidate") and item.get("supported"):
        return DriveExtractionStatus.READY_FOR_PARSING
    if item.get("kind") == "shortcut" or item.get("exclusion_reason") == "unsupported_mime_type":
        return DriveExtractionStatus.UNSUPPORTED
    return DriveExtractionStatus.METADATA_ONLY


def _promote_artifact_version(
    artifact: DriveDocumentArtifact,
    snapshot: Mapping,
    *,
    captured_at,
) -> bool:
    metadata_hash = _canonical_hash(snapshot)
    current = artifact.current_version
    if current and current.metadata_hash == metadata_hash:
        return False
    base_version = str(snapshot.get("provider_version") or snapshot.get("modified_at") or "metadata")
    version_key = f"{base_version}:{metadata_hash[:20]}"[:512]
    artifact.versions.filter(is_current=True).update(
        is_current=False,
        retired_at=captured_at,
    )
    version = DriveDocumentArtifactVersion.objects.create(
        artifact=artifact,
        version_key=version_key,
        metadata_hash=metadata_hash,
        metadata_snapshot=dict(snapshot),
        acl_snapshot={
            "owners": list(snapshot.get("owners") or []),
            "permission_class": dict(snapshot.get("permission_class") or {}),
        },
        is_current=True,
        captured_at=captured_at,
    )
    artifact.current_version = version
    artifact.save(update_fields=("current_version", "updated_at"))
    return True


@transaction.atomic
def upsert_drive_artifact(configuration, item: Mapping, *, synced_at=None) -> tuple[DriveDocumentArtifact, bool, bool]:
    if configuration.provider != MemoryProvider.GOOGLE_DRIVE:
        raise DriveArtifactError("Artifact upsert requires a Google Drive configuration.")
    file_id = str(item.get("id") or "").strip()
    if not file_id:
        raise DriveArtifactError("Drive artifact record is missing a file ID.")
    if item.get("kind") not in {"file", "shortcut"}:
        raise DriveArtifactError("Only Drive files and shortcuts are durable artifacts.")
    selected_scopes = _selected_scope_map(configuration)
    root_ids = sorted({str(value) for value in item.get("selected_root_ids") or []})
    if not root_ids or any(root_id not in selected_scopes for root_id in root_ids):
        raise DriveArtifactError("Drive artifact escaped its currently selected roots.")
    now = synced_at or timezone.now()
    snapshot = _artifact_snapshot(item, lifecycle_state=DriveArtifactState.ACTIVE)
    source_scope = selected_scopes[root_ids[0]]
    extraction_status = _extraction_status(item)
    defaults = {
        "organization": configuration.organization,
        "source_scope": source_scope,
        "drive_id": snapshot["drive_id"],
        "shortcut_target_id": snapshot["shortcut_target_id"],
        "parent_ids": snapshot["parent_ids"],
        "selected_root_ids": root_ids,
        "lineages": snapshot["lineages"],
        "name": snapshot["name"],
        "mime_type": snapshot["mime_type"],
        "size_bytes": snapshot["size_bytes"],
        "web_view_url": snapshot["web_view_url"],
        "source_created_at": _datetime(snapshot["created_at"]),
        "source_modified_at": _datetime(snapshot["modified_at"]),
        "provider_version": snapshot["provider_version"],
        "checksum": _checksum(item),
        "owner_snapshot": snapshot["owners"],
        "permission_snapshot": snapshot["permission_class"],
        "lifecycle_state": DriveArtifactState.ACTIVE,
        "supported": snapshot["supported"],
        "transcript_candidate": snapshot["transcript_candidate"],
        "exclusion_reason": snapshot["exclusion_reason"],
        "last_seen_at": now,
        "last_synced_at": now,
        "removed_at": None,
    }
    artifact, created = DriveDocumentArtifact.objects.get_or_create(
        configuration=configuration,
        file_id=file_id,
        defaults={
            **defaults,
            "extraction_status": extraction_status,
            "extraction_error": "",
        },
    )
    if not created:
        for field, value in defaults.items():
            setattr(artifact, field, value)
        artifact.save(update_fields=(*defaults.keys(), "updated_at"))
    version_created = _promote_artifact_version(
        artifact,
        snapshot,
        captured_at=now,
    )
    if version_created and not created:
        artifact.extraction_status = extraction_status
        artifact.extraction_error = ""
        artifact.work_classification = ""
        artifact.extracted_content_hash = ""
        artifact.parser_version = ""
        artifact.extraction_report = {}
        artifact.last_extracted_at = None
        artifact.save(
            update_fields=(
                "extraction_status",
                "extraction_error",
                "work_classification",
                "extracted_content_hash",
                "parser_version",
                "extraction_report",
                "last_extracted_at",
                "updated_at",
            )
        )
    return artifact, created, version_created


@transaction.atomic
def remove_drive_artifact(
    configuration,
    removal: Mapping,
    *,
    synced_at=None,
) -> tuple[bool, bool]:
    file_id = str(removal.get("file_id") or removal.get("external_id") or "").strip()
    if not file_id:
        raise DriveArtifactError("Drive removal is missing a file ID.")
    artifact = DriveDocumentArtifact.objects.select_for_update().filter(
        configuration=configuration,
        file_id=file_id,
    ).first()
    if artifact is None:
        return False, False
    now = synced_at or timezone.now()
    reason = str(removal.get("reason") or "provider_removed")[:512]
    if reason == "trashed":
        state = DriveArtifactState.TRASHED
    elif reason in {"access_lost", "provider_removed"}:
        state = DriveArtifactState.ACCESS_LOST
    else:
        state = DriveArtifactState.REMOVED
    current_snapshot = (
        dict(artifact.current_version.metadata_snapshot)
        if artifact.current_version_id
        else {"file_id": artifact.file_id}
    )
    current_snapshot.update(
        lifecycle_state=state,
        removal_reason=reason,
        provider_version=f"removal:{removal.get('change_time') or now.isoformat()}",
    )
    artifact.lifecycle_state = state
    artifact.removed_at = now
    artifact.last_synced_at = now
    artifact.save(
        update_fields=("lifecycle_state", "removed_at", "last_synced_at", "updated_at")
    )
    version_created = _promote_artifact_version(
        artifact,
        current_snapshot,
        captured_at=now,
    )
    return True, version_created


def commit_drive_metadata_page(
    configuration,
    *,
    records: Iterable[Mapping],
    removals: Iterable[Mapping],
) -> tuple[int, int, int]:
    records_processed = 0
    removals_processed = 0
    versions_created = 0
    now = timezone.now()
    for item in records:
        _artifact, _created, version_created = upsert_drive_artifact(
            configuration,
            item,
            synced_at=now,
        )
        records_processed += 1
        versions_created += int(version_created)
    for removal in removals:
        removed, version_created = remove_drive_artifact(
            configuration,
            removal,
            synced_at=now,
        )
        removals_processed += int(removed)
        versions_created += int(version_created)
    return records_processed, removals_processed, versions_created
