"""Bounded retry for short transactions that PostgreSQL aborted as a deadlock victim."""

import logging
import random
import time

from django.db import OperationalError, transaction

logger = logging.getLogger(__name__)

TRANSACTION_RETRY_ATTEMPTS = 3
RETRYABLE_SQLSTATES = frozenset({"40P01", "40001"})


def _sqlstate(exc: OperationalError):
    cause = getattr(exc, "__cause__", None)
    return (
        getattr(exc, "pgcode", None)
        or getattr(exc, "sqlstate", None)
        or getattr(cause, "pgcode", None)
        or getattr(cause, "sqlstate", None)
    )


def retryable_transaction_error(exc: OperationalError) -> bool:
    """Return true only for PostgreSQL deadlock/serialization aborts."""
    return _sqlstate(exc) in RETRYABLE_SQLSTATES


def run_in_retryable_transaction(operation, *, attempts=TRANSACTION_RETRY_ATTEMPTS, label="transaction"):
    """Run ``operation`` in its own transaction, repeating it after a deadlock.

    PostgreSQL rolls the whole aborted transaction back, so a fresh attempt is
    safe only when ``operation`` derives every value from its inputs again and
    leaves provider dispatch until after this returns. Inside a caller's
    transaction the abort belongs to that caller, so it runs exactly once.
    """
    if transaction.get_connection().in_atomic_block:
        with transaction.atomic():
            return operation()
    for attempt in range(attempts):
        try:
            with transaction.atomic():
                return operation()
        except OperationalError as exc:
            if not retryable_transaction_error(exc) or attempt + 1 >= attempts:
                raise
            logger.warning(
                "retrying_aborted_transaction label=%s attempt=%s sqlstate=%s",
                label, attempt + 1, _sqlstate(exc),
            )
            time.sleep(0.05 * (2 ** attempt) + random.uniform(0, 0.05))
    raise AssertionError("unreachable")
