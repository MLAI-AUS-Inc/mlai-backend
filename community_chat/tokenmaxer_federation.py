"""Read-only federation with the public Tokenmaxer leaderboard.

Tokenmaxer deliberately exposes an unauthenticated leaderboard API.  MLAI
reads that public aggregate only; it never receives or stores another user's
reporter credential or private session content.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote

import requests
from django.conf import settings
from django.core.cache import cache

from .token_usage import SOURCES, TOKEN_FIELDS, normalized_token_total


logger = logging.getLogger(__name__)
MAX_PUBLIC_ENTRIES = 500
MAX_PUBLIC_COUNT = 10**15


def _non_negative_int(value):
    if isinstance(value, bool):
        return 0
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return 0
    return min(max(parsed, 0), MAX_PUBLIC_COUNT)


def _fold_source_payloads(payloads):
    """Normalize and merge one upstream leaderboard response per source."""
    by_username = {}
    for source, payload in payloads.items():
        if source not in SOURCES or not isinstance(payload, dict):
            continue
        raw_entries = payload.get("entries")
        if not isinstance(raw_entries, list):
            continue
        for raw in raw_entries:
            if not isinstance(raw, dict):
                continue
            username = raw.get("username")
            if not isinstance(username, str):
                continue
            username = username.strip()
            if not username or len(username) > 80:
                continue
            key = username.casefold()
            entry = by_username.setdefault(
                key,
                {
                    "external_id": f"tokenmaxer:{key}",
                    "display_name": username,
                    "profile_url": (
                        f"{settings.TOKENMAXER_PUBLIC_API_BASE}/u/"
                        f"{quote(username, safe='')}"
                    ),
                    "sessions": 0,
                    "grand_total": 0,
                    **{field: 0 for field in TOKEN_FIELDS},
                },
            )
            totals = {
                field: _non_negative_int(raw.get(field))
                for field in TOKEN_FIELDS
            }
            entry["sessions"] += _non_negative_int(raw.get("sessions"))
            entry["grand_total"] += normalized_token_total(source, totals)
            for field, count in totals.items():
                entry[field] += count
    return list(by_username.values())


def fetch_public_tokenmaxer_entries(window):
    """Fetch a normalized public board, with fresh and stale cache fallback."""
    if not settings.TOKENMAXER_FEDERATION_ENABLED:
        return []

    fresh_key = f"community-chat:tokenmaxer:{window}:v1"
    stale_key = f"community-chat:tokenmaxer:{window}:stale:v1"
    cached = cache.get(fresh_key)
    if isinstance(cached, list):
        return cached

    def fetch_source(source):
        response = requests.get(
            f"{settings.TOKENMAXER_PUBLIC_API_BASE}/api/leaderboard",
            params={
                "window": window,
                "metric": "total",
                "source": source,
                "limit": MAX_PUBLIC_ENTRIES,
            },
            headers={"Accept": "application/json"},
            timeout=settings.TOKENMAXER_FEDERATION_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        return source, response.json()

    try:
        with ThreadPoolExecutor(max_workers=len(SOURCES)) as executor:
            payloads = dict(executor.map(fetch_source, sorted(SOURCES)))
    except (requests.RequestException, ValueError, TypeError) as exc:
        logger.warning("Tokenmaxer federation unavailable: %s", exc)
        stale = cache.get(stale_key)
        return stale if isinstance(stale, list) else []

    entries = _fold_source_payloads(payloads)
    ttl = max(int(settings.TOKENMAXER_FEDERATION_CACHE_SECONDS), 1)
    cache.set(fresh_key, entries, timeout=ttl)
    cache.set(stale_key, entries, timeout=max(ttl * 12, 3600))
    return entries
