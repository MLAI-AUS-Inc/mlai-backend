"""Run status lifecycle, generated from contracts/run-statuses.json (version 4).

Do not edit by hand. Change the contract in both repos, then run
``python scripts/generate_run_status_contract.py``.
CI runs it with ``--check`` and fails when this file is stale.

ACTIVE: the worker owns the run's progress.
AWAITING: paused until the owner acts.
TERMINAL: stopped; only an explicit resume or recovery transition reopens it.
"""
from types import MappingProxyType

CONTRACT_VERSION = 4

RUN_STATUSES = frozenset({
    "awaiting_approval",
    "awaiting_confirmation",
    "awaiting_delivery_mode",
    "blocked",
    "cancelled",
    "completed",
    "denied",
    "failed",
    "queued",
    "running",
})
PLATFORM_EXTENSIONS = frozenset({
    "approval_required",
})
EXTERNAL_LABELS = MappingProxyType({
    "queued": "queued",
    "running": "processing",
    "completed": "completed",
    "failed": "failed",
    "blocked": "blocked_verification",
    "cancelled": "cancelled",
    "awaiting_delivery_mode": "awaiting_delivery_mode",
    "awaiting_confirmation": "awaiting_confirmation",
    "awaiting_approval": "approval_required",
    "denied": "denied",
})

ACTIVE = frozenset({
    "queued",
    "running",
})
AWAITING = frozenset({
    "approval_required",
    "awaiting_approval",
    "awaiting_confirmation",
    "awaiting_delivery_mode",
})
TERMINAL = frozenset({
    "blocked",
    "cancelled",
    "completed",
    "denied",
    "failed",
})


def _value(status) -> str:
    return str(getattr(status, "value", status) or "").strip().lower()


def is_active(status) -> bool:
    """True while the worker owns the run's progress."""
    return _value(status) in ACTIVE


def is_awaiting(status) -> bool:
    """True while the run waits for the owner to act."""
    return _value(status) in AWAITING


def is_terminal(status) -> bool:
    """True once the run has stopped; only a resume or recovery reopens it."""
    return _value(status) in TERMINAL
