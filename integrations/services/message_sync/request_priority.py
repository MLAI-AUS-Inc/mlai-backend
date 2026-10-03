"""Classify provider work without changing Slack's shared method allowance."""
from contextlib import contextmanager
from contextvars import ContextVar

_priority = ContextVar("message_sync_request_priority", default="foreground")


def current_priority():
    """Return the priority of this task, independently of concurrent workers."""
    return _priority.get()


@contextmanager
def request_priority(priority):
    """Give explicit reads priority over bulk work; always restore the caller."""
    if priority not in {"foreground", "background"}:
        raise ValueError("Unknown provider request priority")
    token = _priority.set(priority)
    try:
        yield
    finally:
        _priority.reset(token)
