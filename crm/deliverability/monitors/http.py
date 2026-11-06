"""Fixed-origin provider clients with redacted failures and bounded pagination."""

from contextlib import contextmanager
from contextvars import ContextVar
import time

from integrations import http_client

from .. import policy


class Unavailable(Exception):
    """A provider did not return an authoritative observation."""


_deadline = ContextVar('crm_monitor_deadline', default=None)


@contextmanager
def budget(seconds):
    token = _deadline.set(time.monotonic() + seconds)
    try:
        yield
    finally:
        _deadline.reset(token)


def remaining(default):
    end = _deadline.get()
    result = min(default, end - time.monotonic()) if end is not None else default
    if result <= 0:
        raise Unavailable('check_budget_exhausted')
    return result


def json_request(method, url, **kwargs):
    try:
        response = http_client.request(method, url, timeout=(min(3, remaining(3)), remaining(policy.value('CRM_MONITOR_HTTP_SECONDS'))),
            allow_redirects=False, **kwargs)
        if response.status_code not in (200, 201) or len(response.content) > 2_000_000:
            raise Unavailable('provider_unavailable')
        body = response.json()
        if not isinstance(body, dict):
            raise Unavailable('invalid_response')
        return body
    except (http_client.RequestException, ValueError, TypeError):
        raise Unavailable('provider_unavailable') from None
