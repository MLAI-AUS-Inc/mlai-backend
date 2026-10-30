from __future__ import annotations

from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, override_settings

from integrations.management.commands.inspect_notion_agenda import ReadOnlyNotionClient

REQUEST = "integrations.services.notion_agenda_client.http_client.request"


class FakeNotionResponse:
    def __init__(self, payload=None, status_code=200):
        self._payload = payload if payload is not None else {}
        self.status_code = status_code
        self.headers = {}

    def json(self):
        return self._payload


def children(*blocks):
    return FakeNotionResponse({"results": list(blocks), "has_more": False})


@override_settings(
    NOTION_AGENDA_API_TOKEN="ntn-test-token",
    NOTION_AGENDA_DATA_SOURCE_ID="",
    NOTION_AGENDA_TEMPLATE_ID="",
)
class InspectNotionAgendaTests(SimpleTestCase):
    def test_runs_without_data_source_or_template_ids(self):
        with patch(REQUEST, side_effect=[
            FakeNotionResponse({"data_sources": [{"id": "ds-1", "name": "Meeting Notes"}]}),
            FakeNotionResponse({"templates": [{"id": "tpl-1", "name": "Weekly", "is_default": True}]}),
        ]) as request:
            out = StringIO()
            call_command("inspect_notion_agenda", "db", "db-1", stdout=out)

        self.assertIn("id=ds-1", out.getvalue())
        self.assertIn("id=tpl-1  DEFAULT", out.getvalue())
        self.assertEqual([c.args[0] for c in request.call_args_list], ["GET", "GET"])

    def test_refuses_writes_before_any_request(self):
        with patch(REQUEST) as request:
            with self.assertRaises(CommandError):
                ReadOnlyNotionClient().request("PATCH", "/blocks/b-1/children", body={})
        request.assert_not_called()

    def test_page_tree_flags_toggle_headings_and_nests_children(self):
        heading = {
            "id": "h-1", "type": "heading_1", "has_children": True,
            "heading_1": {"is_toggleable": True, "rich_text": [{"plain_text": "Agenda"}]},
        }
        bullet = {
            "id": "b-1", "type": "bulleted_list_item", "has_children": False,
            "bulleted_list_item": {"rich_text": []},
        }
        with patch(REQUEST, side_effect=[
            FakeNotionResponse({"url": "https://notion.so/page-1"}),
            children(heading),
            children(bullet),
        ]):
            out = StringIO()
            call_command("inspect_notion_agenda", "page", "page-1", stdout=out)

        lines = out.getvalue().splitlines()
        self.assertIn("heading_1 [TOGGLE, has_children] 'Agenda'  id=h-1", lines)
        self.assertIn("  bulleted_list_item (empty)  id=b-1", lines)

    @override_settings(NOTION_AGENDA_API_TOKEN="")
    def test_missing_token_fails_before_any_request(self):
        with patch(REQUEST) as request:
            with self.assertRaises(CommandError):
                call_command("inspect_notion_agenda", "db", "db-1")
        request.assert_not_called()

    def test_shows_notions_own_error_message(self):
        with patch(REQUEST, return_value=FakeNotionResponse(
            {"code": "validation_error", "message": "Provided ID is a page, not a database."}, status_code=400,
        )):
            with self.assertRaises(CommandError) as raised:
                call_command("inspect_notion_agenda", "db", "db-1")
        self.assertIn("Notion 400 validation_error: Provided ID is a page, not a database.", str(raised.exception))
