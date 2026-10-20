"""Add one item to a dated Notion Meeting Notes agenda.

Order: find or create the page, read it, check for a duplicate, insert one
bullet, read it back and verify. Receipts, locks and permissions wrap this in
the endpoint layer; this module only talks to Notion through NotionAgendaClient.

Because the duplicate check always reads the page first, re-running after an
uncertain write finds the earlier bullet instead of adding a second one.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable

from django.conf import settings

from integrations.services.notion_agenda_client import NotionAgendaClient, NotionAgendaError
from integrations.services.notion_agenda_page import (
    AgendaPageError,
    agenda_bullet,
    agenda_line,
    find_agenda_section,
    find_duplicate,
    verify_inserted,
)


@dataclass
class AgendaResult:
    status: str  # added | duplicate | ambiguous | template_pending
    page_title: str
    page_id: str = ""
    page_url: str = ""
    block_id: str = ""
    duplicate_location: str = ""
    created_page: bool = False
    choices: list[dict[str, str]] = field(default_factory=list)


def meeting_page_title(meeting_date: date) -> str:
    return f"{meeting_date.isoformat()} Meeting Notes"


def add_agenda_item(
    *,
    meeting_date: date,
    owner: str,
    item: str,
    page_id: str = "",
    client: NotionAgendaClient | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> AgendaResult:
    line = agenda_line(owner, item)
    client = client or NotionAgendaClient()
    title = meeting_page_title(meeting_date)

    pages = client.find_pages_by_title(title)
    created_page = False
    if page_id:
        page = next((candidate for candidate in pages if candidate.get("id") == page_id), None)
        if page is None:
            raise AgendaPageError("meeting_page_mismatch", "That page isn't a meeting page for this date.")
    elif len(pages) > 1:
        return AgendaResult(
            status="ambiguous",
            page_title=title,
            choices=[{"page_id": str(p.get("id") or ""), "page_url": str(p.get("url") or "")} for p in pages],
        )
    elif pages:
        page = pages[0]
    else:
        page = client.create_page_from_template(title)
        created_page = True

    result = AgendaResult(
        status="added",
        page_title=title,
        page_id=str(page["id"]),
        page_url=str(page.get("url") or ""),
        created_page=created_page,
    )
    if created_page:
        blocks = _wait_for_template(client, result.page_id, sleep)
        if blocks is None:
            result.status = "template_pending"
            return result
    else:
        blocks = client.list_children(result.page_id)

    section = find_agenda_section(blocks)
    location = find_duplicate(section, item)
    if location:
        result.status = "duplicate"
        result.duplicate_location = location
        return result

    inserted = client.insert_after(result.page_id, section.insert_after_id, [agenda_bullet(line)])
    result.block_id = str(inserted[0].get("id") or "")
    if not result.block_id or not verify_inserted(client.list_children(result.page_id), result.block_id, line):
        raise NotionAgendaError(
            "agenda_verify_failed",
            "Roo added the item but couldn't confirm it on the agenda. Check the page before retrying.",
            uncertain=True,
        )
    return result


def _wait_for_template(client: NotionAgendaClient, page_id: str, sleep: Callable[[float], None]) -> list[dict[str, Any]] | None:
    """Notion applies templates asynchronously, so a new page starts blank."""

    attempts = max(1, min(int(getattr(settings, "NOTION_AGENDA_TEMPLATE_POLL_ATTEMPTS", 6) or 6), 20))
    delay = max(0.2, min(float(getattr(settings, "NOTION_AGENDA_TEMPLATE_POLL_SECONDS", 1.0) or 1.0), 5.0))
    for attempt in range(attempts):
        if attempt:
            sleep(delay)
        blocks = client.list_children(page_id)
        try:
            find_agenda_section(blocks)
        except AgendaPageError as exc:
            if exc.code != "agenda_section_not_found":
                raise
            continue
        return blocks
    return None
