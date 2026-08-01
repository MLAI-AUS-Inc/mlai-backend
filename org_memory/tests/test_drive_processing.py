import hashlib
import uuid
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from integrations.models import ExternalServiceConnection
from organizations.models import Organization
from org_memory.connectors.google_drive import GoogleDriveMemoryConnector
from org_memory.drive_artifacts import persist_drive_inventory_manifest
from org_memory.drive_processing import (
    DriveProcessingError,
    commit_drive_processing_page,
    prepare_drive_processing_record,
)
from org_memory.kernel import EvidenceKernelError, revoke_configuration_sources
from org_memory.models import (
    DriveDocumentArtifact,
    DriveDocumentExtraction,
    DriveExtractionStatus,
    DriveMeeting,
    DriveMeetingRelation,
    DriveReconciliationReport,
    DriveWorkClassification,
    MemoryActionType,
    MemoryChunk,
    MemoryConnectionConfiguration,
    MemoryConnectionState,
    MemoryOutboxEvent,
    MemoryOutboxEventType,
    MemoryScopeStatus,
    MemorySource,
    MemorySourceActionRequest,
    MemorySourceLifecycle,
    MemorySourceScope,
    MemorySyncRun,
)


GOOGLE_DOC = "application/vnd.google-apps.document"


class ByteRequest:
    def __init__(self, value):
        self.value = value

    def execute(self, **_kwargs):
        return self.value


class ContentFiles:
    def __init__(self, values):
        self.values = dict(values)
        self.calls = []

    def export_media(self, **kwargs):
        self.calls.append(("export_media", kwargs))
        return ByteRequest(self.values[kwargs["fileId"]])

    def get_media(self, **kwargs):
        self.calls.append(("get_media", kwargs))
        return ByteRequest(self.values[kwargs["fileId"]])


class ContentService:
    def __init__(self, values):
        self.file_resource = ContentFiles(values)

    def files(self):
        return self.file_resource


@override_settings(
    ORG_MEMORY_DRIVE_PROCESSING_PAGE_SIZE=1,
    ORG_MEMORY_DRIVE_MAX_DOWNLOAD_BYTES=1024 * 1024,
    ORG_MEMORY_DRIVE_CHUNK_TARGET_CHARS=500,
    ORG_MEMORY_DRIVE_CHUNK_MAX_CHARS=800,
    ORG_MEMORY_DRIVE_CHUNK_OVERLAP_CHARS=50,
)
class DriveProcessingTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="MLAI", domain="processing.mlai.test")
        self.user = get_user_model().objects.create_user(email="processing@mlai.test")
        self.connection = ExternalServiceConnection.objects.create(
            provider="google_drive",
            user=self.user,
            organization=self.organization,
            access_token="encrypted-test-token",
            scopes=["https://www.googleapis.com/auth/drive.readonly"],
            external_account_id="drive-processing-account",
        )
        self.configuration = MemoryConnectionConfiguration.objects.create(
            organization=self.organization,
            provider="google_drive",
            external_connection=self.connection,
            historical_cutoff=timezone.now() - timedelta(days=1000),
            created_by=self.user,
        )
        self.scope = MemorySourceScope.objects.create(
            configuration=self.configuration,
            scope_type="folder",
            external_id="meeting-root",
            name="Meetings",
            selected=True,
            status=MemoryScopeStatus.SELECTED,
            default_classification="committee",
        )

    def item(self, file_id, *, version="1", modified="2026-01-01T00:00:00Z", name=None):
        return {
            "id": file_id,
            "name": name or f"Committee Meeting Transcript 2026-01-01 {file_id}",
            "kind": "file",
            "mime_type": GOOGLE_DOC,
            "size_bytes": None,
            "created_at": "2026-01-01T00:00:00Z",
            "modified_at": modified,
            "version": version,
            "checksums": {},
            "drive_id": "",
            "parent_ids": ["meeting-root"],
            "owners": [{"class": "connection_owned"}],
            "selected_root_ids": ["meeting-root"],
            "lineages": [["meeting-root", file_id]],
            "permission_class": {
                "container": "my_drive",
                "shared": False,
                "permission_count": 1,
                "owned_by_connection": True,
                "download_allowed": True,
                "link_sharing": "not_requested",
            },
            "web_view_url": f"https://drive.google.com/file/d/{file_id}/view",
            "supported": True,
            "transcript_candidate": True,
            "exclusion_reason": None,
            "shortcut": None,
        }

    def _prepare(self, item, content):
        service = ContentService({item["id"]: content})
        record = prepare_drive_processing_record(service, self.configuration, item)
        return record, service

    def test_commit_creates_cited_source_meeting_and_is_idempotent(self):
        item = self.item("doc-1")
        record, service = self._prepare(
            item,
            b"# Committee Meeting\n\n## Decisions\n\nSam: Approved the transcript pilot.",
        )
        result = commit_drive_processing_page(
            self.configuration,
            records=[record],
            removals=[],
        )

        self.assertEqual(result.outcomes["processed"], 1)
        artifact = DriveDocumentArtifact.objects.get(file_id="doc-1")
        source = MemorySource.objects.get(external_id="doc-1")
        extraction = DriveDocumentExtraction.objects.get()
        chunk = MemoryChunk.objects.get()
        self.assertEqual(artifact.extraction_status, DriveExtractionStatus.EXTRACTED)
        self.assertEqual(extraction.source_version_id, source.current_version_id)
        self.assertTrue(chunk.active_for_retrieval)
        self.assertEqual(chunk.source_locator["file_id"], "doc-1")
        self.assertIn("Decisions", chunk.source_locator["sections"])
        self.assertEqual(chunk.source_locator["meeting"]["title"], "committee meeting doc 1")
        self.assertEqual(chunk.source_locator["meeting"]["occurred_at"][:10], "2026-01-01")
        self.assertEqual(chunk.source_locator["meeting"]["timezone"], "Australia/Sydney")
        self.assertEqual(chunk.source_locator["meeting"]["participants"], ["Sam"])
        self.assertEqual(source.current_version.metadata["meeting"]["occurred_at"][:10], "2026-01-01")
        self.assertEqual(artifact.meeting_link.relation_type, DriveMeetingRelation.CANONICAL)
        self.assertEqual(service.file_resource.calls[0][0], "export_media")

        no_download_service = ContentService({})
        unchanged = prepare_drive_processing_record(
            no_download_service,
            self.configuration,
            item,
        )
        replay = commit_drive_processing_page(
            self.configuration,
            records=[unchanged],
            removals=[],
        )
        self.assertEqual(replay.outcomes["unchanged"], 1)
        self.assertEqual(no_download_service.file_resource.calls, [])
        self.assertEqual(MemorySource.objects.count(), 1)
        self.assertEqual(MemoryChunk.objects.count(), 1)
        self.assertEqual(DriveDocumentExtraction.objects.count(), 1)

    def test_copy_is_linked_and_suppressed_as_duplicate_evidence(self):
        body = " ".join(f"agenda{index}" for index in range(100))
        content = f"# Committee Meeting\n\nSam: {body}.".encode("utf-8")
        first = self.item("canonical-1", name="Committee Meeting Transcript 2026-01-02")
        copied = self.item("copy-1", name="Committee Meeting 2026-01-02 - Copy")
        first_record, _service = self._prepare(first, content)
        commit_drive_processing_page(self.configuration, records=[first_record], removals=[])
        canonical_chunk_ids = set(
            MemoryChunk.objects.filter(active_for_retrieval=True).values_list("id", flat=True)
        )
        self.assertTrue(canonical_chunk_ids)
        copy_record, _service = self._prepare(copied, content)
        result = commit_drive_processing_page(self.configuration, records=[copy_record], removals=[])

        duplicate = DriveDocumentArtifact.objects.get(file_id="copy-1")
        canonical = DriveDocumentArtifact.objects.get(file_id="canonical-1")
        self.assertEqual(result.outcomes["duplicate"], 1)
        self.assertEqual(duplicate.extraction_status, DriveExtractionStatus.DUPLICATE)
        self.assertEqual(duplicate.work_classification, DriveWorkClassification.DUPLICATE_SUPPRESSED)
        self.assertEqual(duplicate.meeting_link.duplicate_of_id, canonical.pk)
        self.assertEqual(duplicate.meeting_link.relation_type, DriveMeetingRelation.COPIED_FROM)
        self.assertEqual(MemorySource.objects.count(), 1)
        self.assertSetEqual(
            set(MemoryChunk.objects.filter(active_for_retrieval=True).values_list("id", flat=True)),
            canonical_chunk_ids,
        )
        self.assertEqual(DriveMeeting.objects.count(), 1)

        renamed = self.item("renamed-copy-1", name="Unrelated Export Name 2026-03-04")
        renamed_record, _service = self._prepare(renamed, content)
        renamed_result = commit_drive_processing_page(
            self.configuration,
            records=[renamed_record],
            removals=[],
        )
        self.assertEqual(renamed_result.outcomes["duplicate"], 1)
        self.assertEqual(DriveMeeting.objects.count(), 1)

        near = self.item("near-copy-1", name="Committee Meeting 2026-01-02 (2)")
        near_record, _service = self._prepare(
            near,
            f"# Committee Meeting\n\nSam: {body}!".encode("utf-8"),
        )
        near_result = commit_drive_processing_page(
            self.configuration,
            records=[near_record],
            removals=[],
        )
        near_artifact = DriveDocumentArtifact.objects.get(file_id="near-copy-1")
        self.assertEqual(near_result.outcomes["duplicate"], 1)
        self.assertEqual(near_artifact.meeting_link.duplicate_of_id, canonical.pk)
        self.assertEqual(MemorySource.objects.count(), 1)
        self.assertSetEqual(
            set(MemoryChunk.objects.filter(active_for_retrieval=True).values_list("id", flat=True)),
            canonical_chunk_ids,
        )

    def test_new_parser_version_reprocesses_an_unchanged_drive_version(self):
        item = self.item("parser-upgrade-1")
        first_record, _service = self._prepare(item, b"Sam: Original parser output.")
        commit_drive_processing_page(self.configuration, records=[first_record], removals=[])
        old_chunk_ids = set(MemoryChunk.objects.values_list("id", flat=True))

        with patch("org_memory.drive_processing.DRIVE_PARSER_VERSION", "drive-parser-v3"):
            second_record, service = self._prepare(item, b"Sam: Improved parser output.")
            result = commit_drive_processing_page(
                self.configuration,
                records=[second_record],
                removals=[],
            )

        source = MemorySource.objects.get(external_id="parser-upgrade-1")
        self.assertEqual(result.outcomes["processed"], 1)
        self.assertEqual(len(service.file_resource.calls), 1)
        self.assertEqual(source.versions.count(), 2)
        self.assertEqual(DriveDocumentExtraction.objects.count(), 2)
        self.assertSetEqual(
            set(DriveDocumentExtraction.objects.values_list("parser_version", flat=True)),
            {"drive-parser-v2", "drive-parser-v3"},
        )
        self.assertFalse(
            MemoryChunk.objects.filter(id__in=old_chunk_ids, active_for_retrieval=True).exists()
        )
        self.assertTrue(source.current_version.chunks.filter(active_for_retrieval=True).exists())

    def test_new_unsupported_version_retires_previous_retrievable_evidence(self):
        item = self.item("format-change-1", version="1")
        first_record, _service = self._prepare(item, b"Sam: Previously supported transcript.")
        commit_drive_processing_page(self.configuration, records=[first_record], removals=[])

        changed = self.item(
            "format-change-1",
            version="2",
            modified="2026-01-04T00:00:00Z",
        )
        changed.update(
            mime_type="application/octet-stream",
            supported=False,
            transcript_candidate=False,
            exclusion_reason="unsupported_mime_type",
        )
        service = ContentService({})
        changed_record = prepare_drive_processing_record(service, self.configuration, changed)
        result = commit_drive_processing_page(
            self.configuration,
            records=[changed_record],
            removals=[],
        )

        source = MemorySource.objects.get(external_id="format-change-1")
        self.assertEqual(result.outcomes["unsupported"], 1)
        self.assertEqual(source.lifecycle_state, MemorySourceLifecycle.ACCESS_REVOKED)
        self.assertFalse(MemoryChunk.objects.filter(active_for_retrieval=True).exists())
        self.assertEqual(service.file_resource.calls, [])

        replay_service = ContentService({})
        replay_record = prepare_drive_processing_record(
            replay_service,
            self.configuration,
            changed,
        )
        replay_result = commit_drive_processing_page(
            self.configuration,
            records=[replay_record],
            removals=[],
        )
        self.assertEqual(replay_result.outcomes["unchanged"], 1)
        self.assertEqual(replay_service.file_resource.calls, [])

    def test_corrected_version_retires_old_chunks_and_removal_deactivates_current(self):
        first = self.item("corrected-1", version="1")
        first_record, _service = self._prepare(first, b"Sam: The pilot is proposed.")
        commit_drive_processing_page(self.configuration, records=[first_record], removals=[])
        old_chunk = MemoryChunk.objects.get()

        corrected = self.item(
            "corrected-1",
            version="2",
            modified="2026-01-02T00:00:00Z",
        )
        corrected_record, _service = self._prepare(
            corrected,
            b"Sam: The committee approved the pilot.",
        )
        commit_drive_processing_page(self.configuration, records=[corrected_record], removals=[])
        source = MemorySource.objects.get(external_id="corrected-1")
        old_chunk.refresh_from_db()
        self.assertEqual(source.versions.count(), 2)
        self.assertFalse(old_chunk.active_for_retrieval)
        self.assertEqual(source.current_version.chunks.filter(active_for_retrieval=True).count(), 1)

        removal = commit_drive_processing_page(
            self.configuration,
            records=[],
            removals=[{"file_id": "corrected-1", "reason": "trashed"}],
        )
        source.refresh_from_db()
        self.assertEqual(removal.outcomes["removed"], 1)
        self.assertEqual(source.lifecycle_state, MemorySourceLifecycle.TOMBSTONED)
        self.assertFalse(MemoryChunk.objects.filter(active_for_retrieval=True).exists())

    def test_access_loss_revokes_chunks_and_a_new_access_version_restores_them(self):
        item = self.item("access-1", version="1")
        record, _service = self._prepare(item, b"Sam: Access-controlled transcript.")
        commit_drive_processing_page(self.configuration, records=[record], removals=[])

        commit_drive_processing_page(
            self.configuration,
            records=[],
            removals=[{"file_id": "access-1", "reason": "access_lost"}],
        )
        source = MemorySource.objects.get(external_id="access-1")
        self.assertEqual(source.lifecycle_state, MemorySourceLifecycle.ACCESS_REVOKED)
        self.assertFalse(source.current_version.chunks.filter(active_for_retrieval=True).exists())

        restored = self.item(
            "access-1",
            version="2",
            modified="2026-01-03T00:00:00Z",
        )
        restored_record, _service = self._prepare(
            restored,
            b"Sam: Access-controlled transcript.",
        )
        commit_drive_processing_page(
            self.configuration,
            records=[restored_record],
            removals=[],
        )
        source.refresh_from_db()
        self.assertEqual(source.lifecycle_state, MemorySourceLifecycle.ACTIVE)
        self.assertTrue(source.current_version.chunks.filter(active_for_retrieval=True).exists())

    def test_unchanged_accessible_version_restores_policy_revoked_chunks(self):
        item = self.item("policy-restored-1", version="1")
        record, _service = self._prepare(
            item,
            b"Sam: The committee approved the organisational memory pilot.",
        )
        commit_drive_processing_page(self.configuration, records=[record], removals=[])
        source = MemorySource.objects.get(external_id="policy-restored-1")
        version_id = source.current_version_id
        chunk_ids = set(source.current_version.chunks.values_list("pk", flat=True))

        revoke_configuration_sources(
            self.configuration,
            reason="source_configuration_changed",
        )
        self.configuration.lifecycle_state = MemoryConnectionState.SCOPED
        self.configuration.save(update_fields=("lifecycle_state", "updated_at"))
        source.refresh_from_db()
        self.assertEqual(source.lifecycle_state, MemorySourceLifecycle.ACCESS_REVOKED)
        self.assertFalse(source.current_version.acl_snapshot.is_accessible)
        self.assertFalse(source.current_version.chunks.filter(active_for_retrieval=True).exists())

        no_download_service = ContentService({})
        unchanged = prepare_drive_processing_record(
            no_download_service,
            self.configuration,
            item,
        )
        with self.assertRaises(EvidenceKernelError):
            commit_drive_processing_page(
                self.configuration,
                records=[unchanged],
                removals=[],
            )
        source.refresh_from_db()
        self.assertEqual(source.lifecycle_state, MemorySourceLifecycle.ACCESS_REVOKED)

        self.configuration.lifecycle_state = MemoryConnectionState.ACTIVE
        self.configuration.save(update_fields=("lifecycle_state", "updated_at"))
        replay = commit_drive_processing_page(
            self.configuration,
            records=[unchanged],
            removals=[],
        )

        source.refresh_from_db()
        source.current_version.acl_snapshot.refresh_from_db()
        self.assertEqual(replay.outcomes["unchanged"], 1)
        self.assertEqual(no_download_service.file_resource.calls, [])
        self.assertEqual(source.current_version_id, version_id)
        self.assertEqual(source.lifecycle_state, MemorySourceLifecycle.ACTIVE)
        self.assertIsNone(source.access_revoked_at)
        self.assertTrue(source.current_version.acl_snapshot.is_accessible)
        self.assertIsNone(source.current_version.acl_snapshot.revoked_at)
        self.assertSetEqual(
            set(source.current_version.chunks.values_list("pk", flat=True)),
            chunk_ids,
        )
        self.assertTrue(
            source.current_version.chunks.filter(active_for_retrieval=True).exists()
        )
        self.assertTrue(
            MemoryOutboxEvent.objects.filter(
                source=source,
                source_version_id=version_id,
                event_type=MemoryOutboxEventType.SOURCE_ACCESS_RESTORED,
            ).exists()
        )

    def test_audio_and_download_restrictions_create_visible_unsupported_work(self):
        audio = self.item("audio-1", name="Town Hall Recording.mp3")
        audio.update(
            mime_type="audio/mpeg",
            supported=False,
            transcript_candidate=False,
            exclusion_reason="unsupported_mime_type",
        )
        restricted = self.item("restricted-1")
        restricted["permission_class"]["download_allowed"] = False
        service = ContentService({})

        audio_record = prepare_drive_processing_record(service, self.configuration, audio)
        restricted_record = prepare_drive_processing_record(
            service,
            self.configuration,
            restricted,
        )
        result = commit_drive_processing_page(
            self.configuration,
            records=[audio_record, restricted_record],
            removals=[],
        )

        self.assertEqual(result.outcomes["unsupported"], 2)
        self.assertEqual(
            DriveDocumentArtifact.objects.get(file_id="audio-1").work_classification,
            DriveWorkClassification.NEEDS_TRANSCRIPTION,
        )
        self.assertEqual(
            DriveDocumentArtifact.objects.get(file_id="restricted-1").work_classification,
            DriveWorkClassification.DOWNLOAD_RESTRICTED,
        )
        self.assertEqual(service.file_resource.calls, [])
        self.assertEqual(MemorySource.objects.count(), 0)

        escaped = self.item("escaped-1")
        escaped["selected_root_ids"] = ["unselected-root"]
        with self.assertRaises(DriveProcessingError):
            prepare_drive_processing_record(service, self.configuration, escaped)
        self.assertEqual(service.file_resource.calls, [])

    def test_oldest_first_checkpoint_resume_and_reconciliation_report(self):
        newer = self.item("newer-1", modified="2026-02-01T00:00:00Z")
        older = self.item("older-1", modified="2026-01-01T00:00:00Z")
        result = {
            "inventory_id": str(uuid.uuid4()),
            "selected_roots": ["meeting-root"],
            "historical_cutoff": "2024-01-01T00:00:00Z",
            "allowed_mime_types": [GOOGLE_DOC],
            "partial": False,
            "ceiling_reason": None,
            "counts": {"candidate_transcripts": 2},
            "formats": {GOOGLE_DOC: 2},
            "owners": {"connection_owned": 2},
            "date_range": {},
            "estimated": {},
            "warnings": [],
            "items": [newer, older],
        }
        manifest, _created = persist_drive_inventory_manifest(
            configuration=self.configuration,
            scopes=[self.scope],
            result=result,
            start_page_token="drive-start-token",
        )
        action = MemorySourceActionRequest.objects.create(
            configuration=self.configuration,
            action=MemoryActionType.BACKFILL,
            idempotency_key="drive-backfill-test",
        )
        sync_run = MemorySyncRun.objects.create(
            organization=self.organization,
            configuration=self.configuration,
            action_request=action,
            provider="google_drive",
            action_type=MemoryActionType.BACKFILL,
        )
        service = ContentService(
            {
                "older-1": b"Sam: Older meeting transcript.",
                "newer-1": b"Alex: Newer meeting transcript.",
            }
        )
        connector = GoogleDriveMemoryConnector()
        checkpoint = {"manifest_id": str(manifest.pk), "offset": 0}

        with patch("org_memory.connectors.google_drive.build_drive_service", return_value=service):
            first_page = connector.backfill(self.configuration, [self.scope], checkpoint)
        self.assertEqual(first_page.records[0]["artifact"]["id"], "older-1")
        self.assertTrue(first_page.has_more)
        commit_drive_processing_page(
            self.configuration,
            records=first_page.records,
            removals=first_page.removals,
            sync_run=sync_run,
            checkpoint=first_page.checkpoint,
            completed=False,
        )

        with patch("org_memory.connectors.google_drive.build_drive_service", return_value=service):
            second_page = connector.backfill(
                self.configuration,
                [self.scope],
                first_page.checkpoint,
            )
        self.assertEqual(second_page.records[0]["artifact"]["id"], "newer-1")
        self.assertFalse(second_page.has_more)
        self.assertEqual(second_page.next_cursor, "drive-start-token")
        commit_drive_processing_page(
            self.configuration,
            records=second_page.records,
            removals=second_page.removals,
            sync_run=sync_run,
            checkpoint=second_page.checkpoint,
            completed=True,
        )

        report = DriveReconciliationReport.objects.get(sync_run=sync_run)
        self.assertEqual(report.counts["processed"], 2)
        self.assertIsNotNone(report.completed_at)
        self.assertEqual(MemorySource.objects.count(), 2)

        calls_before = len(service.file_resource.calls)
        with patch("org_memory.connectors.google_drive.build_drive_service", return_value=service):
            unchanged_page = connector.backfill(self.configuration, [self.scope], checkpoint)
        self.assertEqual(unchanged_page.records[0]["processing"]["status"], "unchanged")
        self.assertEqual(len(service.file_resource.calls), calls_before)
