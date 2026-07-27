import base64
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from integrations.models import GoogleConnection
from integrations.services.gmail import StaleHistoryCursorError
from organizations.models import Organization
from org_memory.connectors.gmail import GmailMemoryConnector
from org_memory.connectors.registry import MetadataOnlyMemoryConnector, connector_registry
from org_memory.models import (
    GmailMailboxWatch,
    GmailScopedArtifactState,
    GmailScopedMessageArtifact,
    GmailWatchStatus,
    MemoryConnectionConfiguration,
    MemoryConnectionState,
    MemoryScopeStatus,
    MemorySource,
    MemorySourceLifecycle,
    MemorySourceScope,
)
from org_memory.runtime import _apply_removal, _capture_record
from startup_updates.models import (
    ArtifactProcessingStatus,
    GmailAttachmentArtifact,
    GmailMessageArtifact,
)


def _encoded(value):
    return base64.urlsafe_b64encode(value.encode("utf-8")).decode("ascii").rstrip("=")


@override_settings(
    ORG_MEMORY_GMAIL_PAGE_SIZE=10,
    ORG_MEMORY_GMAIL_BACKFILL_DAYS=365,
    ORG_MEMORY_GMAIL_CHUNK_TARGET_CHARS=200,
    ORG_MEMORY_GMAIL_MAX_MESSAGE_CHARS=5000,
    ORG_MEMORY_GMAIL_MAX_ATTACHMENT_CHARS=5000,
    ORG_MEMORY_GMAIL_FULL_RECONCILE_SECONDS=604800,
    ORG_MEMORY_GMAIL_PUBSUB_TOPIC="",
    ORG_MEMORY_GMAIL_PUBSUB_AUDIENCE="",
    ORG_MEMORY_GMAIL_PUBSUB_SERVICE_ACCOUNT_EMAIL="",
)
class GmailMemoryConnectorTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(
            name="Gmail Adapter Org",
            domain="gmail-adapter.mlai.test",
        )
        self.user = get_user_model().objects.create_user(email="gmail@mlai.test")
        self.connection = GoogleConnection.objects.create(
            user=self.user,
            google_email="shared-committee@mlai.test",
            refresh_token="gmail-refresh-secret",
            scope="https://www.googleapis.com/auth/gmail.readonly",
        )
        self.configuration = MemoryConnectionConfiguration.objects.create(
            organization=self.organization,
            provider="gmail",
            google_connection=self.connection,
            lifecycle_state=MemoryConnectionState.ACTIVE,
            created_by=self.user,
        )
        self.scope = MemorySourceScope.objects.create(
            configuration=self.configuration,
            scope_type="label",
            external_id="Label_Sponsors",
            name="Sponsors",
            selected=True,
            status=MemoryScopeStatus.SELECTED,
            default_classification="internal",
            metadata={"label_type": "user", "mailbox": self.connection.google_email},
        )
        self.body = "We confirm the sponsorship package and the August launch date."
        self.current_labels = ["Label_Sponsors", "IMPORTANT"]

    def _message(self, *, message_id="msg-1", thread_id="thread-1", body=None, labels=None):
        now = timezone.now() - timedelta(minutes=10)
        return {
            "id": message_id,
            "threadId": thread_id,
            "historyId": "100",
            "internalDate": str(int(now.timestamp() * 1000)),
            "labelIds": list(self.current_labels if labels is None else labels),
            "snippet": "Sponsorship confirmation",
            "payload": {
                "mimeType": "text/plain",
                "headers": [
                    {"name": "Subject", "value": "MLAI sponsorship"},
                    {"name": "From", "value": "partner@example.com"},
                    {"name": "To", "value": "shared-committee@mlai.test"},
                    {"name": "Cc", "value": "chair@mlai.test"},
                    {"name": "Bcc", "value": "private-bcc@example.com"},
                ],
                "body": {"data": _encoded(body if body is not None else self.body)},
            },
        }

    def _first_backfill_page(self, connector):
        return connector.backfill(self.configuration, [self.scope], {})

    def _finish_backfill(self, connector, first_page):
        records = list(first_page.records)
        removals = list(first_page.removals)
        checkpoint = first_page.checkpoint
        page = first_page
        while page.has_more:
            page = connector.backfill(self.configuration, [self.scope], checkpoint)
            records.extend(page.records)
            removals.extend(page.removals)
            checkpoint = page.checkpoint
        return page, records, removals

    @patch("org_memory.connectors.gmail.list_gmail_labels")
    def test_discovery_exposes_user_labels_only_and_installs_real_adapter(self, labels):
        labels.return_value = [
            {"id": "INBOX", "name": "Inbox", "type": "system"},
            {"id": "Label_Sponsors", "name": "Sponsors", "type": "user"},
        ]
        connector = connector_registry.get("gmail")
        self.assertIsInstance(connector, GmailMemoryConnector)
        self.assertNotIsInstance(connector, MetadataOnlyMemoryConnector)
        self.assertEqual(connector_registry.validate_conformance("gmail"), [])

        discovery = connector.discover_scopes(self.configuration)

        self.assertEqual(
            [(row.scope_type, row.external_id) for row in discovery.scopes],
            [("label", "Label_Sponsors")],
        )
        self.assertTrue(discovery.scopes[0].metadata["metadata_only"])
        self.assertFalse(GmailScopedMessageArtifact.objects.exists())
        self.assertFalse(MemorySource.objects.exists())

        self.scope.external_id = "INBOX"
        self.scope.metadata = {"label_type": "system"}
        self.scope.save(update_fields=("external_id", "metadata", "updated_at"))
        with self.assertRaisesMessage(ValueError, "system labels"):
            connector.preview(self.configuration, [self.scope], None)

    @patch("org_memory.connectors.gmail.get_gmail_profile")
    @patch("org_memory.connectors.gmail.get_message_full")
    @patch("org_memory.connectors.gmail.list_label_message_page")
    def test_exact_label_backfill_versions_attachment_and_label_removal(
        self,
        list_page,
        get_full,
        get_profile,
    ):
        list_page.return_value = {
            "messages": [{"id": "msg-1", "threadId": "thread-1"}],
            "nextPageToken": None,
        }
        get_full.side_effect = lambda _connection, _message_id: self._message()
        get_profile.return_value = {
            "emailAddress": self.connection.google_email,
            "historyId": "100",
        }
        unlabelled = GmailMessageArtifact.objects.create(
            organization=self.organization,
            google_connection=self.connection,
            gmail_message_id="msg-unlabelled",
            gmail_thread_id="thread-1",
            internal_date=timezone.now() - timedelta(minutes=5),
            subject="Private side conversation",
            from_address="private@example.com",
            label_ids=["INBOX"],
            cleaned_text="This unlabelled message must never enter memory.",
        )
        connector = connector_registry.get("gmail")

        first_page = self._first_backfill_page(connector)
        self.assertTrue(first_page.has_more)
        selected_message = GmailMessageArtifact.objects.get(gmail_message_id="msg-1")
        GmailAttachmentArtifact.objects.create(
            organization=self.organization,
            message_artifact=selected_message,
            gmail_attachment_id="attachment-1",
            part_id="2",
            filename="sponsorship.txt",
            mime_type="text/plain",
            raw_content_base64="VEhJUyBJUyBSQVcgU0VDUkVU",
            extracted_text="The signed sponsorship is worth AUD 20,000.",
            extraction_status=ArtifactProcessingStatus.PROCESSED,
            sha256="a" * 64,
            extracted_at=timezone.now(),
        )
        page, records, removals = self._finish_backfill(connector, first_page)

        self.assertEqual(removals, [])
        self.assertEqual(
            sorted(row["source_type"] for row in records),
            ["gmail_attachment", "gmail_thread"],
        )
        thread_record = next(row for row in records if row["source_type"] == "gmail_thread")
        attachment_record = next(
            row for row in records if row["source_type"] == "gmail_attachment"
        )
        self.assertEqual(thread_record["classification"], "executive")
        self.assertIn("sponsorship package", thread_record["bounded_excerpt"])
        self.assertNotIn(unlabelled.cleaned_text, thread_record["bounded_excerpt"])
        self.assertNotIn("private-bcc@example.com", repr(thread_record))
        self.assertNotIn("VEhJUyBJUyBSQVcgU0VDUkVU", repr(attachment_record))
        self.assertEqual(
            thread_record["chunks"][0]["source_locator"]["message_id"],
            "msg-1",
        )
        self.assertEqual(
            thread_record["chunks"][0]["source_locator"]["label_ids"],
            ["Label_Sponsors"],
        )
        self.assertEqual(
            attachment_record["chunks"][0]["source_locator"]["part_id"],
            "2",
        )
        mapping = GmailScopedMessageArtifact.objects.get(gmail_message_id="msg-1")
        self.assertEqual(mapping.selected_label_ids, ["Label_Sponsors"])
        self.assertEqual(mapping.lifecycle_state, GmailScopedArtifactState.ACTIVE)

        for record in records:
            _capture_record(self.configuration, record)
        thread_source = MemorySource.objects.get(external_id="gmail_thread:thread-1")
        self.assertEqual(thread_source.versions.count(), 1)
        self.assertTrue(thread_source.current_version.acl_snapshot.is_accessible)

        self.body = "We confirm the sponsorship package, funding and August launch date."
        with patch("org_memory.connectors.gmail.list_history_page") as history:
            history.return_value = {
                "history": [
                    {
                        "id": "101",
                        "messagesAdded": [
                            {
                                "message": {
                                    "id": "msg-1",
                                    "threadId": "thread-1",
                                    "labelIds": ["Label_Sponsors", "IMPORTANT"],
                                }
                            }
                        ],
                    }
                ],
                "historyId": "101",
                "nextPageToken": None,
            }
            changed = connector.incremental_sync(self.configuration, page.next_cursor)
        for record in changed.records:
            _capture_record(self.configuration, record)
        thread_source.refresh_from_db()
        self.assertEqual(thread_source.versions.count(), 2)
        self.assertIn("funding", thread_source.current_version.bounded_excerpt)

        self.current_labels = ["IMPORTANT"]
        with patch("org_memory.connectors.gmail.list_history_page") as history:
            history.return_value = {
                "history": [
                    {
                        "id": "102",
                        "labelsRemoved": [
                            {
                                "message": {
                                    "id": "msg-1",
                                    "threadId": "thread-1",
                                    "labelIds": ["IMPORTANT"],
                                },
                                "labelIds": ["Label_Sponsors"],
                            }
                        ],
                    }
                ],
                "historyId": "102",
                "nextPageToken": None,
            }
            removed = connector.incremental_sync(self.configuration, changed.next_cursor)
        self.assertEqual(removed.records, ())
        self.assertEqual(len(removed.removals), 2)
        for removal in removed.removals:
            _apply_removal(self.configuration, removal)
        mapping.refresh_from_db()
        thread_source.refresh_from_db()
        self.assertEqual(mapping.lifecycle_state, GmailScopedArtifactState.LABEL_REMOVED)
        self.assertEqual(thread_source.lifecycle_state, MemorySourceLifecycle.ACCESS_REVOKED)
        self.assertFalse(thread_source.current_version.chunks.filter(active_for_retrieval=True).exists())

    @patch("org_memory.connectors.gmail.get_gmail_profile")
    @patch("org_memory.connectors.gmail.get_message_metadata")
    @patch("org_memory.connectors.gmail.get_message_full")
    @patch("org_memory.connectors.gmail.list_label_message_page")
    def test_unselected_history_change_is_not_body_hydrated_or_persisted(
        self,
        list_page,
        get_full,
        get_metadata,
        get_profile,
    ):
        list_page.return_value = {
            "messages": [{"id": "msg-1", "threadId": "thread-1"}],
            "nextPageToken": None,
        }
        get_full.return_value = self._message()
        get_profile.return_value = {"historyId": "100"}
        connector = connector_registry.get("gmail")
        first = self._first_backfill_page(connector)
        page, _records, _removals = self._finish_backfill(connector, first)
        get_full.reset_mock()
        get_metadata.return_value = self._message(
            message_id="msg-unselected",
            thread_id="thread-unselected",
            body="Private unselected message body.",
            labels=["INBOX"],
        )

        with patch("org_memory.connectors.gmail.list_history_page") as history:
            history.return_value = {
                "history": [
                    {
                        "id": "101",
                        "messagesAdded": [
                            {
                                "message": {
                                    "id": "msg-unselected",
                                    "threadId": "thread-unselected",
                                }
                            }
                        ],
                    }
                ],
                "historyId": "101",
                "nextPageToken": None,
            }
            result = connector.incremental_sync(self.configuration, page.next_cursor)

        self.assertEqual(result.records, ())
        get_metadata.assert_called_once_with(self.connection, "msg-unselected")
        get_full.assert_not_called()
        self.assertFalse(
            GmailMessageArtifact.objects.filter(gmail_message_id="msg-unselected").exists()
        )

    @patch("org_memory.connectors.gmail.get_gmail_profile")
    @patch("org_memory.connectors.gmail.get_message_full")
    @patch("org_memory.connectors.gmail.list_label_message_page")
    def test_lost_mailbox_credential_revokes_active_sources(
        self,
        list_page,
        get_full,
        get_profile,
    ):
        list_page.return_value = {
            "messages": [{"id": "msg-1", "threadId": "thread-1"}],
            "nextPageToken": None,
        }
        get_full.return_value = self._message()
        get_profile.return_value = {"historyId": "100"}
        connector = connector_registry.get("gmail")
        first = self._first_backfill_page(connector)
        page, records, _removals = self._finish_backfill(connector, first)
        for record in records:
            _capture_record(self.configuration, record)

        self.connection.refresh_token = ""
        self.connection.save(update_fields=("refresh_token", "updated_at"))
        access_lost = connector.incremental_sync(
            self.configuration,
            page.next_cursor,
        )

        self.assertEqual(access_lost.records, ())
        self.assertEqual(access_lost.checkpoint["mode"], "access_lost")
        self.assertEqual(len(access_lost.removals), 1)
        mapping = GmailScopedMessageArtifact.objects.get(gmail_message_id="msg-1")
        self.assertEqual(mapping.lifecycle_state, GmailScopedArtifactState.ACCESS_LOST)
        for removal in access_lost.removals:
            _apply_removal(self.configuration, removal)
        source = MemorySource.objects.get(external_id="gmail_thread:thread-1")
        self.assertEqual(source.lifecycle_state, MemorySourceLifecycle.ACCESS_REVOKED)

    @patch("org_memory.connectors.gmail.get_gmail_profile")
    @patch("org_memory.connectors.gmail.get_message_full")
    @patch("org_memory.connectors.gmail.list_label_message_page")
    def test_stale_history_cursor_starts_full_recovery(
        self,
        list_page,
        get_full,
        get_profile,
    ):
        list_page.return_value = {
            "messages": [{"id": "msg-1", "threadId": "thread-1"}],
            "nextPageToken": None,
        }
        get_full.return_value = self._message()
        get_profile.return_value = {"historyId": "100"}
        connector = connector_registry.get("gmail")
        first = self._first_backfill_page(connector)
        page, _records, _removals = self._finish_backfill(connector, first)

        with patch(
            "org_memory.connectors.gmail.list_history_page",
            side_effect=StaleHistoryCursorError("expired"),
        ):
            recovery = connector.incremental_sync(self.configuration, page.next_cursor)

        self.assertTrue(recovery.has_more)
        self.assertEqual(recovery.checkpoint["mode"], "full_scan")
        self.assertTrue(recovery.checkpoint["cursor_recovered"])
        self.assertIn(recovery.checkpoint["phase"], {"scan", "reconcile"})

    @override_settings(
        ORG_MEMORY_GMAIL_PUBSUB_TOPIC="projects/mlai/topics/gmail-memory",
        ORG_MEMORY_GMAIL_PUBSUB_AUDIENCE="https://api.mlai.test/gmail-push",
        ORG_MEMORY_GMAIL_PUBSUB_SERVICE_ACCOUNT_EMAIL="gmail-push@mlai.iam.gserviceaccount.com",
    )
    @patch("org_memory.connectors.gmail.watch_gmail_mailbox")
    @patch("org_memory.connectors.gmail.get_gmail_profile")
    @patch("org_memory.connectors.gmail.get_message_full")
    @patch("org_memory.connectors.gmail.list_label_message_page")
    def test_successful_scan_registers_exact_label_watch(
        self,
        list_page,
        get_full,
        get_profile,
        watch_mailbox,
    ):
        list_page.return_value = {
            "messages": [{"id": "msg-1", "threadId": "thread-1"}],
            "nextPageToken": None,
        }
        get_full.return_value = self._message()
        get_profile.return_value = {"historyId": "100"}
        expiration = int((timezone.now() + timedelta(days=7)).timestamp() * 1000)
        watch_mailbox.return_value = {"historyId": "100", "expiration": str(expiration)}
        connector = connector_registry.get("gmail")

        first = self._first_backfill_page(connector)
        self._finish_backfill(connector, first)

        watch_mailbox.assert_called_once_with(
            self.connection,
            topic_name="projects/mlai/topics/gmail-memory",
            label_ids=["Label_Sponsors"],
        )
        watch = GmailMailboxWatch.objects.get(configuration=self.configuration)
        self.assertEqual(watch.status, GmailWatchStatus.ACTIVE)
        self.assertEqual(watch.label_ids, ["Label_Sponsors"])
        self.assertEqual(watch.history_id, "100")
        self.assertIsNotNone(watch.expiration_at)
