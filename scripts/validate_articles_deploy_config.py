#!/usr/bin/env python3
"""Validate the founder-journey Articles switches before production mutation.

Every setting is optional: an empty value keeps whatever the host's ``.env``
already holds, so a release never switches a founder path off by omission.
mlai/settings.py turns most of these into ints at import, so a malformed value
would stop the web and worker containers from starting.
"""

from __future__ import annotations

from datetime import date, datetime
import json
import os
import re
import sys


FLAG_KEYS = (
    "ARTICLES_FREE_SETUP_RESEARCH_ENABLED",
    "ARTICLES_FREE_FIRST_ARTICLE_ENABLED",
    "ARTICLES_FIRST_ARTICLE_GOAL_ENABLED",
    "ARTICLES_FOUNDER_JOURNEY_ALERTS_ENABLED",
)
# 0 means "no cap", "off" or, for the queue slots, "no ETA".
COUNT_KEYS = (
    "ARTICLES_FREE_SETUP_AUTOFILL_DAILY_CAP",
    "ARTICLES_FREE_SETUP_BASELINE_DAILY_CAP",
    "ARTICLES_FREE_FIRST_ARTICLE_DAILY_CAP",
    "ARTICLES_FREE_FIRST_ARTICLE_SPIKE_THRESHOLD",
    "CONTENT_FACTORY_INTERACTIVE_QUEUE_SLOTS",
)
# 0 is refused: the sweep reads it as 1, and a per-IP sign-in limit of 0 removes
# that ceiling altogether. Raise a limit for a large event instead.
POSITIVE_KEYS = (
    "ARTICLES_FOUNDER_JOURNEY_ALERTS_MAX_PER_TICK",
    "AUTH_THROTTLE_IP_CODE_REQUESTS_PER_HOUR",
    "AUTH_THROTTLE_IP_VERIFY_PER_10M",
    "AUTH_THROTTLE_IP_PASSWORD_PER_10M",
    "AUTH_THROTTLE_IP_BOOTSTRAP_AUTHENTICATED_PER_10M",
    "AUTH_THROTTLE_IP_DEVICE_AUTH_PER_10M",
)
ALERTS_SINCE = "ARTICLES_FOUNDER_JOURNEY_ALERTS_SINCE"
WORKSHOP_WINDOWS = "ARTICLES_WORKSHOP_WINDOWS"
KEYS = (*FLAG_KEYS, *COUNT_KEYS, *POSITIVE_KEYS, ALERTS_SINCE, WORKSHOP_WINDOWS)

MAX_WORKSHOP_WINDOWS = 50
# An explicit offset is required: the scheduler reads a naive time as UTC,
# which would put a Melbourne workshop eleven hours out.
WINDOW_TIME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:\d{2})")
# Only what a list of {"start", "end"} timestamps needs, so the line stays
# literal in the host's .env (no '#', '$', quotes or backslashes).
WINDOWS_CHARACTERS = re.compile(r'[\[\]{}",: 0-9TZ+\-adenrst]*')


def _window_time(value) -> datetime | None:
    if not isinstance(value, str) or not WINDOW_TIME.fullmatch(value):
        return None
    try:
        # datetime.fromisoformat accepts "Z" only from Python 3.11.
        return datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except ValueError:
        return None


def _validate_workshop_windows(key: str, value: str) -> None:
    if not WINDOWS_CHARACTERS.fullmatch(value):
        raise ValueError(f'{key} must be a JSON list of {{"start", "end"}} timestamps')
    try:
        windows = json.loads(value)
    except ValueError as exc:
        raise ValueError(f"{key} must be valid JSON") from exc
    if not isinstance(windows, list) or len(windows) > MAX_WORKSHOP_WINDOWS:
        raise ValueError(f"{key} must be a JSON list of at most {MAX_WORKSHOP_WINDOWS} windows")
    for index, window in enumerate(windows, start=1):
        if not isinstance(window, dict) or set(window) != {"start", "end"}:
            raise ValueError(f'{key} window {index} must have exactly "start" and "end"')
        start, end = _window_time(window["start"]), _window_time(window["end"])
        if start is None or end is None:
            raise ValueError(f"{key} window {index} needs ISO 8601 times with an explicit offset")
        if end <= start:
            raise ValueError(f"{key} window {index} must end after it starts")


def validate_single(key: str, value: str, *, optional: bool = False) -> None:
    """Reject malformed single-line configuration without echoing its value."""
    if optional and not value:
        return
    if not isinstance(value, str) or not value or "\n" in value or "\r" in value:
        raise ValueError(f"{key} must be a non-empty single-line value")
    if key in FLAG_KEYS:
        if value not in {"true", "false"}:
            raise ValueError(f"{key} must be true or false")
    elif key in COUNT_KEYS:
        if not re.fullmatch(r"0|[1-9][0-9]{0,8}", value):
            raise ValueError(f"{key} must be a whole number of 0 or more")
    elif key in POSITIVE_KEYS:
        if not re.fullmatch(r"[1-9][0-9]{0,8}", value):
            raise ValueError(f"{key} must be a whole number of 1 or more")
    elif key == ALERTS_SINCE:
        try:
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                raise ValueError
            date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(f"{key} must be an ISO date (YYYY-MM-DD)") from exc
    elif key == WORKSHOP_WINDOWS:
        _validate_workshop_windows(key, value)
    else:
        raise ValueError("Unsupported Articles configuration key")


def validate(environment: dict[str, str]) -> None:
    """Validate every Articles setting a release may carry; each may be left empty."""
    for key in KEYS:
        validate_single(key, environment.get(key, ""), optional=True)


if __name__ == "__main__":
    try:
        if len(sys.argv) == 3 and sys.argv[1] == "--stdin":
            validate_single(sys.argv[2], sys.stdin.read())
        elif len(sys.argv) == 1:
            validate(dict(os.environ))
        else:
            raise ValueError("Use no arguments, or --stdin with a configuration key")
    except ValueError as error:
        raise SystemExit(str(error)) from error
