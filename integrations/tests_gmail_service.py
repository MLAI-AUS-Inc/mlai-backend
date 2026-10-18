import ssl
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone
from googleapiclient.errors import HttpError

from organizations.models import Organization
from integrations.models import GoogleConnection
from startup_updates.models import GmailMessageArtifact
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

    @override_settings(GMAIL_METADATA_PAGE_MAX_RESULTS=25)
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

        self.assertEqual(result["effective_max_results"], 25)
        self.assertEqual(result["requested_max_results"], 250)
        self.assertEqual([item.gmail_message_id for item in result["artifacts"]], ["msg-1", "msg-2"])
        mock_build_gmail_service.assert_called_once_with(connection, cache_discovery=False)
        self.assertEqual(mock_list_message_page.call_args.kwargs["max_results"], 25)
        self.assertIs(mock_list_message_page.call_args.kwargs["service"], service)
        self.assertEqual(mock_get_message_metadata.call_count, 2)
        for call in mock_get_message_metadata.call_args_list:
            self.assertIs(call.kwargs["service"], service)


    @override_settings(GMAIL_METADATA_PAGE_DEADLINE_SECONDS=40)
    def test_slow_metadata_is_durable_and_reused_on_first_page_resume(self):
        organization = Organization.objects.create(name="Deadline", domain="deadline.invalid")
        user = User.objects.create_user(email="deadline@example.invalid", password="test-only")
        connection = GoogleConnection.objects.create(user=user, google_email="deadline@example.invalid",
            refresh_token="synthetic", scope="https://www.googleapis.com/auth/gmail.readonly")
        now = [0.0]
        stamp = timezone.now()
        def metadata(connection, message_id, **kwargs):
            now[0] += 50
            return {"id": message_id, "threadId": message_id, "internalDate": str(int(stamp.timestamp() * 1000)),
                "payload": {"headers": []}}
        with patch("integrations.services.gmail.time.monotonic", side_effect=lambda: now[0]), \
                patch("integrations.services.gmail.build_gmail_service"), \
                patch("integrations.services.gmail.list_message_page", return_value={"messages": [{"id": "one"}, {"id": "two"}]}), \
                patch("integrations.services.gmail.get_message_metadata", side_effect=metadata) as fetch:
            kwargs = dict(organization=organization, connection=connection, after_dt=stamp, before_dt=stamp)
            first = sync_message_metadata_page(**kwargs)
            self.assertFalse(first["page_complete"])
            self.assertIsNone(first["next_page_token"])
            self.assertEqual(GmailMessageArtifact.objects.filter(organization=organization).count(), 1)
            second = sync_message_metadata_page(**kwargs)
        self.assertTrue(second["page_complete"])
        self.assertEqual(second["reused_existing_count"], 1)
        self.assertEqual(GmailMessageArtifact.objects.filter(organization=organization).count(), 2)
        self.assertEqual(fetch.call_count, 2)


class GmailMetadataDeadlineTests(SimpleTestCase):
    @override_settings(GMAIL_METADATA_PAGE_MAX_RESULTS=25, GMAIL_METADATA_PAGE_DEADLINE_SECONDS=40)
    def test_incomplete_pages_keep_token_and_resume_using_persisted_metadata(self):
        for token in (None, "same-page"):
            with self.subTest(token=token):
                now = [0.0]
                stored = {"reused-tail": SimpleNamespace(gmail_message_id="reused-tail")}
                fetched = []
                def metadata(connection, message_id, **kwargs):
                    fetched.append(message_id)
                    now[0] += 25
                    return {"id": message_id}
                def persist(**kwargs):
                    row = SimpleNamespace(gmail_message_id=kwargs["message_data"]["id"])
                    stored[row.gmail_message_id] = row
                    return row
                def existing(**kwargs):
                    return {key: stored[key] for key in kwargs["message_ids"] if key in stored}
                with patch("integrations.services.gmail.time.monotonic", side_effect=lambda: now[0]), \
                        patch("integrations.services.gmail.build_gmail_service"), \
                        patch("integrations.services.gmail.list_message_page", return_value={
                            "messages": [{"id": key} for key in ("one", "two", "three", "reused-tail")],
                            "nextPageToken": "next-page", "resultSizeEstimate": 4}) as listing, \
                        patch("integrations.services.gmail._existing_metadata_artifact_map", side_effect=existing), \
                        patch("integrations.services.gmail.get_message_metadata", side_effect=metadata), \
                        patch("integrations.services.gmail.upsert_message_artifact_from_message_data", side_effect=persist):
                    kwargs = dict(organization=SimpleNamespace(), connection=SimpleNamespace(),
                        after_dt=timezone.now(), before_dt=timezone.now(), max_results=50, page_token=token)
                    first = sync_message_metadata_page(**kwargs)
                    self.assertFalse(first["page_complete"])
                    self.assertEqual(first["next_page_token"], token)
                    self.assertEqual([a.gmail_message_id for a in first["artifacts"]], ["one", "two", "reused-tail"])
                    self.assertEqual(set(stored), {"one", "two", "reused-tail"})
                    self.assertEqual(first["reused_existing_count"], 1)
                    second = sync_message_metadata_page(**kwargs)
                self.assertTrue(second["page_complete"])
                self.assertEqual(second["next_page_token"], "next-page")
                self.assertEqual([a.gmail_message_id for a in second["artifacts"]], ["one", "two", "three", "reused-tail"])
                self.assertEqual(second["reused_existing_count"], 3)
                self.assertEqual(fetched, ["one", "two", "three"])
                self.assertEqual(listing.call_args_list[0].kwargs["query"], listing.call_args_list[1].kwargs["query"])
                self.assertEqual(listing.call_args.kwargs["max_results"], 25)

    def test_expired_deadline_still_fetches_one_new_message(self):
        with patch("integrations.services.gmail.build_gmail_service"), \
                patch("integrations.services.gmail.list_message_page", return_value={"messages": [{"id": "one"}, {"id": "two"}]}), \
                patch("integrations.services.gmail._existing_metadata_artifact_map", return_value={}), \
                patch("integrations.services.gmail.get_message_metadata", return_value={"id": "one"}) as fetch, \
                patch("integrations.services.gmail.upsert_message_artifact_from_message_data", return_value=SimpleNamespace(gmail_message_id="one")):
            result = sync_message_metadata_page(organization=SimpleNamespace(), connection=SimpleNamespace(),
                after_dt=timezone.now(), before_dt=timezone.now(), deadline_seconds=0)
        fetch.assert_called_once()
        self.assertFalse(result["page_complete"])
        self.assertIsNone(result["next_page_token"])

    @override_settings(GMAIL_METADATA_PAGE_MAX_RESULTS=25)
    def test_untimed_history_pages_keep_the_previous_safety_cap(self):
        from integrations.services.gmail import sync_history_metadata_page
        with patch("integrations.services.gmail.build_gmail_service"), \
                patch("integrations.services.gmail.list_history_page", return_value={}) as listing, \
                patch("integrations.services.gmail._existing_metadata_artifact_map", return_value={}):
            result = sync_history_metadata_page(organization=SimpleNamespace(), connection=SimpleNamespace(),
                start_history_id="1", max_results=50)
        self.assertEqual(result["effective_max_results"], 5)
        self.assertEqual(listing.call_args.kwargs["max_results"], 5)
