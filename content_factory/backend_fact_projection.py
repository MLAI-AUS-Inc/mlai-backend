"""Private permission for the single Run-to-Job compatibility projector."""
from contextlib import contextmanager
from contextvars import ContextVar

_projecting = ContextVar("run_backend_fact_projection", default=False)


def projection_in_progress():
    """Whether this synchronous context is writing compatibility output."""
    return _projecting.get() is True


@contextmanager
def backend_fact_projection():
    """Allow output persistence only while the canonical Run transaction owns it."""
    token = _projecting.set(True)
    try:
        yield
    finally:
        _projecting.reset(token)
