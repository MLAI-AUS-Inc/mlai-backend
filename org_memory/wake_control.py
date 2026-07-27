from contextlib import contextmanager
from contextvars import ContextVar


_artifact_wakes_suppressed = ContextVar(
    "org_memory_artifact_wakes_suppressed",
    default=False,
)


def artifact_wakes_suppressed() -> bool:
    return bool(_artifact_wakes_suppressed.get())


@contextmanager
def suppress_artifact_wakes():
    token = _artifact_wakes_suppressed.set(True)
    try:
        yield
    finally:
        _artifact_wakes_suppressed.reset(token)
