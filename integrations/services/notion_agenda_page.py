"""Pure page logic for Notion Meeting Notes agendas.

Works on a page's top-level blocks as returned by the Notion API. Nothing here
makes HTTP calls, so every rule can be tested against fixture pages.

The Agenda section starts at the only heading whose
text is "Agenda" and ends at the next heading or the end of the page. Inside
it, an "If time:" paragraph splits the main list from the "If time:" list.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

HEADING_TYPES = {"heading_1", "heading_2", "heading_3"}
LIST_ITEM_TYPES = {"bulleted_list_item", "numbered_list_item"}
MAX_ITEM_LENGTH = 500
MAX_OWNER_LENGTH = 80

_LEADING_OWNER = re.compile(r"^\[[^\]]*\]\s*[-–—:]?\s*")
_TRAILING_OWNER = re.compile(r"\s*\[[^\]]*\]$")
_TRAILING_PUNCTUATION = re.compile(r"[\s.!?;:,]+$")


class AgendaPageError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass
class AgendaSection:
    heading_id: str
    main_items: list[dict[str, Any]] = field(default_factory=list)
    if_time_items: list[dict[str, Any]] = field(default_factory=list)

    @property
    def insert_after_id(self) -> str:
        filled = [block for block in self.main_items if _is_filled(block)]
        return filled[-1]["id"] if filled else self.heading_id


def block_text(block: dict[str, Any]) -> str:
    payload = block.get(block.get("type") or "")
    if not isinstance(payload, dict):
        return ""
    parts = payload.get("rich_text") if isinstance(payload.get("rich_text"), list) else []
    return "".join(
        str(part.get("plain_text") or (part.get("text") or {}).get("content") or "")
        for part in parts
        if isinstance(part, dict)
    )


def normalise_item(text: str) -> str:
    value = " ".join(str(text or "").replace("’", "'").split())
    value = _LEADING_OWNER.sub("", value)
    value = _TRAILING_OWNER.sub("", value)
    value = _TRAILING_PUNCTUATION.sub("", value)
    return value.lower()


def find_agenda_section(blocks: list[dict[str, Any]]) -> AgendaSection:
    starts = [
        index
        for index, block in enumerate(blocks)
        if block.get("type") in HEADING_TYPES and block_text(block).strip().lower() == "agenda"
    ]
    if not starts:
        raise AgendaPageError("agenda_section_not_found", "The meeting page has no Agenda heading.")
    if len(starts) > 1:
        raise AgendaPageError("agenda_section_ambiguous", "The meeting page has more than one Agenda heading.")

    heading = blocks[starts[0]]
    if (heading.get(heading["type"]) or {}).get("is_toggleable"):
        # Its items would be children of the heading, which this rule doesn't read.
        raise AgendaPageError("agenda_section_unsupported", "The Agenda heading is a toggle heading.")

    section = AgendaSection(heading_id=heading["id"])
    current = section.main_items
    for block in blocks[starts[0] + 1:]:
        block_type = block.get("type")
        if block_type in HEADING_TYPES:
            break
        if block_type == "paragraph" and _is_if_time(block):
            current = section.if_time_items
        elif block_type in LIST_ITEM_TYPES:
            current.append(block)
    return section


def find_duplicate(section: AgendaSection, item: str) -> str | None:
    """Return "agenda" or "if_time" when the item is already listed (spec D9)."""

    wanted = normalise_item(item)
    for location, items in (("agenda", section.main_items), ("if_time", section.if_time_items)):
        if any(normalise_item(block_text(block)) == wanted for block in items):
            return location
    return None


def agenda_line(owner: str, item: str) -> str:
    clean_owner = " ".join(str(owner or "").replace("[", "").replace("]", "").split())
    clean_item = " ".join(str(item or "").split())
    if not clean_owner or len(clean_owner) > MAX_OWNER_LENGTH:
        raise AgendaPageError("invalid_owner", "The requester's name is missing or too long.")
    if not normalise_item(clean_item):
        raise AgendaPageError("invalid_item", "The agenda item is empty.")
    if len(clean_item) > MAX_ITEM_LENGTH:
        raise AgendaPageError("invalid_item", "The agenda item is too long.")
    return f"[{clean_owner}] - {clean_item}"


def agenda_bullet(line: str) -> dict[str, Any]:
    return {
        "object": "block",
        "type": "bulleted_list_item",
        "bulleted_list_item": {"rich_text": [{"type": "text", "text": {"content": line}}]},
    }


def verify_inserted(blocks: list[dict[str, Any]], block_id: str, line: str) -> bool:
    """True only when the inserted block sits in the main Agenda list with the expected text."""

    try:
        section = find_agenda_section(blocks)
    except AgendaPageError:
        return False
    return any(
        block.get("id") == block_id and block_text(block).strip() == line
        for block in section.main_items
    )


def _is_filled(block: dict[str, Any]) -> bool:
    return bool(block_text(block).strip()) or bool(block.get("has_children"))


def _is_if_time(block: dict[str, Any]) -> bool:
    return block_text(block).strip().rstrip(":").strip().lower() == "if time"
