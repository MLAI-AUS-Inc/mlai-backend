"""Idempotency-Key records for My Startup writes (IF-P2-18 server).

A write that honours ``Idempotency-Key`` calls :func:`begin` before any work and
:func:`finish` with the response it is about to send. Records live in the cache
for 24 hours, under a key derived from the caller's scope and the client key,
so the same key under two scopes never collides. The in-flight marker is taken
with ``cache.add``, so of two concurrent requests with one key only one does
the work; the other gets ``in_flight`` and answers 409.

Scopes in use: ``("profile_save", user_id)`` (be-runs),
``("staff_action", organization_id, staff_user_id)`` (be-staff) and
``("publish_reviewed", source_run_id, user_id)`` (be-publish).

This module has no flag of its own: each caller gates its own use.
"""
from __future__ import annotations

import hashlib
import json
import re

from django.core.cache import cache

TTL_SECONDS = 24 * 60 * 60
KEY_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,128}")
CACHE_PREFIX = "founder_tools:idem:"
IN_PROGRESS = "in_progress"
DONE = "done"

NEW = "new"
REPLAY = "replay"
CONFLICT = "conflict"
IN_FLIGHT = "in_flight"

CONFLICT_DETAIL = "Couldn't save just now. Trying again…"
CONFLICT_REASON = "operation_key_conflict"
REPLAY_HEADER = "Idempotent-Replayed"


def valid_key(key) -> bool:
    """1 to 128 characters of ``[A-Za-z0-9_-]``."""
    return isinstance(key, str) and KEY_PATTERN.fullmatch(key) is not None


def request_sha256(data) -> str:
    """A stable SHA-256 of a JSON-like request body, for comparing a resend with the original."""
    encoded = json.dumps(data, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def cache_key(scope: tuple, key: str) -> str:
    """``founder_tools:idem:<sha256 of the scope and the key>``."""
    material = json.dumps([[str(part) for part in scope], key], separators=(",", ":")).encode("utf-8")
    return CACHE_PREFIX + hashlib.sha256(material).hexdigest()


def _checked(scope, key) -> str:
    if not isinstance(scope, tuple) or not scope:
        raise ValueError("An idempotency scope must be a non-empty tuple.")
    if not valid_key(key):
        raise ValueError("An Idempotency-Key must be 1 to 128 letters, digits, '-' or '_'.")
    return cache_key(scope, key)


def begin(scope: tuple, key: str, request_sha256: str) -> tuple[str, dict | None]:
    """Claim ``key`` in ``scope`` for this request, or say why not.

    Returns ``("new", None)`` when this request should do the work,
    ``("replay", stored)`` when the same body already finished (``stored`` is
    ``{"status", "body"}``), ``("conflict", None)`` when the key was used with a
    different body, and ``("in_flight", None)`` while the first request with
    this body is still running. Raises ``ValueError`` for an invalid key.
    """
    record_key = _checked(scope, key)
    if cache.add(record_key, {"state": IN_PROGRESS, "request_sha256": request_sha256}, TTL_SECONDS):
        return NEW, None
    record = cache.get(record_key)
    if not isinstance(record, dict):
        # The record expired between the add and the read: claim it again once.
        if cache.add(record_key, {"state": IN_PROGRESS, "request_sha256": request_sha256}, TTL_SECONDS):
            return NEW, None
        record = cache.get(record_key)
        if not isinstance(record, dict):
            return IN_FLIGHT, None
    if record.get("request_sha256") != request_sha256:
        return CONFLICT, None
    if record.get("state") == DONE:
        return REPLAY, {"status": record.get("status"), "body": record.get("body")}
    return IN_FLIGHT, None


def finish(scope: tuple, key: str, request_sha256: str, *, status: int, body: dict) -> None:
    """Store the response for ``key`` so a resend with the same body replays it.

    A record that belongs to a different body is left alone.
    """
    record_key = _checked(scope, key)
    record = cache.get(record_key)
    if isinstance(record, dict) and record.get("request_sha256") not in (None, request_sha256):
        return
    cache.set(record_key, {"state": DONE, "request_sha256": request_sha256, "status": int(status), "body": body},
              TTL_SECONDS)


def abandon(scope: tuple, key: str, request_sha256: str) -> None:
    """Release an in-flight claim after a failure, so a resend can do the work again."""
    record_key = _checked(scope, key)
    record = cache.get(record_key)
    if isinstance(record, dict) and record.get("state") == IN_PROGRESS and record.get("request_sha256") == request_sha256:
        cache.delete(record_key)
