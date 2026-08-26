"""Ingest helpers for the community token leaderboard.

Implements the wire protocol of the published ``tokenmaxer`` npm reporter so
the unmodified package can report to MLAI. The reporter posts summed token
counts per (session, model) and expects ``{"accepted": int, "rejected": [...]}``
back; any non-2xx is treated as a whole-batch failure and retried later.

Counts are self-reported and stay that way — this is a community board, not a
contest. The validation below exists for correctness, not to police cheating:
it keeps unbounded magnitudes out of the aggregates, keeps rows inside a sane
time range so a mis-stamped row cannot pin a window open forever, and keeps
filesystem paths out of ``session_id``.
"""

import math
import re
from datetime import datetime, timezone as datetime_timezone
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .models import (
    TokenUsageAccount,
    TokenUsageDailyBucket,
    TokenUsageSession,
)


SOURCES = frozenset(
    {"claude_code", "codex", "opencode", "pi", "cursor"}
)

# The reporter chunks at 200 (live) and 500 (backfill); these ceilings match
# upstream's so a legitimate batch is never refused.
MAX_INGEST_SESSIONS = 500
MAX_HISTORY_SESSIONS = 5000

MAX_SESSION_ID_LEN = 200
MAX_MODEL_LEN = 128

# ~25x the largest genuine session observed upstream (a 16-day Codex run at
# 2.13B input tokens). High enough never to reject real usage, low enough that
# aggregates stay meaningful.
MAX_TOKENS_PER_CATEGORY = 50_000_000_000

# Bounds on client-supplied started_at. Without an upper bound a row dated in
# the future satisfies every window at once and never rolls off "today".
CLOCK_SKEW_MS = 5 * 60 * 1000
EPOCH_FLOOR_MS = 1_672_531_200_000  # 2023-01-01T00:00:00Z

# Opaque-id shape. Claude Code and Codex emit UUIDs; the Cursor leg emits
# "cursor-YYYY-MM-DD". A path-derived id (which upstream can produce in an
# unusual transcript layout) is rejected rather than stored, so member project
# and client directory names never reach our database.
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")

TOKEN_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_creation_tokens",
    "reasoning_tokens",
)


class IngestError(ValueError):
    """The request is malformed as a whole and no row can be salvaged."""


def _is_synthetic_model(model):
    """Claude Code emits ``<synthetic>`` turns that are not real usage."""
    return model.strip().strip("<>").lower() == "synthetic"


def _coerce_count(value):
    """Non-numeric, negative, NaN and infinite values all read as zero."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return 0
        value = math.floor(value)
    return value if value > 0 else 0


def _parse_started_at(value, now_ms):
    """Epoch milliseconds from the reporter to an aware datetime."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None, "started_at must be epoch milliseconds"
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None, "started_at must be epoch milliseconds"
        value = math.floor(value)
    if value < EPOCH_FLOOR_MS:
        return None, "started_at is implausibly old"
    if value > now_ms + CLOCK_SKEW_MS:
        return None, "started_at is in the future"
    return (
        datetime.fromtimestamp(value / 1000.0, tz=datetime_timezone.utc),
        None,
    )


def _parse_row(raw, now_ms):
    """Validate one session entry. Returns ``(row, error)``."""
    if not isinstance(raw, dict):
        return None, "each session must be an object"

    session_id = raw.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        return None, "session_id is required"
    if len(session_id) > MAX_SESSION_ID_LEN:
        return None, "session_id too long"
    if not SESSION_ID_RE.match(session_id):
        return None, "session_id must be an opaque id"

    model = raw.get("model")
    if not isinstance(model, str) or not model:
        return None, "model is required"
    if len(model) > MAX_MODEL_LEN:
        return None, "model too long"

    started_at, error = _parse_started_at(raw.get("started_at"), now_ms)
    if error is not None:
        return None, error

    row = {
        "session_id": session_id,
        "model": model,
        "started_at": started_at,
    }
    for field in TOKEN_FIELDS:
        count = _coerce_count(raw.get(field))
        if count > MAX_TOKENS_PER_CATEGORY:
            return None, "token count exceeds the accepted range"
        row[field] = count
    return row, None


def parse_sessions(body, max_sessions):
    """Parse a reporter payload.

    Returns ``(source, rows, rejected)``. Structurally broken rows are
    reported individually by their index rather than failing the batch, so one
    bad row never blocks a member's whole report. Raises :class:`IngestError`
    only when nothing in the request can be used.
    """
    if not isinstance(body, dict):
        raise IngestError("body must be a JSON object")

    source = body.get("source")
    if source not in SOURCES:
        raise IngestError(
            "source must be one of: " + ", ".join(sorted(SOURCES))
        )

    sessions = body.get("sessions")
    if not isinstance(sessions, list):
        raise IngestError("sessions must be an array")
    if not sessions:
        raise IngestError("sessions must not be empty")
    if len(sessions) > max_sessions:
        raise IngestError(f"too many sessions (max {max_sessions})")

    now_ms = int(datetime.now(tz=datetime_timezone.utc).timestamp() * 1000)
    rows = []
    rejected = []
    seen = set()
    for index, raw in enumerate(sessions):
        row, error = _parse_row(raw, now_ms)
        if error is not None:
            rejected.append({"index": index, "error": error})
            continue
        if _is_synthetic_model(row["model"]):
            continue
        # A single batch carrying the same (session, model) twice would make
        # the upsert ambiguous; last write wins, matching the replace
        # semantics the reporter already relies on.
        key = (row["session_id"], row["model"])
        if key in seen:
            rows = [
                existing
                for existing in rows
                if (existing["session_id"], existing["model"]) != key
            ]
        seen.add(key)
        rows.append(row)

    return source, rows, rejected


def local_usage_date(at=None):
    """Return the configured leaderboard calendar date for an instant."""
    instant = at or timezone.now()
    return instant.astimezone(ZoneInfo(settings.TOKEN_USAGE_TIME_ZONE)).date()


def _session_key(row):
    return row["session_id"], row["model"]


@transaction.atomic
def upsert_sessions(
    account,
    source,
    rows,
    *,
    reported_at=None,
    attribute_daily=True,
):
    """Persist cumulative snapshots and, for live reports, positive deltas.

    The upstream reporter sends cumulative session totals. The latest
    cumulative snapshot powers the all-time board; only positive growth since
    that snapshot is added to today's Melbourne-calendar bucket. Locking the
    account serializes overlapping hooks from the same member and keeps both
    writes idempotent.

    History backfills pass ``attribute_daily=False`` because old cumulative
    snapshots do not reveal which calendar day their tokens were consumed on.
    They establish the baseline for the next live delta without inventing
    daily history.
    """
    if not rows:
        return 0

    locked_account = TokenUsageAccount.objects.select_for_update().get(pk=account.pk)
    session_ids = [row["session_id"] for row in rows]
    existing = {
        (session.session_id, session.model): session
        for session in TokenUsageSession.objects.filter(
            account=locked_account,
            source=source,
            session_id__in=session_ids,
        )
    }

    observed_at = reported_at or timezone.now()
    usage_date = local_usage_date(observed_at)
    new_snapshots = []
    changed_snapshots = []
    deltas = {}

    for row in rows:
        key = _session_key(row)
        snapshot = existing.get(key)
        previous = {
            field: int(getattr(snapshot, field)) if snapshot is not None else 0
            for field in TOKEN_FIELDS
        }
        delta = {
            field: max(int(row[field]) - previous[field], 0)
            for field in TOKEN_FIELDS
        }
        if attribute_daily and any(delta.values()):
            deltas[key] = delta

        if snapshot is None:
            new_snapshots.append(
                TokenUsageSession(
                    account=locked_account,
                    source=source,
                    **row,
                )
            )
            continue

        # Keep cumulative baselines monotonic. A stale history batch or a
        # reporter reset must not lower the baseline and cause a later hook to
        # credit the same tokens for a second time.
        for field in TOKEN_FIELDS:
            setattr(snapshot, field, max(previous[field], int(row[field])))
        snapshot.started_at = min(snapshot.started_at, row["started_at"])
        snapshot.updated_at = observed_at
        changed_snapshots.append(snapshot)

    if new_snapshots:
        TokenUsageSession.objects.bulk_create(new_snapshots, batch_size=500)
    if changed_snapshots:
        TokenUsageSession.objects.bulk_update(
            changed_snapshots,
            [*TOKEN_FIELDS, "started_at", "updated_at"],
            batch_size=500,
        )

    if deltas:
        bucket_rows = TokenUsageDailyBucket.objects.filter(
            account=locked_account,
            usage_date=usage_date,
            source=source,
            session_id__in=[key[0] for key in deltas],
        )
        buckets = {
            (bucket.session_id, bucket.model): bucket
            for bucket in bucket_rows
        }
        new_buckets = []
        changed_buckets = []
        for key, delta in deltas.items():
            bucket = buckets.get(key)
            if bucket is None:
                new_buckets.append(
                    TokenUsageDailyBucket(
                        account=locked_account,
                        usage_date=usage_date,
                        source=source,
                        session_id=key[0],
                        model=key[1],
                        **delta,
                    )
                )
                continue
            for field in TOKEN_FIELDS:
                setattr(bucket, field, int(getattr(bucket, field)) + delta[field])
            bucket.updated_at = observed_at
            changed_buckets.append(bucket)

        if new_buckets:
            TokenUsageDailyBucket.objects.bulk_create(new_buckets, batch_size=500)
        if changed_buckets:
            TokenUsageDailyBucket.objects.bulk_update(
                changed_buckets,
                [*TOKEN_FIELDS, "updated_at"],
                batch_size=500,
            )

    return len(rows)
