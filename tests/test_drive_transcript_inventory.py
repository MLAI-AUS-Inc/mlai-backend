from __future__ import annotations

import json
import stat
import tempfile
from datetime import date, datetime, timezone
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase

from integrations.models import ExternalServiceConnectionStatus, ExternalServiceProvider
from org_memory.drive_inventory import (
    DOCX_MIME_TYPE,
    FOLDER_MIME_TYPE,
    PDF_MIME_TYPE,
    SHORTCUT_MIME_TYPE,
    VTT_MIME_TYPE,
    DriveInventoryError,
    DriveInventoryLimits,
    GoogleDriveMetadataClient,
    build_drive_service,
    inventory_drive_metadata,
    local_cutoff_to_utc,
    validate_drive_id,
)


ROOT_ID = "root_123"
CHILD_FOLDER_ID = "folder_456"


def folder(file_id, name, **overrides):
    return {
        "id": file_id,
        "name": name,
        "mimeType": FOLDER_MIME_TYPE,
        "modifiedTime": "2026-01-01T00:00:00Z",
        "trashed": False,
        **overrides,
    }


def drive_file(file_id, name, mime_type, **overrides):
    return {
        "id": file_id,
        "name": name,
        "mimeType": mime_type,
        "createdTime": "2026-07-01T00:00:00Z",
        "modifiedTime": "2026-07-10T00:00:00Z",
        "version": "1",
        "size": "4000",
        "ownedByMe": True,
        "shared": True,
        "permissionIds": ["permission-1", "permission-2"],
        "capabilities": {"canDownload": True},
        "webViewLink": f"https://drive.example/{file_id}",
        **overrides,
    }


class FakeDriveClient:
    def __init__(self, *, roots=None, pages=None):
        self.roots = roots or {ROOT_ID: folder(ROOT_ID, "Pilot transcripts")}
        self.pages = pages or {}
        self.list_calls = []

    def get_file(self, file_id):
        return self.roots[file_id]

    def list_children(
        self,
        folder_id,
        *,
        modified_after_rfc3339,
        page_token,
        page_size,
    ):
        self.list_calls.append(
            {
                "folder_id": folder_id,
                "modified_after": modified_after_rfc3339,
                "page_token": page_token,
                "page_size": page_size,
            }
        )
        return self.pages.get((folder_id, page_token), ([], None))


class DriveInventoryEngineTests(SimpleTestCase):
    def test_inventory_is_metadata_only_paginated_and_traverses_folders(self):
        client = FakeDriveClient(
            pages={
                (ROOT_ID, None): (
                    [
                        folder(CHILD_FOLDER_ID, "2025 archive"),
                        drive_file(
                            "doc_001",
                            "Committee Meeting Transcript.docx",
                            DOCX_MIME_TYPE,
                            md5Checksum="checksum-one",
                            raw_body="THIS MUST NOT APPEAR",
                        ),
                        drive_file("sheet_001", "Budget", "application/vnd.google-apps.spreadsheet"),
                    ],
                    "page-two",
                ),
                (ROOT_ID, "page-two"): (
                    [
                        drive_file(
                            "doc_002",
                            "Committee Meeting Transcript copy.docx",
                            DOCX_MIME_TYPE,
                            md5Checksum="checksum-one",
                        ),
                        {
                            "id": "shortcut_1",
                            "name": "External transcripts",
                            "mimeType": SHORTCUT_MIME_TYPE,
                            "modifiedTime": "2026-07-11T00:00:00Z",
                            "shortcutDetails": {
                                "targetId": "external_999",
                                "targetMimeType": FOLDER_MIME_TYPE,
                            },
                        },
                    ],
                    None,
                ),
                (CHILD_FOLDER_ID, None): (
                    [drive_file("caption_1", "2026-07-10 support sync.vtt", VTT_MIME_TYPE)],
                    None,
                ),
            }
        )

        result = inventory_drive_metadata(
            client,
            organization_id="org-1",
            connection_id="connection-1",
            folder_ids=[ROOT_ID],
            modified_after=date(2026, 7, 1),
            now=lambda: datetime(2026, 7, 20, 0, 0, tzinfo=timezone.utc),
        )

        self.assertEqual(result["counts"]["pages"], 3)
        self.assertEqual(result["counts"]["candidate_transcripts"], 3)
        self.assertEqual(result["counts"]["duplicates"], 1)
        self.assertEqual(result["counts"]["unsupported"], 2)
        self.assertFalse(result["partial"])
        self.assertEqual(
            [call["page_token"] for call in client.list_calls[:2]],
            [None, "page-two"],
        )
        self.assertIn(CHILD_FOLDER_ID, [call["folder_id"] for call in client.list_calls])
        self.assertNotIn("THIS MUST NOT APPEAR", json.dumps(result))
        self.assertNotIn("permission-1", json.dumps(result))

        duplicate = next(item for item in result["items"] if item["id"] == "doc_002")
        self.assertEqual(duplicate["duplicate_kind"], "exact_checksum")
        self.assertEqual(duplicate["duplicate_of"], "doc_001")
        shortcut = next(item for item in result["items"] if item["id"] == "shortcut_1")
        self.assertEqual(shortcut["exclusion_reason"], "shortcut_not_followed")

    def test_inventory_id_and_sorted_items_are_stable_for_same_snapshot(self):
        pages = {
            (ROOT_ID, None): (
                [
                    drive_file("doc_999", "Z meeting transcript.pdf", PDF_MIME_TYPE),
                    drive_file("doc_111", "A meeting transcript.pdf", PDF_MIME_TYPE),
                ],
                None,
            )
        }

        first = inventory_drive_metadata(
            FakeDriveClient(pages=pages),
            organization_id="org-1",
            connection_id="connection-1",
            folder_ids=[ROOT_ID],
            modified_after=date(2026, 7, 1),
            now=lambda: datetime(2026, 7, 20, 0, 0, tzinfo=timezone.utc),
        )
        second = inventory_drive_metadata(
            FakeDriveClient(pages=pages),
            organization_id="org-1",
            connection_id="connection-1",
            folder_ids=[ROOT_ID],
            modified_after=date(2026, 7, 1),
            now=lambda: datetime(2026, 7, 21, 0, 0, tzinfo=timezone.utc),
        )

        self.assertEqual(first["inventory_id"], second["inventory_id"])
        self.assertEqual([item["id"] for item in first["items"]], ["doc_111", "doc_999"])

    def test_file_ceiling_stops_safely_and_marks_partial(self):
        client = FakeDriveClient(
            pages={
                (ROOT_ID, None): (
                    [
                        drive_file("doc_001", "Meeting transcript one.pdf", PDF_MIME_TYPE),
                        drive_file("doc_002", "Meeting transcript two.pdf", PDF_MIME_TYPE),
                    ],
                    "more",
                )
            }
        )

        result = inventory_drive_metadata(
            client,
            organization_id="org-1",
            connection_id="connection-1",
            folder_ids=[ROOT_ID],
            modified_after=date(2026, 7, 1),
            limits=DriveInventoryLimits(max_files=1, max_pages=10, max_seconds=30),
        )

        self.assertTrue(result["partial"])
        self.assertEqual(result["ceiling_reason"], "max_files")
        self.assertEqual(result["counts"]["records_seen"], 1)

    def test_supported_caption_extension_survives_generic_drive_mime_type(self):
        client = FakeDriveClient(
            pages={
                (ROOT_ID, None): (
                    [drive_file("caption_002", "Support sync.srt", "application/octet-stream")],
                    None,
                )
            }
        )

        result = inventory_drive_metadata(
            client,
            organization_id="org-1",
            connection_id="connection-1",
            folder_ids=[ROOT_ID],
            modified_after=date(2026, 7, 1),
        )

        item = next(item for item in result["items"] if item["id"] == "caption_002")
        self.assertTrue(item["supported"])
        self.assertTrue(item["transcript_candidate"])

    def test_invalid_or_non_folder_roots_are_rejected(self):
        with self.assertRaises(DriveInventoryError):
            validate_drive_id("bad id with spaces")

        client = FakeDriveClient(
            roots={ROOT_ID: drive_file(ROOT_ID, "Not a folder.pdf", PDF_MIME_TYPE)}
        )
        with self.assertRaisesMessage(DriveInventoryError, "is not a Google Drive folder"):
            inventory_drive_metadata(
                client,
                organization_id="org-1",
                connection_id="connection-1",
                folder_ids=[ROOT_ID],
                modified_after=date(2026, 7, 1),
            )

    def test_sydney_cutoff_is_normalised_to_utc(self):
        cutoff = local_cutoff_to_utc(date(2026, 7, 20))
        self.assertEqual(cutoff.isoformat(), "2026-07-19T14:00:00+00:00")


class GoogleDriveMetadataClientTests(SimpleTestCase):
    def test_list_query_keeps_old_folders_for_traversal_and_requests_no_bodies(self):
        service = MagicMock()
        request = service.files.return_value.list.return_value
        request.execute.return_value = {"files": [], "nextPageToken": "next"}
        client = GoogleDriveMetadataClient(service)

        files, token = client.list_children(
            ROOT_ID,
            modified_after_rfc3339="2026-07-01T00:00:00Z",
            page_token=None,
            page_size=250,
        )

        self.assertEqual(files, [])
        self.assertEqual(token, "next")
        kwargs = service.files.return_value.list.call_args.kwargs
        self.assertIn(f"mimeType = '{FOLDER_MIME_TYPE}'", kwargs["q"])
        self.assertIn("modifiedTime >= '2026-07-01T00:00:00Z'", kwargs["q"])
        self.assertTrue(kwargs["includeItemsFromAllDrives"])
        self.assertNotIn("description", kwargs["fields"])
        self.assertNotIn("owners", kwargs["fields"])


class BuildDriveServiceTests(SimpleTestCase):
    @patch("googleapiclient.discovery.build")
    def test_active_read_only_connection_builds_v3_client_without_refresh(self, build):
        expected_service = MagicMock()
        build.return_value = expected_service
        connection = SimpleNamespace(
            scopes=["https://www.googleapis.com/auth/drive.readonly"],
            access_token="synthetic-access-token",
            refresh_token="synthetic-refresh-token",
            token_expires_at=datetime(2099, 1, 1, tzinfo=timezone.utc),
            last_error="",
            save=MagicMock(),
        )

        service = build_drive_service(connection)

        self.assertIs(service, expected_service)
        build.assert_called_once()
        self.assertEqual(build.call_args.args[:2], ("drive", "v3"))
        credentials = build.call_args.kwargs["credentials"]
        self.assertEqual(credentials.expiry, datetime(2099, 1, 1))
        self.assertIsNone(credentials.expiry.tzinfo)
        self.assertTrue(credentials.valid)
        self.assertFalse(connection.save.called)

    def test_connection_without_read_only_scope_is_rejected(self):
        connection = SimpleNamespace(scopes=["openid"], access_token="token")

        with self.assertRaisesMessage(DriveInventoryError, "lacks a read-only Drive scope"):
            build_drive_service(connection)


class DriveInventoryCommandTests(SimpleTestCase):
    def fake_connection(self):
        return SimpleNamespace(
            pk=123,
            organization_id=456,
            provider=ExternalServiceProvider.GOOGLE_DRIVE,
            status=ExternalServiceConnectionStatus.CONNECTED,
        )

    @patch("core.management.commands.inventory_drive_transcripts.inventory_drive_metadata")
    @patch("core.management.commands.inventory_drive_transcripts.build_drive_service")
    @patch("core.management.commands.inventory_drive_transcripts.assert_provider_inventory_allowed")
    @patch("core.management.commands.inventory_drive_transcripts.ExternalServiceConnection.objects")
    def test_command_writes_new_json_output_after_governance_check(
        self,
        connection_objects,
        inventory_allowed,
        build_service,
        inventory,
    ):
        connection_objects.select_related.return_value.filter.return_value.first.return_value = (
            self.fake_connection()
        )
        build_service.return_value = MagicMock()
        inventory.return_value = {
            "inventory_id": "inventory-test",
            "counts": {"candidate_transcripts": 2, "duplicates": 1},
            "partial": False,
        }

        with tempfile.TemporaryDirectory() as directory:
            output_path = Path(directory) / "inventory.json"
            out = StringIO()
            call_command(
                "inventory_drive_transcripts",
                connection_id="123",
                folder_id=[ROOT_ID],
                modified_after="2026-07-01",
                output=str(output_path),
                max_files=100,
                max_pages=10,
                max_seconds=30,
                dry_run=True,
                stdout=out,
            )

            self.assertEqual(json.loads(output_path.read_text()), inventory.return_value)
            self.assertEqual(stat.S_IMODE(output_path.stat().st_mode), 0o600)
            self.assertIn("inventory-test", out.getvalue())

        inventory_allowed.assert_called_once_with(
            ExternalServiceProvider.GOOGLE_DRIVE,
            {f"organization:456", "connection:123", f"folder:{ROOT_ID}"},
            requested_max_files=100,
        )

    def test_command_requires_dry_run_and_absolute_new_output(self):
        with self.assertRaisesMessage(CommandError, "--dry-run is required"):
            call_command(
                "inventory_drive_transcripts",
                connection_id="123",
                folder_id=[ROOT_ID],
                modified_after="2026-07-01",
                output="relative.json",
            )

        with self.assertRaisesMessage(CommandError, "--output must be an absolute path"):
            call_command(
                "inventory_drive_transcripts",
                connection_id="123",
                folder_id=[ROOT_ID],
                modified_after="2026-07-01",
                output="relative.json",
                dry_run=True,
            )
