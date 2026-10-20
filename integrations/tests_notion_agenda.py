from __future__ import annotations

from datetime import date

from django.test import SimpleTestCase, override_settings

from integrations.services.notion_agenda import add_agenda_item, meeting_page_title
from integrations.services.notion_agenda_client import NotionAgendaError
from integrations.services.notion_agenda_page import AgendaPageError

MEETING_DATE = date(2026, 10, 27)
TITLE = "2026-10-27 Meeting Notes"
ITEM = "Monthly check-ins between presidents and committee members"
LINE = f"[Alex] - {ITEM}"


def _block(block_id, block_type, text=""):
    rich_text = [{"type": "text", "plain_text": text}] if text else []
    return {"id": block_id, "type": block_type, block_type: {"rich_text": rich_text}, "has_children": False}


def _template():
    return [
        _block("h-attendees", "heading_1", "Attendees"),
        _block("h-agenda", "heading_1", "Agenda"),
        _block("empty-1", "bulleted_list_item"),
        _block("empty-2", "bulleted_list_item"),
        _block("if-time", "paragraph", "If time:"),
        _block("empty-3", "bulleted_list_item"),
    ]


class _FakeClient:
    """In-memory Notion: pages by id, each a list of top-level blocks."""

    def __init__(self, pages=None):
        self.pages = {page_id: list(blocks) for page_id, blocks in (pages or {}).items()}
        self.titles = {page_id: TITLE for page_id in self.pages}
        self.calls = []
        self.blank_reads_after_create = 0
        self.insert_error = None
        self.drop_inserted_on_read = False
        self._inserted = 0

    def find_pages_by_title(self, title):
        self.calls.append(("find", title))
        return [
            {"id": page_id, "url": f"https://notion.test/{page_id}"}
            for page_id, page_title in self.titles.items()
            if page_title == title
        ]

    def create_page_from_template(self, title):
        self.calls.append(("create", title))
        self.pages["new-page"] = _template()
        self.titles["new-page"] = title
        return {"id": "new-page", "url": "https://notion.test/new-page"}

    def list_children(self, page_id):
        self.calls.append(("read", page_id))
        if page_id == "new-page" and self.blank_reads_after_create:
            self.blank_reads_after_create -= 1
            return []
        blocks = list(self.pages[page_id])
        if self.drop_inserted_on_read:
            blocks = [block for block in blocks if not block["id"].startswith("inserted")]
        return blocks

    def insert_after(self, page_id, after_block_id, children):
        self.calls.append(("insert", page_id, after_block_id))
        self._inserted += 1
        new = _block(f"inserted-{self._inserted}", "bulleted_list_item", children[0]["bulleted_list_item"]["rich_text"][0]["text"]["content"])
        blocks = self.pages[page_id]
        position = next(index for index, block in enumerate(blocks) if block["id"] == after_block_id)
        blocks.insert(position + 1, new)
        if self.insert_error:
            raise self.insert_error
        return [{"id": new["id"]}]

    def writes(self):
        return [call for call in self.calls if call[0] in {"create", "insert"}]


def _add(client, **kwargs):
    sleeps = []
    result = add_agenda_item(
        meeting_date=MEETING_DATE, owner="Alex", item=ITEM, client=client, sleep=sleeps.append, **kwargs
    )
    return result, sleeps


@override_settings(NOTION_AGENDA_TEMPLATE_POLL_ATTEMPTS=3, NOTION_AGENDA_TEMPLATE_POLL_SECONDS=1)
class AddAgendaItemTests(SimpleTestCase):
    def test_title_uses_iso_date(self):
        self.assertEqual(meeting_page_title(MEETING_DATE), TITLE)

    def test_existing_page_gets_one_bullet_and_nothing_else_changes(self):
        client = _FakeClient({"page-1": _template()})
        before = [block["id"] for block in client.pages["page-1"]]

        result, _ = _add(client)

        self.assertEqual(result.status, "added")
        self.assertEqual((result.page_id, result.block_id, result.created_page), ("page-1", "inserted-1", False))
        self.assertEqual(client.writes(), [("insert", "page-1", "h-agenda")])
        after = [block["id"] for block in client.pages["page-1"]]
        self.assertEqual(after, before[:2] + ["inserted-1"] + before[2:])

    def test_duplicate_is_reported_without_writing(self):
        blocks = _template()
        blocks.insert(2, _block("typed", "bulleted_list_item", f"[Jordan] - {ITEM}."))
        client = _FakeClient({"page-1": blocks})

        result, _ = _add(client)

        self.assertEqual((result.status, result.duplicate_location), ("duplicate", "agenda"))
        self.assertEqual(client.writes(), [])

    def test_several_pages_return_choices_without_writing(self):
        client = _FakeClient({"page-1": _template(), "page-2": _template()})

        result, _ = _add(client)

        self.assertEqual(result.status, "ambiguous")
        self.assertEqual([choice["page_id"] for choice in result.choices], ["page-1", "page-2"])
        self.assertEqual(client.writes(), [])

    def test_chosen_page_is_used_when_it_matches_the_date(self):
        client = _FakeClient({"page-1": _template(), "page-2": _template()})

        result, _ = _add(client, page_id="page-2")

        self.assertEqual(result.status, "added")
        self.assertEqual(client.writes(), [("insert", "page-2", "h-agenda")])

    def test_chosen_page_from_another_date_is_refused(self):
        client = _FakeClient({"page-1": _template()})
        client.pages["other-date"] = _template()
        client.titles["other-date"] = "2026-11-03 Meeting Notes"

        with self.assertRaises(AgendaPageError) as raised:
            _add(client, page_id="other-date")

        self.assertEqual(raised.exception.code, "meeting_page_mismatch")
        self.assertEqual(client.writes(), [])

    def test_missing_page_is_created_and_waits_for_the_template(self):
        client = _FakeClient()
        client.blank_reads_after_create = 1

        result, sleeps = _add(client)

        self.assertEqual(result.status, "added")
        self.assertTrue(result.created_page)
        self.assertEqual(client.writes(), [("create", TITLE), ("insert", "new-page", "h-agenda")])
        self.assertEqual(sleeps, [1.0])

    def test_template_that_never_loads_stops_before_inserting(self):
        client = _FakeClient()
        client.blank_reads_after_create = 99

        result, sleeps = _add(client)

        self.assertEqual(result.status, "template_pending")
        self.assertEqual(result.page_url, "https://notion.test/new-page")
        self.assertEqual(client.writes(), [("create", TITLE)])
        self.assertEqual(len(sleeps), 2)

    def test_existing_page_without_agenda_heading_is_not_written(self):
        client = _FakeClient({"page-1": [_block("h", "heading_1", "Attendees")]})

        with self.assertRaises(AgendaPageError) as raised:
            _add(client)

        self.assertEqual(raised.exception.code, "agenda_section_not_found")
        self.assertEqual(client.writes(), [])

    def test_invalid_item_fails_before_calling_notion(self):
        client = _FakeClient({"page-1": _template()})

        with self.assertRaises(AgendaPageError):
            add_agenda_item(meeting_date=MEETING_DATE, owner="Alex", item="  ", client=client)

        self.assertEqual(client.calls, [])

    def test_read_back_without_the_inserted_block_is_not_success(self):
        client = _FakeClient({"page-1": _template()})
        client.drop_inserted_on_read = True

        with self.assertRaises(NotionAgendaError) as raised:
            _add(client)

        self.assertEqual(raised.exception.code, "agenda_verify_failed")
        self.assertTrue(raised.exception.uncertain)

    def test_retry_after_uncertain_write_finds_the_earlier_bullet(self):
        # The bank-transfer case: Notion applied the insert, but the response never arrived.
        client = _FakeClient({"page-1": _template()})
        client.insert_error = NotionAgendaError("notion_unavailable", "timed out", uncertain=True)

        with self.assertRaises(NotionAgendaError):
            _add(client)
        client.insert_error = None
        result, _ = _add(client)

        self.assertEqual((result.status, result.duplicate_location), ("duplicate", "agenda"))
        self.assertEqual([call for call in client.writes() if call[0] == "insert"], [("insert", "page-1", "h-agenda")])
