"""Why a cancelled run stopped, for the client run payload (IF-20).

The cancellation reason and actor are recorded on ``run.result`` by the
cancel paths. This projection is a stub until that recording lands, so a
cancelled run exposes no extra fields yet.
"""


def cancellation_payload(result) -> dict:
    """Return the camelCase cancellation fields for a run result (none yet)."""
    return {}
