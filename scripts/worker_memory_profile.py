"""Opt-in bounded process metrics for an evidence-based worker recycle limit."""

import json
import os
from pathlib import Path
import re

_STATE = "_mlai_memory_profile"
_MAX_STATUS_BYTES = 16_384


def parse_process_status(value):
    """Read only Linux RSS/high-water fields, preserving unavailable as None."""
    if not isinstance(value, str) or len(value) > _MAX_STATUS_BYTES:
        return None
    result = {}
    for field, name in (("VmRSS", "rss_kib"), ("VmHWM", "peak_rss_kib")):
        matches = re.findall(r"^" + field + r":[ \t]+([0-9]{1,12})[ \t]+kB[ \t]*$", value, re.M)
        if len(matches) != 1:
            return None
        result[name] = int(matches[0])
    return result


def read_process_memory():
    """Read this worker's fixed procfs status; never inspect requests or peers."""
    try:
        with Path("/proc/self/status").open("r", encoding="ascii") as handle:
            return parse_process_status(handle.read(_MAX_STATUS_BYTES + 1))
    except (OSError, UnicodeError):
        return None


def _log(worker, message, payload=None):
    try:
        if payload is None:
            worker.log.info(message)
        else:
            worker.log.info(message + " %s", json.dumps(payload, sort_keys=True, separators=(",", ":")))
    except Exception:
        # Optional telemetry must never fail startup, a request or shutdown.
        pass


def _sample(worker, state, phase):
    try:
        sample = read_process_memory()
    except Exception:
        sample = None
    if sample is None:
        if not state["unavailable_reported"]:
            _log(worker, "worker_memory_profile_unavailable")
            state["unavailable_reported"] = True
        return
    if state["baseline"] is None:
        state["baseline"] = {**sample, "completed_requests": state["requests"]}
    baseline = state["baseline"]
    _log(worker, "worker_memory_profile", {"schema_version": 1, "phase": phase,
        "worker_pid": os.getpid(),
        "completed_requests": state["requests"], "baseline_completed_requests": baseline["completed_requests"],
        "rss_kib": sample["rss_kib"], "peak_rss_kib": sample["peak_rss_kib"],
        "rss_delta_kib": sample["rss_kib"] - baseline["rss_kib"]})


def start_worker_profile(worker):
    """Capture the baseline after route warming only when explicitly enabled."""
    if str(os.environ.get("GUNICORN_MEMORY_PROFILE_ENABLED", "false")).strip().lower() not in {"1", "true", "yes", "on"}:
        setattr(worker, _STATE, None)
        return
    raw = os.environ.get("GUNICORN_MEMORY_PROFILE_INTERVAL", "100")
    try:
        interval = int(raw)
        if not 1 <= interval <= 100_000:
            interval = 100
    except (ValueError, TypeError):
        interval = 100
    state = {"requests": 0, "interval": interval, "baseline": None, "unavailable_reported": False}
    setattr(worker, _STATE, state)
    _sample(worker, state, "start")


def record_worker_request(worker):
    """Count a completed request and sample only at the configured interval."""
    state = getattr(worker, _STATE, None)
    if not isinstance(state, dict):
        return
    state["requests"] += 1
    if state["requests"] % state["interval"] == 0:
        _sample(worker, state, "sample")


def finish_worker_profile(worker):
    """Record the exit sample in the worker process, independent of exit cause."""
    state = getattr(worker, _STATE, None)
    if isinstance(state, dict):
        _sample(worker, state, "exit")
        setattr(worker, _STATE, None)
