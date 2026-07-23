"""Helpers for reconciling durable Content Factory run state.

Content Factory can resume a run that originally returned a precondition failure.
The active snapshot is authoritative at that point: keeping the old blocker fields
in the result makes the run look both running and terminal to API consumers.
"""

from __future__ import annotations

from typing import Any, Mapping


ACTIVE_RUN_STATUSES = frozenset(
    {"queued", "running", "processing", "pending", "starting", "in_progress"}
)
ARTICLE_WORKFLOWS = frozenset(
    {
        "article_generation",
        "article_revision",
        "confirmed_topic",
        "content_factory_article",
        "direct_generate",
    }
)
RESUMABLE_TERMINAL_STATUSES = frozenset(
    {
        "blocked",
        "denied",
        "failed",
        "fallback_ready",
        "manual_blocked",
        "precondition_failed",
        "preview_failed",
    }
)

_OBSOLETE_ACTIVE_BLOCKER_KEYS = frozenset(
    {
        "blocking_code",
        "blockingCode",
        "blocking_reason",
        "blockingReason",
        "content_factory_dispatch_blocked",
        "error",
        "error_code",
        "errorCode",
        "errors",
        "model_adapter_blocking_code",
        "modelAdapterBlockingCode",
        "model_adapter_blocking_reason",
        "modelAdapterBlockingReason",
        "next_action",
        "nextAction",
        "next_required_step",
        "nextRequiredStep",
        "repair_status",
        "repairStatus",
        "requires_user_action",
        "requiresUserAction",
        "retry_available",
        "retryAvailable",
        "retryable",
    }
)
_NESTED_CONTROL_STATE_KEYS = (
    "content_factory_response",
    "contentFactoryResponse",
    "latest_control_response",
    "latestControlResponse",
    "result",
)
_READINESS_STATE_KEYS = (
    "article_system_readiness",
    "articleSystemReadiness",
)


def _normalized_status(value: Any) -> str:
    return str(value or "").strip().lower()


def _mapping_has_obsolete_blocker(value: Mapping[str, Any]) -> bool:
    if any(value.get(key) not in (None, "", [], {}) for key in _OBSOLETE_ACTIVE_BLOCKER_KEYS):
        return True
    return _normalized_status(value.get("status")) in RESUMABLE_TERMINAL_STATUSES


def clear_obsolete_active_run_blockers(
    value: Any,
    *,
    active_status: str = "running",
    current_step: str = "",
) -> dict:
    """Return an active result snapshot without stale terminal/precondition state.

    This deliberately leaves diagnostics and step-level failures alone. They are
    useful evidence for self-healing. Only fields interpreted as the *current* run
    blocker are removed.
    """

    if not isinstance(value, dict):
        return {}
    cleaned = dict(value)
    for key in _OBSOLETE_ACTIVE_BLOCKER_KEYS:
        cleaned.pop(key, None)

    normalized_active_status = _normalized_status(active_status) or "running"
    if _normalized_status(cleaned.get("status")) in RESUMABLE_TERMINAL_STATUSES:
        cleaned["status"] = normalized_active_status
    if current_step:
        if "current_step" in cleaned:
            cleaned["current_step"] = current_step
        if "currentStep" in cleaned:
            cleaned["currentStep"] = current_step

    for key in _NESTED_CONTROL_STATE_KEYS:
        nested = cleaned.get(key)
        if isinstance(nested, dict):
            cleaned[key] = clear_obsolete_active_run_blockers(
                nested,
                active_status=normalized_active_status,
                current_step=current_step,
            )

    # Readiness blocks describe why dispatch stopped before orchestration. Once
    # that same run is active they are stale by definition. Drop only blocked
    # readiness payloads; successful readiness/proof metadata remains available.
    for key in _READINESS_STATE_KEYS:
        readiness = cleaned.get(key)
        if isinstance(readiness, dict) and _mapping_has_obsolete_blocker(readiness):
            cleaned.pop(key, None)
    return cleaned


def active_retry_signal(*payloads: Any) -> bool:
    """Whether a snapshot carries Content Factory's exact article-recovery contract."""

    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        if payload.get("precondition_recovered") is True:
            return True
        recovery = payload.get("recovery")
        if isinstance(recovery, dict):
            recovery_status = _normalized_status(recovery.get("status"))
            if recovery_status in {"recovered", "resume_queued", "resumed"}:
                return True
        for key in ("result", "latest_control_response", "latestControlResponse"):
            nested = payload.get(key)
            if isinstance(nested, dict) and active_retry_signal(nested):
                return True
    return False
