import hashlib
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from integrations.models import ExternalServiceConnection
from organizations.models import Organization
from org_memory.connectors.google_drive import GoogleDriveMemoryConnector
from org_memory.drive_artifacts import (
    DriveArtifactError,
    commit_drive_metadata_page,
    upsert_drive_artifact,
)
from org_memory.drive_processing import commit_drive_processing_page
from org_memory.drive_watch import renew_drive_watch
from org_memory.models import (
    DriveArtifactState,
    DriveDocumentArtifact,
    DriveDocumentArtifactVersion,
    DriveInventoryManifest,
    DriveWatchChannel,
    MemoryChunk,
    MemoryConnectionConfiguration,
    MemoryConnectionState,
    MemoryScopeStatus,
    MemorySource,
    MemorySourceScope,
)


FOLDER = "application/vnd.google-apps.folder"
GOOGLE_DOC = "application/vnd.google-apps.document"
SHORTCUT = "application/vnd.google-apps.shortcut"


class FakeRequest:
    def __init__(self, result):
        self.result = result

    def execute(self, **_kwargs):
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class FakeResource:
    def __init__(
        self,
        *,
        get_results=None,
        list_results=None,
        start_tokens=None,
        watch_results=None,
        content_results=None,
    ):
        self.get_results = dict(get_results or {})
        self.list_results = list(list_results or [])
        self.start_tokens = list(start_tokens or [])
        self.watch_results = list(watch_results or [])
        self.content_results = dict(content_results or {})
        self.calls = []

    def get(self, **kwargs):
        self.calls.append(("get", kwargs))
        return FakeRequest(self.get_results[kwargs["fileId"]])

    def list(self, **kwargs):
        self.calls.append(("list", kwargs))
        return FakeRequest(self.list_results.pop(0))

    def getStartPageToken(self, **kwargs):
        self.calls.append(("getStartPageToken", kwargs))
        return FakeRequest(self.start_tokens.pop(0))

    def watch(self, **kwargs):
        self.calls.append(("watch", kwargs))
        return FakeRequest(self.watch_results.pop(0))

    def export_media(self, **kwargs):
        self.calls.append(("export_media", kwargs))
        return FakeRequest(self.content_results[kwargs["fileId"]])

    def get_media(self, **kwargs):
        self.calls.append(("get_media", kwargs))
        return FakeRequest(self.content_results[kwargs["fileId"]])


class FakeDriveService:
    def __init__(self, *, files=None, drives=None, changes=None):
        self._files = files or FakeResource()
        self._drives = drives or FakeResource(list_results=[{"drives": []}])
        self._changes = changes or FakeResource()

    def files(self):
        return self._files

    def drives(self):
        return self._drives

    def changes(self):
        return self._changes


def drive_file(
    file_id,
    name,
    *,
    mime_type=GOOGLE_DOC,
    parents=None,
    drive_id="",
    version="1",
    trashed=False,
    shortcut=None,
    shared=False,
    owned=True,
):
    return {
        "id": file_id,
        "name": name,
        "mimeType": mime_type,
        "parents": list(parents or []),
        "driveId": drive_id,
        "version": version,
        "createdTime": "2025-01-01T00:00:00Z",
        "modifiedTime": "2026-07-01T00:00:00Z",
        "trashed": trashed,
        "ownedByMe": owned,
        "shared": shared,
        "permissionIds": ["permission-1"],
        "capabilities": {"canDownload": True},
        "webViewLink": f"https://drive.google.com/file/d/{file_id}/view",
        "shortcutDetails": shortcut,
    }


@override_settings(
    ORG_MEMORY_DRIVE_INVENTORY_MAX_FILES=100,
    ORG_MEMORY_DRIVE_INVENTORY_MAX_PAGES=20,
    ORG_MEMORY_DRIVE_INVENTORY_MAX_SECONDS=30,
    ORG_MEMORY_DRIVE_SYNC_PAGE_SIZE=100,
)
class GoogleDriveConnectorTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="MLAI", domain="drive.mlai.test")
        self.user = get_user_model().objects.create_user(email="drive@mlai.test")
        self.connection = ExternalServiceConnection.objects.create(
            provider="google_drive",
            user=self.user,
            organization=self.organization,
            access_token="encrypted-test-access-token",
            refresh_token="encrypted-test-refresh-token",
            scopes=["https://www.googleapis.com/auth/drive.readonly"],
            external_account_id="drive-account-1",
        )
        self.configuration = MemoryConnectionConfiguration.objects.create(
            organization=self.organization,
            provider="google_drive",
            external_connection=self.connection,
            historical_cutoff=timezone.now() - timedelta(days=730),
            created_by=self.user,
        )
        self.folder_scope = MemorySourceScope.objects.create(
            configuration=self.configuration,
            scope_type="folder",
            external_id="root-folder-1",
            name="Meeting transcripts",
            selected=True,
            status=MemoryScopeStatus.SELECTED,
        )
        self.shared_scope = MemorySourceScope.objects.create(
            configuration=self.configuration,
            scope_type="shared_drive",
            external_id="shared-drive-1",
            name="Company Shared Drive",
            selected=True,
            status=MemoryScopeStatus.SELECTED,
        )
        self.connector = GoogleDriveMemoryConnector()

    def _artifact_item(self, file_id="doc-1", *, version="1", permission_count=1):
        return {
            "id": file_id,
            "name": "Weekly meeting transcript",
            "kind": "file",
            "mime_type": GOOGLE_DOC,
            "size_bytes": None,
            "created_at": "2025-01-01T00:00:00Z",
            "modified_at": "2026-07-01T00:00:00Z",
            "version": version,
            "checksums": {},
            "drive_id": "",
            "parent_ids": ["root-folder-1"],
            "owners": [{"class": "connection_owned"}],
            "selected_root_ids": ["root-folder-1"],
            "lineages": [["root-folder-1", file_id]],
            "permission_class": {
                "container": "my_drive",
                "shared": False,
                "permission_count": permission_count,
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

    def test_scope_discovery_lists_my_drive_folders_and_shared_drives_with_pagination(self):
        files = FakeResource(
            get_results={"root": drive_file("root-folder-1", "My Drive", mime_type=FOLDER)},
            list_results=[
                {
                    "files": [drive_file("folder-2", "Operations", mime_type=FOLDER)],
                    "nextPageToken": "folders-page-2",
                }
            ],
        )
        drives = FakeResource(
            list_results=[
                {
                    "drives": [{"id": "shared-drive-1", "name": "Company"}],
                    "nextPageToken": "drives-page-2",
                }
            ]
        )
        service = FakeDriveService(files=files, drives=drives)

        with patch("org_memory.connectors.google_drive.build_drive_service", return_value=service):
            page = self.connector.discover_scopes(self.configuration)

        self.assertEqual(
            {(scope.scope_type, scope.external_id) for scope in page.scopes},
            {
                ("folder", "root-folder-1"),
                ("folder", "folder-2"),
                ("shared_drive", "shared-drive-1"),
            },
        )
        self.assertTrue(page.next_cursor)
        self.assertTrue(all("access_token" not in dict(scope.metadata) for scope in page.scopes))
        list_kwargs = [kwargs for method, kwargs in files.calls if method == "list"][0]
        self.assertTrue(list_kwargs["includeItemsFromAllDrives"])
        self.assertTrue(list_kwargs["supportsAllDrives"])

    @override_settings(ORG_MEMORY_DRIVE_WATCH_RENEW_SECONDS=86400)
    def test_drive_watch_renewal_is_noop_until_channel_nears_expiry(self):
        existing = DriveWatchChannel.objects.create(
            configuration=self.configuration,
            channel_id="healthy-channel",
            resource_id="healthy-resource",
            token_hash="a" * 64,
            expiration_at=timezone.now() + timedelta(days=3),
        )

        with patch("org_memory.drive_watch.register_drive_watch") as register:
            result = renew_drive_watch(self.configuration)

        self.assertEqual(result.pk, existing.pk)
        register.assert_not_called()

    @override_settings(ORG_MEMORY_DRIVE_WATCH_RENEW_SECONDS=86400)
    def test_drive_watch_renewal_replaces_channel_inside_renewal_window(self):
        DriveWatchChannel.objects.create(
            configuration=self.configuration,
            channel_id="expiring-channel",
            resource_id="expiring-resource",
            token_hash="b" * 64,
            expiration_at=timezone.now() + timedelta(hours=1),
        )
        replacement = DriveWatchChannel(
            configuration=self.configuration,
            channel_id="replacement-channel",
            resource_id="replacement-resource",
            token_hash="c" * 64,
            expiration_at=timezone.now() + timedelta(days=6),
        )

        with patch(
            "org_memory.drive_watch.register_drive_watch",
            return_value=replacement,
        ) as register:
            result = renew_drive_watch(self.configuration)

        self.assertEqual(result.channel_id, "replacement-channel")
        register.assert_called_once_with(self.configuration)

    def test_preview_and_dry_run_are_metadata_only_before_approved_backfill(self):
        native_doc = drive_file(
            "doc-native-1",
            "Leadership meeting transcript",
            parents=["root-folder-1"],
        )
        shortcut = drive_file(
            "shortcut-1",
            "Transcript shortcut",
            mime_type=SHORTCUT,
            parents=["root-folder-1"],
            shortcut={"targetId": "doc-native-1", "targetMimeType": GOOGLE_DOC},
        )
        files = FakeResource(
            get_results={
                "root-folder-1": drive_file("root-folder-1", "Transcripts", mime_type=FOLDER),
                "shared-drive-1": drive_file(
                    "shared-drive-1", "Company", mime_type=FOLDER, drive_id="shared-drive-1"
                ),
            },
            list_results=[{"files": [native_doc, shortcut]}, {"files": []}],
            content_results={
                "doc-native-1": b"# Leadership Meeting\n\nSam: Approved the transcript pilot.",
            },
        )
        changes = FakeResource(start_tokens=[{"startPageToken": "start-token-1"}])
        service = FakeDriveService(files=files, changes=changes)

        with patch("org_memory.connectors.google_drive.build_drive_service", return_value=service), patch(
            "org_memory.connectors.google_drive.assert_provider_inventory_allowed"
        ):
            preview = self.connector.preview(
                self.configuration,
                [self.folder_scope, self.shared_scope],
                {},
            )

        self.assertFalse(preview.summary["content_activated"])
        manifest = DriveInventoryManifest.objects.get(pk=preview.summary["manifest_id"])
        self.assertEqual(manifest.start_page_token, "start-token-1")
        self.assertEqual(manifest.owners, {"connection_owned": 2})
        self.assertTrue(all("display_name" not in str(item.get("owners")) for item in manifest.snapshot))

        dry_run = self.connector.dry_run(
            self.configuration,
            [self.folder_scope, self.shared_scope],
            {},
        )
        self.assertTrue(dry_run.summary["approval_ready"])
        self.assertEqual(MemorySource.objects.count(), 0)
        self.assertEqual(MemoryChunk.objects.count(), 0)
        with patch("org_memory.connectors.google_drive.build_drive_service", return_value=service):
            page = self.connector.backfill(
                self.configuration,
                [self.folder_scope, self.shared_scope],
                {"manifest_id": str(manifest.pk)},
            )
        self.assertFalse(page.has_more)
        self.assertEqual(page.next_cursor, "start-token-1")
        commit_drive_processing_page(
            self.configuration,
            records=page.records,
            removals=page.removals,
        )
        self.assertEqual(DriveDocumentArtifact.objects.count(), 2)
        self.assertEqual(MemorySource.objects.count(), 1)
        self.assertEqual(MemoryChunk.objects.count(), 1)
        called_methods = {method for method, _kwargs in files.calls}
        self.assertEqual(called_methods, {"get", "list", "export_media"})

    def test_artifact_versions_are_idempotent_and_scope_escape_is_rejected(self):
        artifact, created, version_created = upsert_drive_artifact(
            self.configuration,
            self._artifact_item(),
        )
        self.assertTrue(created)
        self.assertTrue(version_created)
        _artifact, created, version_created = upsert_drive_artifact(
            self.configuration,
            self._artifact_item(),
        )
        self.assertFalse(created)
        self.assertFalse(version_created)

        changed = self._artifact_item(version="2", permission_count=2)
        artifact, _created, version_created = upsert_drive_artifact(self.configuration, changed)
        self.assertTrue(version_created)
        self.assertEqual(artifact.versions.count(), 2)
        self.assertEqual(artifact.versions.filter(is_current=True).count(), 1)
        self.assertEqual(artifact.current_version.acl_snapshot["permission_class"]["permission_count"], 2)

        records, removals, versions = commit_drive_metadata_page(
            self.configuration,
            records=(),
            removals=({"file_id": "doc-1", "reason": "missing_from_selected_inventory"},),
        )
        artifact.refresh_from_db()
        self.assertEqual((records, removals, versions), (0, 1, 1))
        self.assertEqual(artifact.lifecycle_state, DriveArtifactState.REMOVED)
        self.assertEqual(artifact.versions.count(), 3)

        escaped = self._artifact_item(file_id="escaped-1")
        escaped["selected_root_ids"] = ["unselected-folder"]
        with self.assertRaises(DriveArtifactError):
            upsert_drive_artifact(self.configuration, escaped)

    def test_changes_feed_handles_shared_drive_shortcut_removals_and_pagination(self):
        removed, _created, _version = upsert_drive_artifact(
            self.configuration,
            self._artifact_item(file_id="removed-1"),
        )
        trashed, _created, _version = upsert_drive_artifact(
            self.configuration,
            self._artifact_item(file_id="trashed-1"),
        )
        native = drive_file("native-2", "Standup transcript", parents=["root-folder-1"])
        shared = drive_file(
            "shared-doc-1",
            "Board meeting transcript",
            drive_id="shared-drive-1",
            parents=["shared-folder-2"],
            owned=False,
            shared=True,
        )
        shortcut = drive_file(
            "shortcut-2",
            "Meeting shortcut",
            mime_type=SHORTCUT,
            parents=["root-folder-1"],
            shortcut={"targetId": "native-2", "targetMimeType": GOOGLE_DOC},
        )
        outside = drive_file("outside-1", "Outside transcript", parents=["outside-parent"])
        files = FakeResource(
            get_results={
                "outside-parent": drive_file("outside-parent", "Elsewhere", mime_type=FOLDER),
            },
            content_results={
                "native-2": b"Sam: Native standup transcript.",
                "shared-doc-1": b"Alex: Shared board meeting transcript.",
            },
        )
        changes = FakeResource(
            list_results=[
                {
                    "changes": [
                        {"fileId": "native-2", "file": native},
                        {"fileId": "shared-doc-1", "file": shared},
                        {"fileId": "shortcut-2", "file": shortcut},
                        {"fileId": "outside-1", "file": outside},
                        {"fileId": "removed-1", "removed": True, "time": "2026-07-20T00:00:00Z"},
                        {"fileId": "trashed-1", "file": {**drive_file("trashed-1", "Old"), "trashed": True}},
                    ],
                    "nextPageToken": "change-page-2",
                },
                {"changes": [], "newStartPageToken": "change-cursor-current"},
            ]
        )
        service = FakeDriveService(files=files, changes=changes)

        with patch("org_memory.connectors.google_drive.build_drive_service", return_value=service):
            page = self.connector.incremental_sync(self.configuration, "change-page-1")

        self.assertTrue(page.has_more)
        self.assertEqual(page.next_cursor, "change-page-2")
        self.assertEqual(
            {item["artifact"]["id"] for item in page.records},
            {"native-2", "shared-doc-1", "shortcut-2"},
        )
        self.assertEqual(
            {(item["file_id"], item["reason"]) for item in page.removals},
            {("removed-1", "access_lost"), ("trashed-1", "trashed")},
        )
        commit_drive_processing_page(
            self.configuration,
            records=page.records,
            removals=page.removals,
        )
        removed.refresh_from_db()
        trashed.refresh_from_db()
        self.assertEqual(removed.lifecycle_state, DriveArtifactState.ACCESS_LOST)
        self.assertEqual(trashed.lifecycle_state, DriveArtifactState.TRASHED)
        self.assertFalse(DriveDocumentArtifact.objects.filter(file_id="outside-1").exists())
        list_kwargs = [kwargs for method, kwargs in changes.calls if method == "list"][0]
        self.assertTrue(list_kwargs["includeRemoved"])
        self.assertTrue(list_kwargs["includeItemsFromAllDrives"])

        with patch("org_memory.connectors.google_drive.build_drive_service", return_value=service):
            final_page = self.connector.incremental_sync(self.configuration, page.next_cursor)
        self.assertFalse(final_page.has_more)
        self.assertEqual(final_page.next_cursor, "change-cursor-current")

    def test_webhook_rejects_forgery_deduplicates_and_only_wakes_on_change(self):
        self.configuration.lifecycle_state = MemoryConnectionState.ACTIVE
        future = timezone.now() + timedelta(hours=12)
        self.configuration.next_scheduled_sync_at = future
        self.configuration.save(update_fields=("lifecycle_state", "next_scheduled_sync_at", "updated_at"))
        token = "drive-channel-secret"
        channel = DriveWatchChannel.objects.create(
            configuration=self.configuration,
            channel_id="channel-1",
            resource_id="resource-1",
            token_hash=hashlib.sha256(token.encode("utf-8")).hexdigest(),
            expiration_at=timezone.now() + timedelta(days=1),
        )
        client = APIClient()
        headers = {
            "HTTP_X_GOOG_CHANNEL_ID": "channel-1",
            "HTTP_X_GOOG_RESOURCE_ID": "resource-1",
            "HTTP_X_GOOG_CHANNEL_TOKEN": token,
            "HTTP_X_GOOG_RESOURCE_STATE": "change",
            "HTTP_X_GOOG_MESSAGE_NUMBER": "1",
        }

        accepted = client.post("/api/v1/org-memory/webhooks/google-drive/changes", {}, **headers)
        self.assertEqual(accepted.status_code, 202)
        self.assertTrue(accepted.data["wake_scheduled"])
        self.configuration.refresh_from_db()
        self.assertLess(self.configuration.next_scheduled_sync_at, future)

        duplicate = client.post("/api/v1/org-memory/webhooks/google-drive/changes", {}, **headers)
        self.assertEqual(duplicate.status_code, 202)
        self.assertEqual(duplicate.data["status"], "duplicate")
        forged = client.post(
            "/api/v1/org-memory/webhooks/google-drive/changes",
            {},
            **{**headers, "HTTP_X_GOOG_CHANNEL_TOKEN": "wrong-token", "HTTP_X_GOOG_MESSAGE_NUMBER": "2"},
        )
        self.assertEqual(forged.status_code, 401)

        future_again = timezone.now() + timedelta(hours=12)
        self.configuration.next_scheduled_sync_at = future_again
        self.configuration.save(update_fields=("next_scheduled_sync_at", "updated_at"))
        sync = client.post(
            "/api/v1/org-memory/webhooks/google-drive/changes",
            {},
            **{**headers, "HTTP_X_GOOG_RESOURCE_STATE": "sync", "HTTP_X_GOOG_MESSAGE_NUMBER": "2"},
        )
        self.assertEqual(sync.status_code, 202)
        self.assertFalse(sync.data["wake_scheduled"])
        self.configuration.refresh_from_db()
        self.assertEqual(self.configuration.next_scheduled_sync_at, future_again)
        channel.refresh_from_db()
        self.assertEqual(channel.last_message_number, 2)
        self.assertNotEqual(channel.token_hash, token)
