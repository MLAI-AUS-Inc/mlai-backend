"""A thin listmonk v6.2 API client for crm email blasts.

Calls go through ``integrations.http_client`` with HTTP Basic auth as a
listmonk API user (``LISTMONK_API_USER`` / ``LISTMONK_API_TOKEN``) against
``LISTMONK_BASE_URL``. Every failure raises ``ListmonkError``; ``transient``
tells an outage (try again next tick) from a refusal.

listmonk's subscriber ``query`` parameter is an SQL expression, so values
put into one go through ``sql_string`` / ``sql_timestamp`` / ``sql_ids``.
"""

from __future__ import annotations

from datetime import datetime, timezone as dt_timezone
from typing import Any, Iterable, Optional

from django.conf import settings

from integrations import http_client

TIMEOUT = (3, 20)
PAGE_SIZE = 500
MAX_PAGES = 200
FINISHED = "finished"
RUNNING = "running"
CANCELLED = "cancelled"
DRAFT = "draft"


class ListmonkError(RuntimeError):
    """A listmonk call failed; ``code`` is short and never holds personal data."""

    def __init__(self, code: str, status_code: Optional[int] = None):
        self.code, self.status_code = code, status_code
        super().__init__(code)

    @property
    def transient(self) -> bool:
        """No answer, a rate limit or a server error: worth trying again later."""

        return self.status_code is None or self.status_code == 429 or self.status_code >= 500

    @property
    def credentials_rejected(self) -> bool:
        return self.status_code in (401, 403)

    @property
    def conflict(self) -> bool:
        return self.status_code == 409


def _setting(name: str) -> str:
    return str(getattr(settings, name, "") or "").strip()


def configured() -> bool:
    """A base URL and an API user are set."""

    return all(_setting(name) for name in ("LISTMONK_BASE_URL", "LISTMONK_API_USER", "LISTMONK_API_TOKEN"))


def template_id() -> Optional[int]:
    value = getattr(settings, "LISTMONK_TEMPLATE_ID", None)
    return value if isinstance(value, int) and value > 0 else None


def _request(method: str, path: str, *, params=None, payload=None, raw: bool = False) -> Any:
    if not configured():
        raise ListmonkError("not_configured")
    url = _setting("LISTMONK_BASE_URL").rstrip("/") + path
    kwargs: dict[str, Any] = {
        "auth": (_setting("LISTMONK_API_USER"), _setting("LISTMONK_API_TOKEN")),
        "timeout": TIMEOUT,
    }
    if params is not None:
        kwargs["params"] = params
    if payload is not None:
        kwargs["json"] = payload
    try:
        response = http_client.request(method, url, **kwargs)
    except http_client.exceptions.Timeout:
        raise ListmonkError("timeout") from None
    except http_client.RequestException:
        raise ListmonkError("connection_error") from None
    if response.status_code >= 300:
        raise ListmonkError(f"http_{response.status_code}", response.status_code)
    if raw:
        return response.text
    try:
        body = response.json()
    except ValueError:
        raise ListmonkError("unreadable_response") from None
    if not isinstance(body, dict) or "data" not in body:
        raise ListmonkError("unreadable_response")
    return body["data"]


def _id(data: Any) -> int:
    value = data.get("id") if isinstance(data, dict) else None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ListmonkError("unreadable_response")
    return value


def _results(data: Any) -> list[dict]:
    results = data.get("results") if isinstance(data, dict) else None
    if not isinstance(results, list):
        raise ListmonkError("unreadable_response")
    return [item for item in results if isinstance(item, dict)]


# Lists


def create_list(name: str, *, tags: Iterable[str] = (), description: str = "") -> int:
    """A private, single opt-in list; returns its id."""

    data = _request(
        "POST",
        "/api/lists",
        payload={
            "name": name,
            "type": "private",
            "optin": "single",
            "tags": list(tags),
            "description": description,
        },
    )
    return _id(data)


def find_list(name: str) -> Optional[int]:
    """The id of the list with exactly this name, if one exists."""

    data = _request("GET", "/api/lists", params={"query": name, "per_page": 50})
    if isinstance(data, list):
        results = [item for item in data if isinstance(item, dict)]
    else:
        results = _results(data)
    for item in results:
        if item.get("name") == name:
            return _id(item)
    return None


# Subscribers


def query_subscribers(query: str, *, per_page: int = PAGE_SIZE, max_pages: int = MAX_PAGES) -> list[dict]:
    """Every subscriber matching an SQL ``query`` expression, oldest id first."""

    found: list[dict] = []
    for page in range(1, max_pages + 1):
        data = _request(
            "GET",
            "/api/subscribers",
            params={
                "query": query,
                "page": page,
                "per_page": per_page,
                "order_by": "id",
                "order": "asc",
            },
        )
        results = _results(data)
        found.extend(results)
        total = data.get("total") if isinstance(data.get("total"), int) else None
        if len(results) < per_page or (total is not None and len(found) >= total):
            return found
    raise ListmonkError("too_many_pages")


def create_subscriber(email: str, name: str, *, lists: Iterable[int] = (), attribs=None) -> dict:
    """A new enabled subscriber, pre-confirmed on ``lists``; 409 if the email exists."""

    data = _request(
        "POST",
        "/api/subscribers",
        payload={
            "email": email,
            "name": name,
            "status": "enabled",
            "lists": list(lists),
            "attribs": attribs or {},
            "preconfirm_subscriptions": True,
        },
    )
    _id(data)
    return data


def update_subscriber(subscriber_id: int, payload: dict) -> dict:
    """Replace a subscriber. listmonk drops every list missing from ``lists``."""

    return _request("PUT", f"/api/subscribers/{int(subscriber_id)}", payload=payload)


def add_subscribers_to_list(subscriber_ids: Iterable[int], list_id: int) -> None:
    ids = sorted({int(value) for value in subscriber_ids})
    if not ids:
        return
    _request(
        "PUT",
        "/api/subscribers/lists",
        payload={
            "ids": ids,
            "action": "add",
            "target_list_ids": [int(list_id)],
            "status": "confirmed",
        },
    )


def blocklist_subscribers(subscriber_ids: Iterable[int]) -> None:
    """Blocklist everywhere: listmonk also unsubscribes them from every list."""

    ids = sorted({int(value) for value in subscriber_ids})
    if ids:
        _request("PUT", "/api/subscribers/blocklist", payload={"ids": ids})


# Campaigns


def create_campaign(payload: dict) -> dict:
    data = _request("POST", "/api/campaigns", payload=payload)
    _id(data)
    return data


def update_campaign(campaign_id: int, payload: dict) -> dict:
    """Replace a draft campaign's content; listmonk refuses once it has started."""

    return _request("PUT", f"/api/campaigns/{int(campaign_id)}", payload=payload)


def get_campaign(campaign_id: int) -> dict:
    data = _request("GET", f"/api/campaigns/{int(campaign_id)}", params={"no_body": "true"})
    if not isinstance(data, dict):
        raise ListmonkError("unreadable_response")
    return data


def find_campaign(name: str) -> Optional[dict]:
    """The campaign with exactly this name, if one exists."""

    data = _request(
        "GET", "/api/campaigns", params={"query": name, "per_page": 50, "no_body": "true"}
    )
    for item in _results(data):
        if item.get("name") == name:
            _id(item)
            return item
    return None


def set_campaign_status(campaign_id: int, status: str) -> dict:
    """``running`` starts a draft or paused campaign; only a running one can be ``cancelled``."""

    return _request("PUT", f"/api/campaigns/{int(campaign_id)}/status", payload={"status": status})


def test_campaign(campaign_id: int, payload: dict, emails: Iterable[str]) -> None:
    """Send the given content to existing subscribers by email, without running the campaign."""

    _request(
        "POST",
        f"/api/campaigns/{int(campaign_id)}/test",
        payload={**payload, "subscribers": list(emails)},
    )


def preview_campaign(campaign_id: int) -> str:
    """The campaign rendered as HTML for a sample subscriber."""

    return _request("GET", f"/api/campaigns/{int(campaign_id)}/preview", raw=True)


# Values inside subscriber ``query`` expressions


def sql_string(value: str) -> str:
    """A quoted SQL string literal; backslashes and control characters are refused."""

    text = str(value)
    if "\\" in text or any(ord(char) < 32 or ord(char) == 127 for char in text):
        raise ListmonkError("unsafe_query_value")
    return "'" + text.replace("'", "''") + "'"


def sql_timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt_timezone.utc)
    stamp = value.astimezone(dt_timezone.utc).strftime("%Y-%m-%d %H:%M:%S+00")
    return f"'{stamp}'::timestamptz"


def sql_ids(values: Iterable[int]) -> str:
    ids = sorted({int(value) for value in values})
    if not ids:
        raise ListmonkError("empty_id_list")
    return "(" + ",".join(str(value) for value in ids) + ")"
