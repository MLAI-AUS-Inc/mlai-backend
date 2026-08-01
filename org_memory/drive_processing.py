from __future__ import annotations

import hashlib
import io
import json
from dataclasses import dataclass
from typing import Iterable, Mapping, Optional

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .drive_artifacts import remove_drive_artifact, upsert_drive_artifact
from .drive_inventory import GOOGLE_DOC_MIME_TYPE, SHORTCUT_MIME_TYPE
from .drive_parsing import (
    DRIVE_PARSER_VERSION,
    GOOGLE_DOC_EXPORT_MIME_TYPE,
    content_signature,
    infer_meeting_metadata,
    normalized_content,
    parse_drive_document,
)
from .kernel import (
    capture_source_version,
    restore_source_access,
    revoke_source_access,
    tombstone_source,
)
from .models import (
    DriveArtifactState,
    DriveDocumentArtifact,
    DriveDocumentExtraction,
    DriveExtractionStatus,
    DriveInventoryManifest,
    DriveMeeting,
    DriveMeetingArtifactLink,
    DriveMeetingRelation,
    DriveReconciliationReport,
    DriveWorkClassification,
    MemorySource,
    MemorySourceLifecycle,
)


CONTENT_DRIVE_SCOPES = frozenset(
    {
        "https://www.googleapis.com/auth/drive",
        "https://www.googleapis.com/auth/drive.file",
        "https://www.googleapis.com/auth/drive.readonly",
    }
)
AUDIO_VIDEO_PREFIXES = ("audio/", "video/")
GOOGLE_MEDIA_MIME_TYPES = frozenset(
    {
        "application/vnd.google-apps.audio",
        "application/vnd.google-apps.video",
        "application/vnd.google-apps.vid",
    }
)


class DriveProcessingError(ValueError):
    pass


@dataclass(frozen=True)
class DriveProcessingCommitResult:
    records_processed: int
    removals_processed: int
    metadata_versions_created: int
    outcomes: Mapping[str, int]


def _hash(value) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


def _connection_scopes(configuration) -> set[str]:
    raw = getattr(configuration.connection, "scopes", []) or []
    if isinstance(raw, str):
        raw = raw.replace(",", " ").split()
    return {str(value).strip() for value in raw if str(value).strip()}


def _unchanged_extraction(configuration, item: Mapping) -> Optional[DriveDocumentExtraction]:
    artifact = (
        DriveDocumentArtifact.objects.filter(
            configuration=configuration,
            file_id=str(item.get("id") or ""),
            provider_version=str(item.get("version") or ""),
            source_modified_at=item.get("modified_at") or None,
            permission_snapshot=dict(item.get("permission_class") or {}),
            lifecycle_state=DriveArtifactState.ACTIVE,
        )
        .select_related("current_version")
        .first()
    )
    if artifact is None or artifact.current_version_id is None:
        return None
    extraction = artifact.current_version.extractions.filter(
        parser_version=DRIVE_PARSER_VERSION,
        status__in={
            DriveExtractionStatus.EXTRACTED,
            DriveExtractionStatus.DUPLICATE,
            DriveExtractionStatus.UNSUPPORTED,
            DriveExtractionStatus.FAILED,
        },
    ).first()
    if extraction:
        return extraction
    return None


def _unsupported(item: Mapping, work_classification: str, warning: str) -> dict:
    return {
        "artifact": dict(item),
        "processing": {
            "status": DriveExtractionStatus.UNSUPPORTED,
            "work_classification": work_classification,
            "parser_name": "classification",
            "parser_version": DRIVE_PARSER_VERSION,
            "export_mime_type": "",
            "content_hash": "",
            "normalized_content_hash": "",
            "content_signature": [],
            "byte_count": 0,
            "character_count": 0,
            "chunks": [],
            "warnings": [warning],
            "error": "",
            "meeting": None,
            "bounded_excerpt": "",
        },
    }


def _download_bytes(service, item: Mapping) -> tuple[bytes, str]:
    file_id = str(item.get("id") or "")
    mime_type = str(item.get("mime_type") or "")
    if mime_type == GOOGLE_DOC_MIME_TYPE:
        request = service.files().export_media(
            fileId=file_id,
            mimeType=GOOGLE_DOC_EXPORT_MIME_TYPE,
        )
        export_mime_type = GOOGLE_DOC_EXPORT_MIME_TYPE
    else:
        request = service.files().get_media(
            fileId=file_id,
            supportsAllDrives=True,
        )
        export_mime_type = mime_type
    if hasattr(request, "http") and hasattr(request, "uri"):
        from googleapiclient.http import MediaIoBaseDownload

        stream = io.BytesIO()
        downloader = MediaIoBaseDownload(
            stream,
            request,
            chunksize=min(1024 * 1024, max(int(settings.ORG_MEMORY_DRIVE_MAX_DOWNLOAD_BYTES), 1)),
        )
        done = False
        while not done:
            _status, done = downloader.next_chunk(num_retries=2)
            if stream.tell() > max(int(settings.ORG_MEMORY_DRIVE_MAX_DOWNLOAD_BYTES), 1):
                raise DriveProcessingError("Drive content exceeded its bounded download limit.")
        payload = stream.getvalue()
    else:
        # Lightweight fake requests in connector tests expose byte responses directly.
        payload = request.execute(num_retries=2)
    if not isinstance(payload, (bytes, bytearray)):
        raise DriveProcessingError("Drive content response was not byte content.")
    payload = bytes(payload)
    maximum = max(int(settings.ORG_MEMORY_DRIVE_MAX_DOWNLOAD_BYTES), 1)
    if len(payload) > maximum:
        raise DriveProcessingError(f"Drive content exceeds the {maximum}-byte processing limit.")
    return payload, export_mime_type


def prepare_drive_processing_record(service, configuration, item: Mapping) -> dict:
    """Fetch and parse an already-inventoried item without writing source content."""

    item = dict(item)
    selected_roots = set(
        configuration.source_scopes.filter(
            selected=True,
            status="selected",
            scope_type__in=("folder", "shared_drive"),
        ).values_list("external_id", flat=True)
    )
    item_roots = {str(value) for value in item.get("selected_root_ids") or []}
    if not item_roots or not item_roots.issubset(selected_roots):
        raise DriveProcessingError(
            "Drive content fetch escaped its currently selected roots."
        )
    unchanged = _unchanged_extraction(configuration, item)
    if unchanged is not None:
        return {
            "artifact": item,
            "processing": {
                "status": "unchanged",
                "existing_extraction_id": str(unchanged.pk),
                "parser_version": unchanged.parser_version,
            },
        }
    if item.get("kind") == "shortcut" or item.get("mime_type") == SHORTCUT_MIME_TYPE:
        return _unsupported(item, DriveWorkClassification.SHORTCUT, "shortcut_not_followed")
    mime_type = str(item.get("mime_type") or "")
    if mime_type.startswith(AUDIO_VIDEO_PREFIXES) or mime_type in GOOGLE_MEDIA_MIME_TYPES:
        return _unsupported(
            item,
            DriveWorkClassification.NEEDS_TRANSCRIPTION,
            "existing_transcript_required_for_audio_or_video",
        )
    if not item.get("transcript_candidate"):
        return _unsupported(
            item,
            DriveWorkClassification.NOT_TRANSCRIPT,
            "not_selected_as_transcript_candidate",
        )
    if not item.get("supported"):
        return _unsupported(
            item,
            DriveWorkClassification.UNSUPPORTED_FORMAT,
            str(item.get("exclusion_reason") or "unsupported_format"),
        )
    permission = dict(item.get("permission_class") or {})
    if permission.get("download_allowed") is False:
        return _unsupported(
            item,
            DriveWorkClassification.DOWNLOAD_RESTRICTED,
            "drive_download_restricted",
        )
    if not (_connection_scopes(configuration) & CONTENT_DRIVE_SCOPES):
        raise DriveProcessingError(
            "Google Drive content processing requires the drive.readonly content scope."
        )
    size = item.get("size_bytes")
    maximum = max(int(settings.ORG_MEMORY_DRIVE_MAX_DOWNLOAD_BYTES), 1)
    if size is not None and int(size) > maximum:
        return _unsupported(
            item,
            DriveWorkClassification.UNSUPPORTED_FORMAT,
            f"metadata_size_exceeds_limit:{maximum}",
        )

    raw_bytes, export_mime_type = _download_bytes(service, item)
    parsed = parse_drive_document(
        file_id=str(item.get("id") or ""),
        filename=str(item.get("name") or ""),
        mime_type=mime_type,
        raw_bytes=raw_bytes,
    )
    normalized = normalized_content(parsed.text)
    content_hash = hashlib.sha256(parsed.text.encode("utf-8")).hexdigest() if parsed.text else ""
    normalized_hash = hashlib.sha256(normalized.encode("utf-8")).hexdigest() if normalized else ""
    meeting = (
        infer_meeting_metadata(
            filename=str(item.get("name") or ""),
            text=parsed.text,
            source_created_at=item.get("created_at"),
        )
        if parsed.text
        else None
    )
    chunks = []
    for raw_chunk in parsed.chunks:
        chunk = dict(raw_chunk)
        locator = dict(chunk.get("source_locator") or {})
        locator["meeting"] = {
            "identity_key": str((meeting or {}).get("identity_key") or ""),
            "title": str((meeting or {}).get("normalized_title") or ""),
            "occurred_at": (meeting or {}).get("occurred_at"),
            "timezone": str((meeting or {}).get("timezone_name") or ""),
            "participants": list((meeting or {}).get("participants") or []),
        }
        chunk["source_locator"] = locator
        chunks.append(chunk)
    return {
        "artifact": item,
        "processing": {
            "status": parsed.status,
            "work_classification": parsed.work_classification,
            "parser_name": parsed.parser_name,
            "parser_version": DRIVE_PARSER_VERSION,
            "export_mime_type": export_mime_type,
            "content_hash": content_hash,
            "normalized_content_hash": normalized_hash,
            "content_signature": content_signature(parsed.text),
            "byte_count": len(raw_bytes),
            "character_count": len(parsed.text),
            "chunks": chunks,
            "warnings": list(parsed.warnings),
            "error": parsed.error,
            "meeting": meeting,
            "bounded_excerpt": parsed.text[:4096],
        },
    }


def _jaccard(left: Iterable[str], right: Iterable[str]) -> float:
    left_set = set(left)
    right_set = set(right)
    if not left_set or not right_set:
        return 0.0
    return len(left_set & right_set) / len(left_set | right_set)


def _meeting_for_processing(configuration, artifact, processing):
    meeting_data = processing.get("meeting") or {}
    identity_key = str(meeting_data.get("identity_key") or "")
    if not identity_key:
        return None
    meeting, _created = DriveMeeting.objects.get_or_create(
        configuration=configuration,
        identity_key=identity_key,
        defaults={
            "organization": configuration.organization,
            "normalized_title": str(meeting_data.get("normalized_title") or "untitled meeting")[:512],
            "occurred_at": meeting_data.get("occurred_at") or None,
            "timezone_name": str(meeting_data.get("timezone_name") or "Australia/Sydney")[:64],
            "participants": list(meeting_data.get("participants") or []),
            "identity_basis": dict(meeting_data.get("identity_basis") or {}),
        },
    )
    return meeting


def _duplicate_candidate(configuration, artifact, processing, meeting):
    normalized_hash = str(processing.get("normalized_content_hash") or "")
    query = DriveDocumentExtraction.objects.filter(
        artifact_version__artifact__configuration=configuration,
        artifact_version__is_current=True,
        status=DriveExtractionStatus.EXTRACTED,
    ).exclude(artifact_version__artifact=artifact)
    if normalized_hash:
        exact = query.filter(normalized_content_hash=normalized_hash).select_related(
            "artifact_version__artifact"
        ).first()
        if exact:
            return exact.artifact_version.artifact, 1.0, "exact_content"
    if meeting is None:
        return None, 0.0, ""
    candidates = query.filter(
        artifact_version__artifact__meeting_link__meeting=meeting,
    ).select_related("artifact_version__artifact")[:50]
    signature = processing.get("content_signature") or []
    best = (None, 0.0, "")
    for candidate in candidates:
        similarity = _jaccard(signature, candidate.content_signature)
        if similarity > best[1]:
            best = (candidate.artifact_version.artifact, similarity, "near_duplicate_signature")
    return best if best[1] >= 0.92 else (None, 0.0, "")


def _source_acl(configuration, artifact, item):
    permission = dict(item.get("permission_class") or {})
    roots = list(item.get("selected_root_ids") or [])
    return {
        "is_accessible": artifact.lifecycle_state == DriveArtifactState.ACTIVE,
        "provider_revision": _hash(
            {
                "provider_version": item.get("version"),
                "permission": permission,
                "roots": roots,
            }
        ),
        "principal_refs": [
            f"organization:{configuration.organization_id}",
            *(f"drive_scope:{root_id}" for root_id in roots),
        ],
        "group_refs": [],
        "link_sharing": {"mode": permission.get("link_sharing") or "not_requested"},
        "metadata": permission,
    }


def _record_extraction(artifact, processing, *, source_version=None, status=None, work=None):
    extraction, created = DriveDocumentExtraction.objects.get_or_create(
        artifact_version=artifact.current_version,
        parser_version=str(processing.get("parser_version") or DRIVE_PARSER_VERSION)[:64],
        defaults={
            "source_version": source_version,
            "status": status or processing.get("status"),
            "work_classification": work or processing.get("work_classification") or "",
            "parser_name": str(processing.get("parser_name") or "unknown")[:64],
            "export_mime_type": str(processing.get("export_mime_type") or "")[:255],
            "content_hash": str(processing.get("content_hash") or "")[:64],
            "normalized_content_hash": str(processing.get("normalized_content_hash") or "")[:64],
            "content_signature": list(processing.get("content_signature") or []),
            "byte_count": max(int(processing.get("byte_count") or 0), 0),
            "character_count": max(int(processing.get("character_count") or 0), 0),
            "chunk_count": len(processing.get("chunks") or []),
            "warnings": list(processing.get("warnings") or []),
            "parser_report": {
                "meeting": dict(processing.get("meeting") or {}),
                "error": str(processing.get("error") or "")[:1000],
            },
        },
    )
    return extraction, created


def _revoke_previous_source(configuration, artifact, *, reason: str):
    source = MemorySource.objects.filter(
        configuration=configuration,
        provider="google_drive",
        external_id=artifact.file_id,
    ).first()
    if source and source.lifecycle_state not in {
        MemorySourceLifecycle.ACCESS_REVOKED,
        MemorySourceLifecycle.TOMBSTONED,
    }:
        revoke_source_access(source, reason=reason[:512])


def _update_artifact_processing(artifact, extraction):
    artifact.extraction_status = extraction.status
    artifact.work_classification = extraction.work_classification
    artifact.extraction_error = str(extraction.parser_report.get("error") or "")[:10000]
    artifact.extracted_content_hash = extraction.content_hash
    artifact.parser_version = extraction.parser_version
    artifact.extraction_report = {
        "extraction_id": str(extraction.pk),
        "warnings": extraction.warnings,
        "characters": extraction.character_count,
        "chunks": extraction.chunk_count,
    }
    artifact.last_extracted_at = extraction.extracted_at
    artifact.save(
        update_fields=(
            "extraction_status",
            "work_classification",
            "extraction_error",
            "extracted_content_hash",
            "parser_version",
            "extraction_report",
            "last_extracted_at",
            "updated_at",
        )
    )


def _capture_processing_source(
    configuration,
    artifact,
    item,
    processing,
    source_scope,
    meeting,
):
    content_hash = str(processing.get("content_hash") or "")
    permission_hash = _hash(item.get("permission_class") or {})[:12]
    version_key = (
        f"{item.get('version') or item.get('modified_at') or 'content'}:"
        f"{processing.get('parser_version')}:{permission_hash}:{content_hash[:16]}"
    )[:512]
    relation = (
        DriveMeetingRelation.CANONICAL
        if meeting and meeting.canonical_artifact_id == artifact.pk
        else DriveMeetingRelation.SAME_MEETING_AS
    )
    source, source_version, _source_created = capture_source_version(
        organization=configuration.organization,
        provider="google_drive",
        external_account_id=str(
            getattr(configuration.connection, "external_account_id", "")
            or configuration.connection.pk
        ),
        source_type="meeting_transcript",
        external_id=str(item.get("id")),
        version_key=version_key,
        content_hash=content_hash,
        classification=source_scope.default_classification,
        acl=_source_acl(configuration, artifact, item),
        chunks=processing.get("chunks") or (),
        configuration=configuration,
        source_scope=source_scope,
        canonical_url=str(item.get("web_view_url") or ""),
        title=str(item.get("name") or "")[:512],
        source_created_at=item.get("created_at") or None,
        source_updated_at=item.get("modified_at") or None,
        occurred_at=(processing.get("meeting") or {}).get("occurred_at") or None,
        bounded_excerpt=str(processing.get("bounded_excerpt") or "")[:4096],
        metadata={
            "artifact_id": str(artifact.pk),
            "artifact_version_id": str(artifact.current_version_id),
            "mime_type": artifact.mime_type,
            "meeting": dict(processing.get("meeting") or {}),
            "parser_name": processing.get("parser_name"),
            "parser_version": processing.get("parser_version"),
            "parser_warnings": list(processing.get("warnings") or []),
            "lineage_relation": relation,
            "duplicate_suppressed": False,
        },
        restore_access=True,
    )
    return source, source_version


def _restore_unchanged_extraction_access(
    configuration,
    artifact,
    item,
    extraction,
):
    if extraction.status != DriveExtractionStatus.EXTRACTED:
        return
    source_version = extraction.source_version
    if source_version is None:
        raise DriveProcessingError(
            "An extracted Drive record must reference its captured source version."
        )
    source = source_version.source
    if (
        source.configuration_id != configuration.pk
        or source.current_version_id != source_version.pk
    ):
        raise DriveProcessingError(
            "An unchanged Drive extraction must reference the current configured source."
        )
    root_id = str((item.get("selected_root_ids") or [""])[0])
    if not configuration.source_scopes.filter(
        external_id=root_id,
        selected=True,
        status="selected",
    ).exists():
        raise DriveProcessingError(
            "An unchanged Drive record no longer belongs to a selected scope."
        )
    restore_source_access(
        source,
        acl=_source_acl(configuration, artifact, item),
        reason="drive_unchanged_source_still_accessible",
    )


def _process_record(configuration, record: Mapping) -> tuple[bool, str]:
    item = record.get("artifact") if isinstance(record.get("artifact"), Mapping) else None
    processing = record.get("processing") if isinstance(record.get("processing"), Mapping) else None
    if item is None or processing is None:
        raise DriveProcessingError("Drive processing records require artifact and processing objects.")
    artifact, _created, version_created = upsert_drive_artifact(configuration, item)
    if processing.get("status") == "unchanged":
        if version_created:
            raise DriveProcessingError("An unchanged Drive record unexpectedly created a metadata version.")
        extraction = (
            DriveDocumentExtraction.objects.select_related(
                "source_version__source__current_version__acl_snapshot"
            )
            .filter(
                pk=processing.get("existing_extraction_id"),
                artifact_version=artifact.current_version,
                parser_version=str(
                    processing.get("parser_version") or DRIVE_PARSER_VERSION
                )[:64],
            )
            .first()
        )
        if extraction is None:
            raise DriveProcessingError(
                "An unchanged Drive record must reference its existing extraction."
            )
        _restore_unchanged_extraction_access(
            configuration,
            artifact,
            item,
            extraction,
        )
        return version_created, "unchanged"
    existing = artifact.current_version.extractions.filter(
        parser_version=str(processing.get("parser_version") or DRIVE_PARSER_VERSION)[:64],
    ).first()
    if existing:
        _update_artifact_processing(artifact, existing)
        _restore_unchanged_extraction_access(
            configuration,
            artifact,
            item,
            existing,
        )
        return version_created, "unchanged"
    status = str(processing.get("status") or "")
    if status in {DriveExtractionStatus.UNSUPPORTED, DriveExtractionStatus.FAILED}:
        if version_created:
            _revoke_previous_source(
                configuration,
                artifact,
                reason=f"drive_current_version_{status}",
            )
        extraction, _created = _record_extraction(artifact, processing)
        _update_artifact_processing(artifact, extraction)
        return version_created, "failed" if status == DriveExtractionStatus.FAILED else "unsupported"
    if status != DriveExtractionStatus.EXTRACTED:
        raise DriveProcessingError(f"Unknown Drive processing status: {status}")

    root_id = str((item.get("selected_root_ids") or [""])[0])
    source_scope = configuration.source_scopes.filter(
        external_id=root_id,
        selected=True,
        status="selected",
    ).first()
    if source_scope is None:
        raise DriveProcessingError("Drive processing record no longer belongs to a selected scope.")
    meeting = None
    duplicate_of, similarity, duplicate_basis = _duplicate_candidate(
        configuration,
        artifact,
        processing,
        meeting,
    )
    if duplicate_of is None:
        meeting = _meeting_for_processing(configuration, artifact, processing)
        duplicate_of, similarity, duplicate_basis = _duplicate_candidate(
            configuration,
            artifact,
            processing,
            meeting,
        )
    if duplicate_of is not None:
        canonical_link = getattr(duplicate_of, "meeting_link", None)
        if canonical_link is not None:
            meeting = canonical_link.meeting
        if meeting is not None:
            DriveMeetingArtifactLink.objects.update_or_create(
                artifact=artifact,
                defaults={
                    "meeting": meeting,
                    "relation_type": DriveMeetingRelation.COPIED_FROM,
                    "duplicate_of": duplicate_of,
                    "confidence": similarity,
                    "evidence": {"basis": duplicate_basis},
                },
            )
        if version_created:
            _revoke_previous_source(
                configuration,
                artifact,
                reason="drive_duplicate_suppressed",
            )
        extraction, _created = _record_extraction(
            artifact,
            processing,
            status=DriveExtractionStatus.DUPLICATE,
            work=DriveWorkClassification.DUPLICATE_SUPPRESSED,
        )
        _update_artifact_processing(artifact, extraction)
        return version_created, "duplicate"

    if meeting is not None and not meeting.canonical_artifact_id:
        meeting.canonical_artifact = artifact
        meeting.save(update_fields=("canonical_artifact", "updated_at"))
    source, source_version = _capture_processing_source(
        configuration,
        artifact,
        item,
        processing,
        source_scope,
        meeting,
    )
    extraction, _created = _record_extraction(
        artifact,
        processing,
        source_version=source_version,
    )
    _update_artifact_processing(artifact, extraction)
    if meeting is not None:
        relation = (
            DriveMeetingRelation.CANONICAL
            if meeting.canonical_artifact_id == artifact.pk
            else DriveMeetingRelation.SAME_MEETING_AS
        )
        DriveMeetingArtifactLink.objects.update_or_create(
            artifact=artifact,
            defaults={
                "meeting": meeting,
                "relation_type": relation,
                "duplicate_of": None,
                "confidence": 1,
                "evidence": dict((processing.get("meeting") or {}).get("identity_basis") or {}),
            },
        )
    return version_created, "processed"


def _process_removal(configuration, removal: Mapping) -> tuple[bool, bool]:
    file_id = str(removal.get("file_id") or "")
    artifact = DriveDocumentArtifact.objects.filter(
        configuration=configuration,
        file_id=file_id,
    ).first()
    removed, version_created = remove_drive_artifact(configuration, removal)
    if not removed:
        return False, version_created
    source = MemorySource.objects.filter(
        configuration=configuration,
        provider="google_drive",
        external_id=file_id,
    ).first()
    if source and source.lifecycle_state != MemorySourceLifecycle.TOMBSTONED:
        reason = str(removal.get("reason") or "provider_removed")[:512]
        if reason == "access_lost":
            revoke_source_access(source, reason=reason)
        else:
            tombstone_source(source, reason=reason)
    return True, version_created


def _update_reconciliation_report(sync_run, page_checkpoint, outcomes, *, completed):
    manifest = None
    manifest_id = str((page_checkpoint or {}).get("manifest_id") or "")
    if manifest_id:
        manifest = DriveInventoryManifest.objects.filter(pk=manifest_id).first()
    report, _created = DriveReconciliationReport.objects.get_or_create(
        sync_run=sync_run,
        defaults={
            "configuration": sync_run.configuration,
            "manifest": manifest,
            "counts": {},
        },
    )
    counts = dict(report.counts or {})
    for key, value in outcomes.items():
        counts[key] = int(counts.get(key, 0)) + int(value)
    report.counts = counts
    report.last_checkpoint = dict(page_checkpoint or {})
    if completed:
        report.completed_at = timezone.now()
    report.save(
        update_fields=("counts", "last_checkpoint", "completed_at", "updated_at")
    )


@transaction.atomic
def commit_drive_processing_page(
    configuration,
    *,
    records: Iterable[Mapping],
    removals: Iterable[Mapping],
    sync_run=None,
    checkpoint: Optional[Mapping] = None,
    completed: bool = False,
) -> DriveProcessingCommitResult:
    outcomes = {
        "processed": 0,
        "unchanged": 0,
        "duplicate": 0,
        "unsupported": 0,
        "failed": 0,
        "removed": 0,
    }
    records_processed = 0
    removals_processed = 0
    metadata_versions_created = 0
    for record in records:
        version_created, outcome = _process_record(configuration, record)
        records_processed += 1
        metadata_versions_created += int(version_created)
        outcomes[outcome] += 1
    for removal in removals:
        removed, version_created = _process_removal(configuration, removal)
        removals_processed += int(removed)
        metadata_versions_created += int(version_created)
        outcomes["removed"] += int(removed)
    if sync_run is not None:
        _update_reconciliation_report(
            sync_run,
            checkpoint or {},
            outcomes,
            completed=completed,
        )
    return DriveProcessingCommitResult(
        records_processed=records_processed,
        removals_processed=removals_processed,
        metadata_versions_created=metadata_versions_created,
        outcomes=outcomes,
    )
