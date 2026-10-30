"""Add one item to a dated Notion Meeting Notes agenda.

Order: find or create the page, read it, check for a duplicate, insert one
bullet, read it back and verify. Receipts, locks and permissions wrap this in
the endpoint layer; this module only talks to Notion through NotionAgendaClient.

Because the duplicate check always reads the page first, re-running after an
uncertain write finds the earlier bullet instead of adding a second one.

handle_agenda_request() is the endpoint entry point. It adds, in order: the
feature flag, request validation, the committee check, an idempotency receipt
and a per-date lock. Receipts and locks live in the cache, like Linear channel
writes, so no model or migration is needed.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Callable
from zoneinfo import ZoneInfo

from django.conf import settings
from django.core.cache import cache

from integrations.services.linear_meeting_actions import _atomic_cache_compare_and_delete
from integrations.services.notion_agenda_client import NotionAgendaClient, NotionAgendaError
from integrations.services.notion_agenda_page import (
    AgendaPageError,
    agenda_bullet,
    agenda_line,
    block_text,
    find_agenda_section,
    find_duplicate,
    verify_inserted,
)
from roo.permissions import can_add_meeting_agenda_items

logger = logging.getLogger(__name__)

MELBOURNE = ZoneInfo("Australia/Melbourne")
REQUEST_FIELDS = {"idempotency_key", "requester_slack_id", "owner_name", "meeting_date", "item", "page_id"}
FINAL_STATUSES = {"added", "duplicate"}
_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9.:_-]{8,128}$")
_PAGE_ID = re.compile(r"^[A-Za-z0-9-]{1,64}$")


class AgendaRequestError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


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
    recent_page: dict[str, str] | None = None,
    client: NotionAgendaClient | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> AgendaResult:
    line = agenda_line(owner, item)
    client = client or NotionAgendaClient()
    title = meeting_page_title(meeting_date)

    pages = client.find_pages_by_title(title)
    if not pages and recent_page:
        # Notion search can lag behind a page created moments ago.
        pages = [recent_page]
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

    existing_ids = {block.get("id") for block in blocks}
    inserted = client.insert_after(result.page_id, section.insert_after_id, [agenda_bullet(line)])
    result.block_id = _new_block_id(inserted, existing_ids, line)
    if not result.block_id or not verify_inserted(client.list_children(result.page_id), result.block_id, line):
        raise NotionAgendaError(
            "agenda_verify_failed",
            "Roo added the item but couldn't confirm it on the agenda. Check the page before retrying.",
            uncertain=True,
        )
    return result


def _new_block_id(returned: list[dict[str, Any]], existing_ids: set[Any], line: str) -> str:
    """Our bullet among the insert response, which can include other blocks.

    Exactly one returned block must be new to the page and carry our text.
    Otherwise (none, or a same-text block typed by hand meanwhile) return no ID,
    so the caller reports the write as uncertain instead of guessing.
    """
    candidates = [
        block for block in returned
        if block.get("id") not in existing_ids and block_text(block).strip() == line
    ]
    return str(candidates[0].get("id") or "") if len(candidates) == 1 else ""


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


def handle_agenda_request(payload: Any) -> dict[str, Any]:
    if not bool(getattr(settings, "NOTION_AGENDA_ENABLED", False)):
        raise AgendaRequestError("notion_agenda_disabled", "Adding Notion agenda items is currently disabled.")
    request = _parse_request(payload)
    if not can_add_meeting_agenda_items(request["requester_slack_id"]):
        raise AgendaRequestError(
            "agenda_requester_not_allowed", "Only committee members can add items to meeting agendas."
        )

    receipt_key = _receipt_key(request["idempotency_key"])
    fingerprint = _fingerprint(request)
    processing = f"processing:{fingerprint}:{uuid.uuid4().hex}"
    stored = _claim_receipt(receipt_key, fingerprint, processing)
    if stored is not None:
        _log_request(request, stored, replayed=True)
        return {**stored, "replayed": True}

    try:
        result = _add_under_date_lock(request)
    except Exception:
        # Safe to release even after an uncertain write: a re-run reads the
        # page before writing, so it finds the earlier bullet.
        _release(receipt_key, processing)
        raise

    body = asdict(result)
    if result.status in FINAL_STATUSES:
        completed = json.dumps({"state": "completed", "fingerprint": fingerprint, "result": body})
        cache.set(receipt_key, completed, timeout=_bounded_setting("NOTION_AGENDA_RECEIPT_TTL_SECONDS", 86400, 300, 604800))
    else:
        # "Which meeting?" and "template still loading" aren't final: the follow-up reuses the key.
        _release(receipt_key, processing)
    _log_request(request, body, replayed=False)
    return {**body, "replayed": False}


def get_agenda_receipt(idempotency_key: str) -> dict[str, Any]:
    if not _IDEMPOTENCY_KEY.match(str(idempotency_key or "")):
        raise AgendaRequestError("invalid_request", "The idempotency key is invalid.")
    value = cache.get(_receipt_key(idempotency_key))
    if value is None:
        return {"state": "not_found"}
    if str(value).startswith("processing:"):
        return {"state": "processing"}
    return {"state": "completed", "result": json.loads(value)["result"]}


def _parse_request(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise AgendaRequestError("invalid_request", "The request body must be a JSON object.")
    unknown = set(payload) - REQUEST_FIELDS
    if unknown:
        raise AgendaRequestError("invalid_request", "Unsupported fields: " + ", ".join(sorted(unknown)))
    values = {name: payload.get(name) for name in REQUEST_FIELDS}
    for name in sorted(REQUEST_FIELDS - {"page_id"}):
        if not isinstance(values[name], str) or not values[name].strip():
            raise AgendaRequestError("invalid_request", f"{name} is required.")
    if not _IDEMPOTENCY_KEY.match(values["idempotency_key"]):
        raise AgendaRequestError("invalid_request", "The idempotency key is invalid.")
    page_id = values["page_id"] or ""
    if not isinstance(page_id, str) or (page_id and not _PAGE_ID.match(page_id)):
        raise AgendaRequestError("invalid_request", "page_id is invalid.")
    try:
        meeting_date = date.fromisoformat(values["meeting_date"].strip())
    except ValueError as exc:
        raise AgendaRequestError("invalid_request", "meeting_date must be YYYY-MM-DD.") from exc
    today = _melbourne_today()
    if not today - timedelta(days=1) <= meeting_date <= today + timedelta(days=365):
        raise AgendaRequestError("invalid_meeting_date", "The meeting date must be from yesterday to a year ahead.")
    return {
        "idempotency_key": values["idempotency_key"],
        "requester_slack_id": values["requester_slack_id"].strip(),
        "owner_name": values["owner_name"],
        "meeting_date": meeting_date,
        "item": values["item"],
        "page_id": page_id,
    }


def _melbourne_today() -> date:
    return datetime.now(MELBOURNE).date()


def _add_under_date_lock(request: dict[str, Any]) -> AgendaResult:
    data_source_id = getattr(settings, "NOTION_AGENDA_DATA_SOURCE_ID", "")
    date_identity = f"{data_source_id}:{request['meeting_date'].isoformat()}"
    digest = hashlib.sha256(date_identity.encode("utf-8")).hexdigest()
    lock_key = f"notion-agenda-lock:{digest}"
    recent_key = f"notion-agenda-recent-page:{digest}"
    owner = uuid.uuid4().hex
    if not cache.add(lock_key, owner, timeout=_bounded_setting("NOTION_AGENDA_LOCK_SECONDS", 120, 30, 600)):
        raise AgendaRequestError(
            "agenda_busy", "Another agenda update for this meeting is in progress. Try again in a minute."
        )
    try:
        result = add_agenda_item(
            meeting_date=request["meeting_date"],
            owner=request["owner_name"],
            item=request["item"],
            page_id=request["page_id"],
            recent_page=cache.get(recent_key),
        )
        if result.created_page:
            cache.set(recent_key, {"id": result.page_id, "url": result.page_url}, timeout=600)
        return result
    finally:
        try:
            _atomic_cache_compare_and_delete(lock_key, owner)
        except Exception:
            # The bounded TTL releases the lock.
            logger.exception("notion_agenda_lock_release_failed")


def _claim_receipt(receipt_key: str, fingerprint: str, processing: str) -> dict[str, Any] | None:
    ttl = _bounded_setting("NOTION_AGENDA_LOCK_SECONDS", 120, 30, 600)
    for _ in range(2):
        if cache.add(receipt_key, processing, timeout=ttl):
            return None
        existing = cache.get(receipt_key)
        if existing is None:
            continue  # Expired between add and get.
        existing = str(existing)
        if existing.startswith("processing:"):
            if existing.split(":")[1] != fingerprint:
                break
            raise AgendaRequestError(
                "agenda_request_in_progress", "This request is still being processed. Check the receipt before retrying."
            )
        stored = json.loads(existing)
        if stored.get("fingerprint") != fingerprint:
            break
        return stored["result"]
    raise AgendaRequestError("idempotency_key_reused", "This idempotency key was already used for a different request.")


def _release(receipt_key: str, processing: str) -> None:
    try:
        _atomic_cache_compare_and_delete(receipt_key, processing)
    except Exception:
        logger.exception("notion_agenda_receipt_release_failed")


def _receipt_key(idempotency_key: str) -> str:
    return "notion-agenda-receipt:" + hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()


def _fingerprint(request: dict[str, Any]) -> str:
    # Item text is left out: Roo extracts it with an LLM, so a Slack retry can word it differently.
    identity = {"requester_slack_id": request["requester_slack_id"], "meeting_date": request["meeting_date"].isoformat()}
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode("utf-8")).hexdigest()


def _bounded_setting(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(getattr(settings, name, default) or default)
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(value, maximum))


def _log_request(request: dict[str, Any], body: dict[str, Any], *, replayed: bool) -> None:
    logger.info(
        "notion_agenda_request status=%s requester=%s meeting_date=%s page_id=%s created_page=%s replayed=%s",
        body.get("status"),
        request["requester_slack_id"],
        request["meeting_date"].isoformat(),
        body.get("page_id"),
        body.get("created_page"),
        replayed,
    )
