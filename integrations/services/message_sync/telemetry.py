"""Bounded, content-free throughput counters; never part of correctness."""
import hashlib
import os
from collections import defaultdict
from queue import Empty, Queue
from threading import Lock, Thread
from time import time

from django.core.cache import cache

METHODS = frozenset({
    "conversations.history", "conversations.replies", "conversations.info",
    "conversations.members", "conversations.mark", "users.conversations",
    "users.info", "users.list", "apps.event.authorizations.list",
})
COUNTERS = ("admitted", "deferred", "rate_limited", "failed", "finished", "request_ms")
RETENTION_SECONDS = 1200
HOURLY_RETENTION_SECONDS = 7 * 86400
MAX_PENDING_COUNTERS = 2048
MAX_BATCH_COUNTERS = 128
_pid = os.getpid()
_startup_lock = Lock()
_queue = None
_worker_thread = None


def scope_key(app_id, workspace_id, method):
    """Use an opaque scope digest, excluding tokens, account IDs and content."""
    return hashlib.sha256(f"{app_id}:{workspace_id}:{method}".encode()).hexdigest()[:24]


def _key(scope, minute, counter):
    return f"message-sync:throughput:v1:{scope}:{minute}:{counter}"


def _hour_key(scope, hour, counter):
    return f"message-sync:throughput-hour:v1:{scope}:{hour}:{counter}"


def _reset_after_fork():
    """A child must never acquire an inherited lock or use a vanished thread."""
    global _pid, _startup_lock, _queue, _worker_thread
    _pid, _startup_lock = os.getpid(), Lock()
    _queue, _worker_thread = None, None


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_after_fork)


def _write_batch(deltas):
    """Coalesce a bounded batch; only the daemon performs cache I/O."""
    totals = defaultdict(int)
    for scope, counter, amount, minute, hour in deltas:
        totals[(_key(scope, minute, counter), RETENTION_SECONDS)] += amount
        totals[(_hour_key(scope, hour, counter), HOURLY_RETENTION_SECONDS)] += amount
    for (key, retention), amount in totals.items():
        cache.add(key, 0, timeout=retention)
        cache.incr(key, amount)


def _drain(queue):
    """Drain without retries; a stalled cache cannot stall a provider caller."""
    while True:
        first = queue.get()
        if first is None:
            queue.task_done()
            return
        batch, stopping = [first], False
        for _ in range(MAX_BATCH_COUNTERS - 1):
            try:
                delta = queue.get_nowait()
            except Empty:
                break
            if delta is None:  # Allows deterministic shutdown in protocol tests.
                queue.task_done()
                stopping = True
                break
            batch.append(delta)
        try:
            _write_batch(batch)
        except Exception:
            # Best-effort counters may be lost; correctness never depends on them.
            pass
        finally:
            for _ in batch:
                queue.task_done()
        if stopping:
            return


def _get_queue():
    global _queue, _worker_thread
    if _pid != os.getpid():
        _reset_after_fork()
    if _queue is not None:
        return _queue
    # A concurrent first caller may drop a counter rather than wait for another
    # caller to bootstrap the daemon. Telemetry never deserves a request stall.
    lock = _startup_lock
    if not lock.acquire(blocking=False):
        return None
    try:
        if _queue is None:
            queue = Queue(maxsize=MAX_PENDING_COUNTERS)
            worker = Thread(target=_drain, args=(queue,), name="message-sync-telemetry", daemon=True)
            worker.start()
            _queue, _worker_thread = queue, worker
    finally:
        lock.release()
    return _queue


def _enqueue(delta):
    queue = _get_queue()
    if queue is not None:
        queue.put_nowait(delta)


def record(scope, counter, amount=1):
    """Queue a bounded best-effort increment, with no cache I/O or waiting."""
    if counter not in COUNTERS:
        raise ValueError("Unknown throughput counter")
    try:
        now = time()
        _enqueue((scope, counter, max(0, int(amount)), int(now // 60), int(now // 3600)))
    except Exception:
        # Full queue, fork/startup failure or shutdown must not affect messages.
        pass


def snapshot(scopes, *, minutes=5, now=None):
    """Read completed minute buckets; missing counters are not measured zeroes."""
    if not 1 <= minutes <= 15:
        raise ValueError("Throughput window must be 1–15 minutes")
    end = int((time() if now is None else now) // 60)
    keys = {(scope, minute, counter): _key(scope, minute, counter)
            for scope in scopes for minute in range(end - minutes, end) for counter in COUNTERS}
    values = cache.get_many(list(keys.values()))
    result, observed = {}, {}
    for scope in scopes:
        selected = {item: values[key] for item, key in keys.items() if item[0] == scope and key in values}
        observed[scope] = len({minute for _, minute, _ in selected})
        result[scope] = None if not selected else {
            counter: sum(value for (_, _, name), value in selected.items() if name == counter)
            for counter in COUNTERS
        }
    return {"window_start": (end - minutes) * 60, "window_end": end * 60,
            "bucket_seconds": 60, "observed_minutes": observed, "scopes": result}


def hourly_snapshot(scopes, *, hours=24, now=None):
    """Read up to seven days without retaining high-cardinality minute keys.

    Missing buckets remain unknown. Report the number of observed hours so a
    fresh deployment or cache eviction cannot masquerade as a quiet full day.
    """
    if not 1 <= hours <= 168:
        raise ValueError("Throughput window must be 1–168 hours")
    end = int((time() if now is None else now) // 3600)
    keys = {(scope, hour, counter): _hour_key(scope, hour, counter)
            for scope in scopes for hour in range(end - hours, end) for counter in COUNTERS}
    values = cache.get_many(list(keys.values()))
    result, observed = {}, {}
    for scope in scopes:
        selected = {item: values[key] for item, key in keys.items() if item[0] == scope and key in values}
        observed[scope] = len({hour for _, hour, _ in selected})
        result[scope] = None if not selected else {
            counter: sum(value for (_, _, name), value in selected.items() if name == counter)
            for counter in COUNTERS
        }
    return {"window_start": (end - hours) * 3600, "window_end": end * 3600,
            "bucket_seconds": 3600, "observed_hours": observed, "scopes": result}
