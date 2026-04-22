"""Bounded outbound HTTP calls for integration services."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import requests

DEFAULT_CONNECT_TIMEOUT_SECONDS = 3
DEFAULT_READ_TIMEOUT_SECONDS = 5
MAX_READ_TIMEOUT_SECONDS = 30

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
    with requests.request(method, url, **kwargs) as response:
        response.content
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
