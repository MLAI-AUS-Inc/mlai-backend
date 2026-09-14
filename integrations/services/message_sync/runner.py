"""Bounded always-on synchronization; no user login is needed to populate work."""
import logging
from contextlib import contextmanager
from contextvars import ContextVar

from integrations.models import BridgeSyncState
from .history import public_page, seed_states
from .coverage import failure_code
from .inbox import enabled
from .private_history import private_page
from .scheduler import BudgetDeferred, LeaseLost, claim_job, fail_job, locked_job

logger = logging.getLogger(__name__)
_current_lease = ContextVar("message_sync_job_lease", default=None)


@contextmanager
def job_context(lease):
    token = _current_lease.set(lease)
    try:
        yield
    finally:
        _current_lease.reset(token)


def fence_current_page():
    """Called by existing private persistence inside its consent transaction."""
    lease = _current_lease.get()
    if lease is not None:
        locked_job(lease)


def process_history_once(*, seed=True):
    if not enabled():
        return 0
    if seed:
        seed_states()
    lease = claim_job(kinds=["head", "archive", "thread"])
    if lease is None:
        return 0
    try:
        state = BridgeSyncState.objects.select_related(
            "public_channel", "private_conversation__grant__connection",
        ).get(pk=lease.state_id)
        with job_context(lease):
            if state.private_conversation_id:
                private_page(lease, state)
            else:
                public_page(lease, state)
        return 1
    except LeaseLost:
        return 0
    except Exception as exc:
        try:
            fail_job(lease, error_code=failure_code(exc), retry_after=exc.retry_after if isinstance(exc, BudgetDeferred) else None)
        except LeaseLost:
            pass
        logger.warning("message_sync_page_failed job_id=%s error_code=%s", lease.job_id, failure_code(exc))
        return 0
