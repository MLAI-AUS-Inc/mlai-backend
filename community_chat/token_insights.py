"""Strict, aggregate-only Antiburn reports. No transcript or free-text fields."""

import math
import re
from datetime import datetime, timedelta, timezone as utc_timezone
from zoneinfo import ZoneInfo

WINDOWS = ("today", "7d", "30d", "all")
DETECTORS = frozenset((
    "sessions_over_depth", "unused_mcp_servers", "model_overthinking",
    "overpowered_subagents", "unused_built_in_tools", "unused_skills",
    "old_model_usage", "overuse_of_fast_mode", "cache_churn",
))
REPORT_KEYS = frozenset((
    "status", "engineRevision", "window", "assessedSessions", "totalSessions",
    "computedAt", "finding",
))
FINDING_KEYS = frozenset((
    "detector", "affectedSessions", "estimatedTokenBurnBasisPoints",
    "estimatedSavingsUsd",
))


def _integer(value, minimum=0, maximum=1_000_000):
    return type(value) is int and minimum <= value <= maximum


def _timestamp(value):
    if not isinstance(value, str) or len(value) > 40:
        raise ValueError("Invalid report timestamp.")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("Invalid report timestamp.") from exc
    if parsed.tzinfo is None:
        raise ValueError("Report timestamps must include a timezone.")
    return parsed


def validate_report(report, *, now):
    """Reject unknown fields and implausible evidence before any persistence."""
    if not isinstance(report, dict) or set(report) != REPORT_KEYS:
        raise ValueError("Only aggregate report fields are accepted.")
    if report["status"] not in ("ready", "no-data") or report["window"] not in WINDOWS:
        raise ValueError("Invalid report status or window.")
    revision = report["engineRevision"]
    if not isinstance(revision, str) or not re.fullmatch(r"[a-f0-9]{40}", revision):
        raise ValueError("Invalid analyzer revision.")
    computed = _timestamp(report["computedAt"])
    if not now - timedelta(minutes=10) <= computed <= now + timedelta(seconds=60):
        raise ValueError("The report is not fresh.")
    assessed, total = report["assessedSessions"], report["totalSessions"]
    if not _integer(assessed) or not _integer(total) or assessed > total:
        raise ValueError("Invalid session counts.")
    finding = report["finding"]
    if report["status"] == "no-data" and (assessed != 0 or finding is not None):
        raise ValueError("An empty report cannot contain a finding.")
    if finding is not None:
        if not isinstance(finding, dict) or set(finding) != FINDING_KEYS:
            raise ValueError("Only aggregate finding fields are accepted.")
        if finding["detector"] not in DETECTORS:
            raise ValueError("Unknown detector.")
        if not _integer(finding["affectedSessions"], 1, assessed):
            raise ValueError("Invalid affected session count.")
        burn = finding["estimatedTokenBurnBasisPoints"]
        if burn is not None and not _integer(burn, 0, 10_000):
            raise ValueError("Invalid token estimate.")
        savings = finding["estimatedSavingsUsd"]
        if savings is not None and (
            type(savings) not in (int, float) or not math.isfinite(savings)
            or not 0 <= savings <= 1_000_000
        ):
            raise ValueError("Invalid savings estimate.")
    return {**report, "computedAt": computed.astimezone(utc_timezone.utc).isoformat()}


def current_report(reports, window, timezone, *, now):
    """Never show yesterday's today report, or findings older than one day."""
    stored = reports.get(window) if isinstance(reports, dict) else None
    if not isinstance(stored, dict) or stored.get("timezone") != timezone:
        return None
    report = stored.get("report")
    if not isinstance(report, dict) or report.get("window") != window:
        return None
    try:
        computed = _timestamp(report.get("computedAt"))
    except ValueError:
        return None
    if not now - timedelta(days=1) <= computed <= now + timedelta(seconds=60):
        return None
    zone = ZoneInfo(timezone)
    if window == "today" and computed.astimezone(zone).date() != now.astimezone(zone).date():
        return None
    return report
