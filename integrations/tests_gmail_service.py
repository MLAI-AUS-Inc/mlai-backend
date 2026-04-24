import ssl
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone
from googleapiclient.errors import HttpError

from core.models import Organization
from integrations.models import GmailMessageArtifact, GoogleConnection
from integrations.services.gmail import METADATA_HEADERS, _execute_gmail_request, get_message_metadata
from integrations.services.gmail import sync_message_metadata_page

User = get_user_model()


class GmailRequestRetryTests(SimpleTestCase):
    @patch("integrations.services.gmail.time.sleep", return_value=None)
    def test_retries_ssl_eof_and_succeeds(self, _sleep):
        first_request = Mock()
        first_request.execute.side_effect = ssl.SSLEOFError("unexpected eof")
        second_request = Mock()
        second_request.execute.return_value = {"ok": True}
        request_factory = Mock(side_effect=[first_request, second_request])

        result = _execute_gmail_request(request_factory, description="messages.get.metadata:test")

        self.assertEqual(result, {"ok": True})
        self.assertEqual(request_factory.call_count, 2)
        _sleep.assert_called_once()

    @patch("integrations.services.gmail.time.sleep", return_value=None)
    def test_does_not_retry_non_retryable_http_error(self, _sleep):
        request = Mock()
        request.execute.side_effect = HttpError(
            resp=SimpleNamespace(status=400, reason="Bad Request"),
            content=b"bad request",
        )
        request_factory = Mock(return_value=request)

        with self.assertRaises(HttpError):
            _execute_gmail_request(request_factory, description="messages.get.metadata:test")

        self.assertEqual(request_factory.call_count, 1)
        _sleep.assert_not_called()


class GmailMetadataHeaderTests(SimpleTestCase):
    @patch("integrations.services.gmail.build_gmail_service")
    def test_get_message_metadata_requests_bulk_mail_headers(self, mock_build_service):
        request = Mock()
        request.execute.return_value = {"id": "msg-1"}
        messages_resource = mock_build_service.return_value.users.return_value.messages.return_value
        messages_resource.get.return_value = request

        result = get_message_metadata(SimpleNamespace(), "msg-1")

        self.assertEqual(result, {"id": "msg-1"})
        messages_resource.get.assert_called_once_with(
            userId="me",
            id="msg-1",
            format="metadata",
            metadataHeaders=METADATA_HEADERS,
        )
        self.assertIn("List-Unsubscribe", METADATA_HEADERS)
        self.assertIn("List-Id", METADATA_HEADERS)
        self.assertIn("Precedence", METADATA_HEADERS)
        self.assertIn("Auto-Submitted", METADATA_HEADERS)


class GmailMetadataReuseTests(TestCase):
    @patch("integrations.services.gmail.build_gmail_service")
    @patch("integrations.services.gmail.get_message_metadata")
    @patch("integrations.services.gmail.list_message_page")
    def test_sync_message_metadata_page_reuses_existing_hydrated_artifacts(
        self,
        mock_list_message_page,
        mock_get_message_metadata,
        _mock_build_gmail_service,
    ):
        organization = Organization.objects.create(name="Acme", domain="acme.com")
        user = User.objects.create_user(email="founder@example.com", password="password123")
        connection = GoogleConnection.objects.create(
            user=user,
            google_email="founder@gmail.com",
            refresh_token="refresh-token",
            scope="https://www.googleapis.com/auth/gmail.readonly",
        )
        internal_date = timezone.now()
        artifact = GmailMessageArtifact.objects.create(
            organization=organization,
            google_connection=connection,
            gmail_message_id="msg-1",
            gmail_thread_id="thread-1",
            history_id="42",
            internal_date=internal_date,
            metadata_hydrated_at=internal_date,
            subject="Existing update",
            from_address="founder@acme.com",
        )
        mock_list_message_page.return_value = {
            "messages": [{"id": "msg-1"}],
            "resultSizeEstimate": 1,
            "nextPageToken": None,
        }

        result = sync_message_metadata_page(
            organization=organization,
            connection=connection,
            after_dt=internal_date,
            before_dt=internal_date,
        )

        self.assertEqual(result["reused_existing_count"], 1)
        self.assertEqual([item.gmail_message_id for item in result["artifacts"]], ["msg-1"])
        mock_get_message_metadata.assert_not_called()

    @override_settings(GMAIL_METADATA_PAGE_MAX_RESULTS=5)
    @patch("integrations.services.gmail.build_gmail_service")
    @patch("integrations.services.gmail.get_message_metadata")
    @patch("integrations.services.gmail.list_message_page")
    def test_sync_message_metadata_page_caps_page_size_and_reuses_service(
        self,
        mock_list_message_page,
        mock_get_message_metadata,
        mock_build_gmail_service,
    ):
        organization = Organization.objects.create(name="Acme", domain="acme.com")
        user = User.objects.create_user(email="founder2@example.com", password="password123")
        connection = GoogleConnection.objects.create(
            user=user,
            google_email="founder2@gmail.com",
            refresh_token="refresh-token",
            scope="https://www.googleapis.com/auth/gmail.readonly",
        )
        internal_date = timezone.now()
        internal_date_ms = str(int(internal_date.timestamp() * 1000))
        service = Mock(name="gmail_service")
        mock_build_gmail_service.return_value = service
        mock_list_message_page.return_value = {
            "messages": [{"id": "msg-1"}, {"id": "msg-2"}],
            "resultSizeEstimate": 25,
            "nextPageToken": "next-page",
        }
        mock_get_message_metadata.side_effect = [
            {
                "id": "msg-1",
                "threadId": "thread-1",
                "historyId": "41",
                "internalDate": internal_date_ms,
                "payload": {"headers": [{"name": "Subject", "value": "Update 1"}]},
            },
            {
                "id": "msg-2",
                "threadId": "thread-2",
                "historyId": "42",
                "internalDate": internal_date_ms,
                "payload": {"headers": [{"name": "Subject", "value": "Update 2"}]},
            },
        ]

        result = sync_message_metadata_page(
            organization=organization,
            connection=connection,
            after_dt=internal_date,
            before_dt=internal_date,
            max_results=250,
        )

        self.assertEqual(result["effective_max_results"], 5)
        self.assertEqual(result["requested_max_results"], 250)
        self.assertEqual([item.gmail_message_id for item in result["artifacts"]], ["msg-1", "msg-2"])
        mock_build_gmail_service.assert_called_once_with(connection, cache_discovery=False)
        self.assertEqual(mock_list_message_page.call_args.kwargs["max_results"], 5)
        self.assertIs(mock_list_message_page.call_args.kwargs["service"], service)
        self.assertEqual(mock_get_message_metadata.call_count, 2)
        for call in mock_get_message_metadata.call_args_list:
            self.assertIs(call.kwargs["service"], service)
