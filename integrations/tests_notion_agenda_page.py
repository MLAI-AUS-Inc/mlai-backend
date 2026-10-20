"""Fixture pages are shaped like real Meeting Notes exports, with made-up owners."""

from __future__ import annotations

from django.test import SimpleTestCase

from integrations.services.notion_agenda_page import (
    AgendaPageError,
    agenda_bullet,
    agenda_line,
    find_agenda_section,
    find_duplicate,
    normalise_item,
    verify_inserted,
)


def _block(block_id, block_type, text="", **extra):
    payload = {"rich_text": [{"type": "text", "plain_text": text}] if text else []}
    payload.update(extra.pop("payload", {}))
    return {"id": block_id, "type": block_type, block_type: payload, "has_children": False, **extra}


def _heading(block_id, text, level=1, toggle=False):
    return _block(block_id, f"heading_{level}", text, payload={"is_toggleable": toggle})


def _bullet(block_id, text="", has_children=False):
    return _block(block_id, "bulleted_list_item", text, has_children=has_children)


def _paragraph(block_id, text=""):
    return _block(block_id, "paragraph", text)


def _page_top():
    return [
        _heading("h-attendees", "Attendees"),
        _heading("h-actions", "Action Items"),
        _block("todo", "to_do"),
        _heading("h-notes", "Notes from Quarterly planning"),
    ]


# Fresh template (shape of 2026-10-27).
FRESH_TEMPLATE = _page_top() + [
    _heading("h-agenda", "Agenda"),
    _bullet("empty-1"),
    _bullet("empty-2"),
    _paragraph("if-time", "If time:"),
    _bullet("empty-3"),
]

# Filled page with an "If time:" list (shape of 2026-09-22).
FILLED_WITH_IF_TIME = _page_top() + [
    _heading("h-agenda", "Agenda"),
    _bullet("a1", "[Alex] - Update on goals and how we’re tracking", has_children=True),
    _bullet("a2", "[Alex, Jordan] - Workshops to book in ASAP"),
    _bullet("a3", "[Jordan] - Merch?"),
    _paragraph("if-time", "If time:"),
    _bullet("t1", "Partner with another Melbourne community [Riley]"),
    _bullet("t2", "Potential event space at another coworking space [Riley]"),
]

# Hand-edited page: no "If time:", intro paragraphs, trailing empty heading (shape of 2026-10-20).
HAND_EDITED = _page_top() + [
    _heading("h-last-week", "Last week: 13 October"),
    _paragraph("summary", "Last week we agreed to keep the newsletter weekly."),
    _heading("h-agenda", "Agenda"),
    _paragraph("when", "Yesterday 6:30 PM → 7:00 PM"),
    _paragraph("intro", "Suggested discussion leads are listed below."),
    _bullet("b1", "1. Review from last week"),
    _bullet("b2", "2. Upcoming events: lock delivery owners (7 min)", has_children=True),
    _bullet("b8", "8. Leadership coverage and close (4 min)", has_children=True),
    _heading("h-empty", ""),
]

# Older template: "Notes" heading, no "If time:", trailing empty bullet (shape of 2026-08-10).
OLDER_TEMPLATE = [
    _heading("h-attendees", "Attendees"),
    _heading("h-notes", "Notes"),
    _heading("h-agenda", "Agenda"),
    _bullet("c1", "[Alex, Casey] - Committee voting"),
    _bullet("c2", "[Morgan] - Events 15th", has_children=True),
    _bullet("c-empty", ""),
]


class FindAgendaSectionTests(SimpleTestCase):
    def test_fresh_template_inserts_after_heading_and_skips_empty_bullets(self):
        section = find_agenda_section(FRESH_TEMPLATE)
        self.assertEqual(section.insert_after_id, "h-agenda")
        self.assertEqual([b["id"] for b in section.main_items], ["empty-1", "empty-2"])
        self.assertEqual([b["id"] for b in section.if_time_items], ["empty-3"])

    def test_filled_page_inserts_after_last_main_item_not_if_time(self):
        section = find_agenda_section(FILLED_WITH_IF_TIME)
        self.assertEqual(section.insert_after_id, "a3")
        self.assertEqual([b["id"] for b in section.if_time_items], ["t1", "t2"])

    def test_hand_edited_page_ends_at_next_heading_and_ignores_paragraphs(self):
        section = find_agenda_section(HAND_EDITED)
        self.assertEqual(section.insert_after_id, "b8")
        self.assertEqual([b["id"] for b in section.main_items], ["b1", "b2", "b8"])
        self.assertEqual(section.if_time_items, [])

    def test_older_template_skips_trailing_empty_bullet(self):
        self.assertEqual(find_agenda_section(OLDER_TEMPLATE).insert_after_id, "c2")

    def test_empty_bullet_with_children_counts_as_filled(self):
        blocks = [_heading("h-agenda", "Agenda"), _bullet("x1", "[Alex] - Topic"), _bullet("x2", "", has_children=True)]
        self.assertEqual(find_agenda_section(blocks).insert_after_id, "x2")

    def test_agenda_must_be_a_heading_with_exact_text(self):
        blocks = [_paragraph("p", "Agenda"), _heading("h", "Agenda notes"), _bullet("b", "Topic")]
        with self.assertRaises(AgendaPageError) as raised:
            find_agenda_section(blocks)
        self.assertEqual(raised.exception.code, "agenda_section_not_found")

    def test_two_agenda_headings_are_refused(self):
        blocks = [_heading("h1", "Agenda"), _bullet("b", "Topic"), _heading("h2", "agenda", level=2)]
        with self.assertRaises(AgendaPageError) as raised:
            find_agenda_section(blocks)
        self.assertEqual(raised.exception.code, "agenda_section_ambiguous")

    def test_toggle_agenda_heading_is_refused(self):
        with self.assertRaises(AgendaPageError) as raised:
            find_agenda_section([_heading("h", "Agenda", toggle=True)])
        self.assertEqual(raised.exception.code, "agenda_section_unsupported")


class DuplicateTests(SimpleTestCase):
    def test_normalise_strips_owner_tags_case_space_and_punctuation(self):
        self.assertEqual(
            normalise_item("[Jordan] -  Monthly check-ins between presidents and committee members."),
            normalise_item("monthly check-ins between presidents and committee members [Alex]"),
        )

    def test_match_in_main_list_with_different_owner(self):
        self.assertEqual(find_duplicate(find_agenda_section(FILLED_WITH_IF_TIME), "[Casey] - merch"), "agenda")

    def test_curly_apostrophe_matches_straight_one(self):
        section = find_agenda_section(FILLED_WITH_IF_TIME)
        self.assertEqual(find_duplicate(section, "Update on goals and how we're tracking"), "agenda")

    def test_match_in_if_time_list(self):
        section = find_agenda_section(FILLED_WITH_IF_TIME)
        self.assertEqual(find_duplicate(section, "Potential event space at another coworking space"), "if_time")

    def test_reworded_item_is_not_a_duplicate(self):
        section = find_agenda_section(FILLED_WITH_IF_TIME)
        self.assertIsNone(find_duplicate(section, "Merch for the community"))

    def test_empty_template_bullets_never_match(self):
        self.assertIsNone(find_duplicate(find_agenda_section(FRESH_TEMPLATE), "Monthly check-ins"))


class AgendaLineTests(SimpleTestCase):
    def test_formats_owner_and_item(self):
        self.assertEqual(
            agenda_line("Alex", "Monthly check-ins between presidents and committee members"),
            "[Alex] - Monthly check-ins between presidents and committee members",
        )

    def test_collapses_newlines_and_strips_brackets_from_owner(self):
        self.assertEqual(agenda_line("[Alex]", "Monthly\n check-ins"), "[Alex] - Monthly check-ins")

    def test_rejects_empty_item_and_missing_owner(self):
        for owner, item, code in (("Alex", "  ", "invalid_item"), ("Alex", "[Jordan]", "invalid_item"), ("", "Topic", "invalid_owner")):
            with self.subTest(owner=owner, item=item):
                with self.assertRaises(AgendaPageError) as raised:
                    agenda_line(owner, item)
                self.assertEqual(raised.exception.code, code)

    def test_rejects_overlong_item(self):
        with self.assertRaises(AgendaPageError):
            agenda_line("Alex", "x" * 501)

    def test_bullet_block_carries_the_line(self):
        block = agenda_bullet("[Alex] - Topic")
        self.assertEqual(block["type"], "bulleted_list_item")
        self.assertEqual(block["bulleted_list_item"]["rich_text"][0]["text"]["content"], "[Alex] - Topic")


class VerifyInsertedTests(SimpleTestCase):
    line = "[Alex] - Monthly check-ins"

    def _with(self, position, block):
        blocks = list(FRESH_TEMPLATE)
        blocks.insert(position, block)
        return blocks

    def test_verified_when_block_is_in_main_list_with_text(self):
        blocks = self._with(5, _bullet("new", self.line))
        self.assertTrue(verify_inserted(blocks, "new", self.line))

    def test_not_verified_when_block_landed_under_if_time(self):
        blocks = self._with(len(FRESH_TEMPLATE), _bullet("new", self.line))
        self.assertFalse(verify_inserted(blocks, "new", self.line))

    def test_not_verified_when_text_differs_or_block_missing(self):
        blocks = self._with(5, _bullet("new", "[Alex] - Something else"))
        self.assertFalse(verify_inserted(blocks, "new", self.line))
        self.assertFalse(verify_inserted(FRESH_TEMPLATE, "new", self.line))

    def test_hand_typed_copy_does_not_count_as_the_inserted_block(self):
        blocks = self._with(5, _bullet("typed-by-hand", self.line))
        self.assertFalse(verify_inserted(blocks, "new", self.line))

    def test_not_verified_when_agenda_heading_disappears(self):
        self.assertFalse(verify_inserted([_bullet("new", self.line)], "new", self.line))
