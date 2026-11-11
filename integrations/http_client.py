"""Bounded outbound HTTP calls for integration services."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import hashlib
import json
import logging
import math
import threading
import time
from typing import Any
from urllib.parse import urlparse

import requests

DEFAULT_CONNECT_TIMEOUT_SECONDS = 3
DEFAULT_READ_TIMEOUT_SECONDS = 5
MAX_READ_TIMEOUT_SECONDS = 90
XERO_LOW_DAY_REMAINING = 1000
logger = logging.getLogger(__name__)


def _xero_cooldown_key(url: str, headers: Mapping[str, str]) -> str | None:
    """Scope Accounting API backoff to the app/tenant, without caching tokens."""
    parsed = urlparse(url)
    if parsed.hostname != "api.xero.com" or not parsed.path.startswith("/api.xro/"):
        return None
    tenant = next((value for name, value in headers.items() if name.lower() == "xero-tenant-id"), "")
    if not tenant:
        return None
    from django.conf import settings
    identity = f"{getattr(settings, 'XERO_CLIENT_ID', '')}:{tenant}"
    return "xero:api-cooldown:v1:" + hashlib.sha256(identity.encode()).hexdigest()


def _retry_after_seconds(raw: str | None) -> int:
    try:
        return max(1, int(raw or ""))
    except (TypeError, ValueError):
        try:
            deadline = parsedate_to_datetime(raw or "")
            if deadline.tzinfo is None:
                deadline = deadline.replace(tzinfo=timezone.utc)
            return max(1, math.ceil((deadline - datetime.now(timezone.utc)).total_seconds()))
        except (TypeError, ValueError, OverflowError):
            return 60


def xero_cooldown_seconds(tenant_id: str | None) -> int:
    """Return the shared remaining app/tenant wait without making a provider call."""
    key = _xero_cooldown_key("https://api.xero.com/api.xro/2.0/", {"Xero-Tenant-Id": tenant_id or ""})
    if not key:
        return 0
    from django.core.cache import cache
    deadline = cache.get(key)
    return max(0, math.ceil(deadline - time.time())) if deadline else 0


def _int_header(headers: Mapping[str, str], name: str) -> int | None:
    try:
        return int(headers.get(name) or "")
    except (TypeError, ValueError):
        return None


def _log_xero_usage(url: str, cooldown_key: str, response: requests.Response) -> None:
    """Record Xero quota use by endpoint and caller, without tokens or bodies.

    Xero's daily limit is shared by every environment using the same app and
    tenant, so a drop between two logged counts that our calls do not explain
    points at another caller.
    """
    headers = response.headers
    day_remaining = _int_header(headers, "X-DayLimit-Remaining")
    limited = response.status_code == 429
    if not limited and (day_remaining is None or (
            day_remaining >= XERO_LOW_DAY_REMAINING and day_remaining % 250)):
        return
    segments = [part for part in urlparse(url).path.split("/")[3:] if part]
    endpoint = "/".join(segments[:2] if segments[:1] == ["Reports"] else segments[:1]) or "-"
    (logger.warning if limited else logger.info)(
        "Xero API usage status=%s endpoint=%s day_remaining=%s minute_remaining=%s "
        "app_minute_remaining=%s problem=%s retry_after=%s tenant=%s caller=%s",
        response.status_code, endpoint, day_remaining,
        _int_header(headers, "X-MinLimit-Remaining"), _int_header(headers, "X-AppMinLimit-Remaining"),
        headers.get("X-Rate-Limit-Problem") or "-", headers.get("Retry-After") or "-",
        cooldown_key[-12:], threading.current_thread().name,
    )


def _xero_backoff_response(url: str, seconds: int) -> requests.Response:
    response = requests.Response()
    response.status_code = 429
    response.url = url
    response.headers.update({"Retry-After": str(seconds), "Content-Type": "application/json"})
    response._content = json.dumps({"Message": "Xero API rate limit reached; retry after the indicated interval."}).encode()
    return response

exceptions = requests.exceptions
RequestException = requests.RequestException
HTTPError = requests.HTTPError


def _split_timeout(timeout: Any = None) -> tuple[float, float]:
    if timeout is None:
        return (DEFAULT_CONNECT_TIMEOUT_SECONDS, DEFAULT_READ_TIMEOUT_SECONDS)

    if isinstance(timeout, tuple):
        if len(timeout) != 2:
            raise ValueError("HTTP timeout tuples must be (connect_timeout, read_timeout)")
        connect_timeout, read_timeout = timeout
    elif isinstance(timeout, list):
        if len(timeout) != 2:
            raise ValueError("HTTP timeout lists must be [connect_timeout, read_timeout]")
        connect_timeout, read_timeout = timeout
    elif isinstance(timeout, (int, float)):
        connect_timeout = DEFAULT_CONNECT_TIMEOUT_SECONDS
        read_timeout = timeout
    else:
        raise TypeError("HTTP timeout must be a number or a (connect, read) tuple")

    connect_timeout = float(connect_timeout)
    read_timeout = float(read_timeout)
    if connect_timeout <= 0 or read_timeout <= 0:
        raise ValueError("HTTP timeouts must be positive")
    if read_timeout > MAX_READ_TIMEOUT_SECONDS:
        read_timeout = MAX_READ_TIMEOUT_SECONDS
    return (connect_timeout, read_timeout)


def _headers_with_close(headers: Mapping[str, str] | None) -> dict[str, str]:
    resolved_headers = dict(headers or {})
    resolved_headers.setdefault("Connection", "close")
    return resolved_headers


def request(method: str, url: str, **kwargs: Any) -> requests.Response:
    kwargs["timeout"] = _split_timeout(kwargs.get("timeout"))
    kwargs["headers"] = _headers_with_close(kwargs.get("headers"))
    cooldown_key = _xero_cooldown_key(url, kwargs["headers"])
    if cooldown_key:
        from django.core.cache import cache
        deadline = cache.get(cooldown_key)
        if deadline and deadline > time.time():
            return _xero_backoff_response(url, math.ceil(deadline - time.time()))
    with requests.request(method, url, **kwargs) as response:
        response.content
        if cooldown_key:
            _log_xero_usage(url, cooldown_key, response)
        if cooldown_key and response.status_code == 429:
            seconds = _retry_after_seconds(response.headers.get("Retry-After"))
            cache.set(cooldown_key, time.time() + seconds, timeout=seconds + 1)
        return response


def get(url: str, **kwargs: Any) -> requests.Response:
    return request("GET", url, **kwargs)


def post(url: str, **kwargs: Any) -> requests.Response:
    return request("POST", url, **kwargs)


def put(url: str, **kwargs: Any) -> requests.Response:
    return request("PUT", url, **kwargs)


def patch(url: str, **kwargs: Any) -> requests.Response:
    return request("PATCH", url, **kwargs)


def delete(url: str, **kwargs: Any) -> requests.Response:
    return request("DELETE", url, **kwargs)
