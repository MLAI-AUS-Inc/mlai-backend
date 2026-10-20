from __future__ import annotations

from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

from integrations import http_client
from integrations.services.notion_agenda_client import NotionAgendaClient, NotionAgendaError

REQUEST = "integrations.services.notion_agenda_client.http_client.request"
DATA_SOURCE = {"properties": {"Name": {"type": "title"}, "Tags": {"type": "multi_select"}}}


class FakeNotionResponse:
    def __init__(self, payload=None, status_code=200, headers=None):
        self._payload = payload if payload is not None else {}
        self.status_code = status_code
        self.headers = headers or {}

    def json(self):
        return self._payload


@override_settings(
    NOTION_AGENDA_API_TOKEN="ntn-test-token",
    NOTION_AGENDA_DATA_SOURCE_ID="ds-1",
    NOTION_AGENDA_TEMPLATE_ID="tpl-1",
    NOTION_API_VERSION="2026-03-11",
)
class NotionAgendaClientTests(SimpleTestCase):
    @override_settings(NOTION_AGENDA_TEMPLATE_ID="")
    def test_missing_configuration_fails_before_any_request(self):
        with patch(REQUEST) as request:
            with self.assertRaises(NotionAgendaError) as raised:
                NotionAgendaClient()
        self.assertEqual(raised.exception.code, "notion_not_configured")
        request.assert_not_called()

    def test_find_pages_filters_on_the_title_property(self):
        with patch(REQUEST, side_effect=[
            FakeNotionResponse(DATA_SOURCE),
            FakeNotionResponse({"results": [{"id": "p1"}, {"id": "p2", "in_trash": True}]}),
        ]) as request:
            pages = NotionAgendaClient().find_pages_by_title("2026-10-27 Meeting Notes")

        self.assertEqual([page["id"] for page in pages], ["p1"])
        method, url = request.call_args.args
        self.assertEqual((method, url), ("POST", "https://api.notion.com/v1/data_sources/ds-1/query"))
        self.assertEqual(
            request.call_args.kwargs["json"]["filter"],
            {"property": "Name", "title": {"equals": "2026-10-27 Meeting Notes"}},
        )
        self.assertEqual(request.call_args.kwargs["headers"]["Authorization"], "Bearer ntn-test-token")
        self.assertEqual(request.call_args.kwargs["headers"]["Notion-Version"], "2026-03-11")

    def test_create_page_uses_data_source_parent_and_template(self):
        with patch(REQUEST, side_effect=[
            FakeNotionResponse(DATA_SOURCE),
            FakeNotionResponse({"id": "new-page", "url": "https://notion.so/new-page"}),
        ]) as request:
            page = NotionAgendaClient().create_page_from_template("2026-11-03 Meeting Notes")

        self.assertEqual(page["id"], "new-page")
        body = request.call_args.kwargs["json"]
        self.assertEqual(body["parent"], {"type": "data_source_id", "data_source_id": "ds-1"})
        self.assertEqual(body["template"], {"type": "template_id", "template_id": "tpl-1"})
        self.assertEqual(body["properties"]["Name"]["title"][0]["text"]["content"], "2026-11-03 Meeting Notes")
        self.assertNotIn("children", body)

    def test_insert_after_sends_the_position_and_returns_new_blocks(self):
        bullet = {"type": "bulleted_list_item", "bulleted_list_item": {"rich_text": []}}
        with patch(REQUEST, return_value=FakeNotionResponse({"results": [{"id": "inserted"}]})) as request:
            results = NotionAgendaClient().insert_after("page-1", "last-bullet", [bullet])

        self.assertEqual(results, [{"id": "inserted"}])
        method, url = request.call_args.args
        self.assertEqual((method, url), ("PATCH", "https://api.notion.com/v1/blocks/page-1/children"))
        self.assertEqual(
            request.call_args.kwargs["json"]["position"],
            {"type": "after_block", "after_block": {"id": "last-bullet"}},
        )

    def test_insert_without_confirmed_blocks_is_uncertain(self):
        with patch(REQUEST, return_value=FakeNotionResponse({"results": []})):
            with self.assertRaises(NotionAgendaError) as raised:
                NotionAgendaClient().insert_after("page-1", "last-bullet", [{}])
        self.assertEqual(raised.exception.code, "notion_invalid_response")
        self.assertTrue(raised.exception.uncertain)

    def test_list_children_follows_pagination(self):
        with patch(REQUEST, side_effect=[
            FakeNotionResponse({"results": [{"id": "a"}], "has_more": True, "next_cursor": "c1"}),
            FakeNotionResponse({"results": [{"id": "b"}], "has_more": False}),
        ]) as request:
            blocks = NotionAgendaClient().list_children("page-1")

        self.assertEqual([block["id"] for block in blocks], ["a", "b"])
        self.assertEqual(request.call_args.kwargs["params"]["start_cursor"], "c1")

    def test_list_children_rejects_a_repeated_cursor(self):
        repeat = {"results": [{"id": "a"}], "has_more": True, "next_cursor": "c1"}
        with patch(REQUEST, side_effect=[FakeNotionResponse(repeat), FakeNotionResponse(repeat)]):
            with self.assertRaises(NotionAgendaError) as raised:
                NotionAgendaClient().list_children("page-1")
        self.assertEqual(raised.exception.code, "notion_invalid_response")

    def test_forbidden_write_raises_instead_of_returning_none(self):
        with patch(REQUEST, return_value=FakeNotionResponse(status_code=403)):
            with self.assertRaises(NotionAgendaError) as raised:
                NotionAgendaClient().insert_after("page-1", "last-bullet", [{}])
        self.assertEqual(raised.exception.code, "notion_access_denied")
        self.assertFalse(raised.exception.uncertain)

    def test_server_error_is_uncertain_only_for_writes(self):
        with patch(REQUEST, return_value=FakeNotionResponse(status_code=502)):
            client = NotionAgendaClient()
            with self.assertRaises(NotionAgendaError) as write_error:
                client.insert_after("page-1", "last-bullet", [{}])
            with self.assertRaises(NotionAgendaError) as read_error:
                client.list_children("page-1")
        self.assertEqual(write_error.exception.code, "notion_unavailable")
        self.assertTrue(write_error.exception.uncertain)
        self.assertFalse(read_error.exception.uncertain)

    def test_network_failure_on_write_is_uncertain(self):
        with patch(REQUEST, side_effect=http_client.RequestException("timed out")):
            with self.assertRaises(NotionAgendaError) as raised:
                NotionAgendaClient().insert_after("page-1", "last-bullet", [{}])
        self.assertEqual(raised.exception.code, "notion_unavailable")
        self.assertTrue(raised.exception.uncertain)
        self.assertNotIn("timed out", str(raised.exception))

    def test_rate_limit_reports_retry_after(self):
        with patch(REQUEST, return_value=FakeNotionResponse(status_code=429, headers={"Retry-After": "7"})):
            with self.assertRaises(NotionAgendaError) as raised:
                NotionAgendaClient().list_children("page-1")
        self.assertEqual(raised.exception.code, "notion_rate_limited")
        self.assertEqual(raised.exception.retry_after, 7)
        self.assertFalse(raised.exception.uncertain)
