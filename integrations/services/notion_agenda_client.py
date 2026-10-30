"""Notion HTTP calls for adding items to Meeting Notes agendas.

Unlike the org-memory reader, no status is treated as "missing": every failure
reaches the caller as a NotionAgendaError with a generic code. Write failures
that may still have landed in Notion are marked ``uncertain`` so the caller
reads the page back before retrying.
"""

from __future__ import annotations

from typing import Any

from django.conf import settings

from integrations import http_client

NOTION_BASE_URL = "https://api.notion.com/v1"
WRITE_METHODS = {"POST", "PATCH"}


class NotionAgendaError(Exception):
    def __init__(self, code: str, message: str, *, uncertain: bool = False, retry_after: int | None = None):
        super().__init__(message)
        self.code = code
        self.uncertain = uncertain
        self.retry_after = retry_after


class NotionAgendaClient:
    def __init__(self):
        self.token = str(getattr(settings, "NOTION_AGENDA_API_TOKEN", "") or "").strip()
        self.data_source_id = str(getattr(settings, "NOTION_AGENDA_DATA_SOURCE_ID", "") or "").strip()
        self.template_id = str(getattr(settings, "NOTION_AGENDA_TEMPLATE_ID", "") or "").strip()
        if not (self.token and self.data_source_id and self.template_id):
            raise NotionAgendaError("notion_not_configured", "Notion agenda access is not configured.")
        self.headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Notion-Version": str(getattr(settings, "NOTION_API_VERSION", "2026-03-11")),
        }
        self._title_property: str | None = None

    def request(self, method: str, path: str, *, params=None, body=None) -> dict[str, Any]:
        is_write = method.upper() in WRITE_METHODS
        try:
            response = http_client.request(
                method,
                f"{NOTION_BASE_URL}{path}",
                headers=self.headers,
                params=params,
                json=body,
                timeout=(3, 20),
            )
        except http_client.RequestException as exc:
            # A timed-out write may still have been applied by Notion.
            raise NotionAgendaError(
                "notion_unavailable", "Roo couldn't reach Notion.", uncertain=is_write
            ) from exc
        status = response.status_code
        if status in {401, 403}:
            raise NotionAgendaError("notion_access_denied", "Roo no longer has access to Meeting Notes in Notion.")
        if status == 404:
            raise NotionAgendaError("notion_not_found", "The Notion page or database could not be found.")
        if status == 429:
            try:
                retry_after = max(1, int(response.headers.get("Retry-After") or 1))
            except ValueError:
                retry_after = 1
            raise NotionAgendaError(
                "notion_rate_limited", "Notion is rate limiting requests.", retry_after=retry_after
            )
        if status >= 500:
            raise NotionAgendaError("notion_unavailable", "Notion is unavailable.", uncertain=is_write)
        if status >= 400:
            raise NotionAgendaError("notion_rejected", "Notion rejected the request.")
        try:
            payload = response.json()
        except ValueError as exc:
            raise NotionAgendaError("notion_invalid_response", "Notion returned invalid JSON.", uncertain=is_write) from exc
        if not isinstance(payload, dict):
            raise NotionAgendaError("notion_invalid_response", "Notion returned an unexpected response.", uncertain=is_write)
        return payload

    def title_property(self) -> str:
        if self._title_property is None:
            data_source = self.request("GET", f"/data_sources/{self.data_source_id}")
            properties = data_source.get("properties") if isinstance(data_source.get("properties"), dict) else {}
            names = [name for name, prop in properties.items() if isinstance(prop, dict) and prop.get("type") == "title"]
            if len(names) != 1:
                raise NotionAgendaError("notion_invalid_response", "Meeting Notes has no single title property.")
            self._title_property = names[0]
        return self._title_property

    def find_pages_by_title(self, title: str) -> list[dict[str, Any]]:
        body = {
            "filter": {"property": self.title_property(), "title": {"equals": title}},
            "page_size": 10,
        }
        payload = self.request("POST", f"/data_sources/{self.data_source_id}/query", body=body)
        results = payload.get("results")
        if not isinstance(results, list):
            raise NotionAgendaError("notion_invalid_response", "Notion returned an unexpected response.")
        return [page for page in results if isinstance(page, dict) and not page.get("in_trash")]

    def create_page_from_template(self, title: str) -> dict[str, Any]:
        body = {
            "parent": {"type": "data_source_id", "data_source_id": self.data_source_id},
            "properties": {self.title_property(): {"title": [{"type": "text", "text": {"content": title}}]}},
            "template": {"type": "template_id", "template_id": self.template_id},
        }
        page = self.request("POST", "/pages", body=body)
        if not page.get("id"):
            raise NotionAgendaError("notion_invalid_response", "Notion did not return the new page.", uncertain=True)
        return page

    def list_children(self, block_id: str, *, max_blocks: int = 500) -> list[dict[str, Any]]:
        blocks: list[dict[str, Any]] = []
        cursor = None
        seen_cursors: set[str] = set()
        while len(blocks) < max_blocks:
            params = {"page_size": 100}
            if cursor:
                params["start_cursor"] = cursor
            payload = self.request("GET", f"/blocks/{block_id}/children", params=params)
            results = payload.get("results")
            if not isinstance(results, list):
                raise NotionAgendaError("notion_invalid_response", "Notion returned an unexpected response.")
            blocks.extend(block for block in results if isinstance(block, dict))
            if not payload.get("has_more"):
                return blocks
            cursor = payload.get("next_cursor")
            if not cursor or cursor in seen_cursors:
                raise NotionAgendaError("notion_invalid_response", "Notion pagination did not advance.")
            seen_cursors.add(cursor)
        raise NotionAgendaError("notion_page_too_large", "The meeting page has too many blocks to check safely.")

    def insert_after(self, parent_id: str, after_block_id: str, children: list[dict[str, Any]]) -> list[dict[str, Any]]:
        body = {
            "children": children,
            "position": {"type": "after_block", "after_block": {"id": after_block_id}},
        }
        payload = self.request("PATCH", f"/blocks/{parent_id}/children", body=body)
        results = payload.get("results")
        # The response can hold more blocks than were sent (seen live with
        # after_block), so the caller picks out its own block.
        if not isinstance(results, list) or not results:
            raise NotionAgendaError(
                "notion_invalid_response", "Notion did not confirm the inserted blocks.", uncertain=True
            )
        return [block for block in results if isinstance(block, dict)]
