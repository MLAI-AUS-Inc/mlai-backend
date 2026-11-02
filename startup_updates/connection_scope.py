"""Match cached input provenance to the exact connections selected for a run."""


def metric_in_scope(metric, pins, *, run_id=None):
    """Keep legacy reads compatible; new runs require matching provider provenance."""
    if pins is None:
        return True
    provider = (
        "stripe" if metric.source_provider == "financial" else metric.source_provider
    )
    if provider in {"founder", "manual_documents"}:
        return True
    if (
        provider not in {"xero", "stripe", "bank_feed"}
        and run_id is not None
        and getattr(metric, "run_id", None) == run_id
    ):
        return True
    expected = pins.get(provider)
    if expected is None:
        return False
    metadata = metric.source_metadata or {}
    ids = metadata.get("connection_ids")
    if ids is None:
        value = metadata.get("connection_id") or metadata.get(
            f"{provider}_connection_id"
        )
        ids = [value] if value is not None else []
    return bool(ids) and all(str(value) == str(expected) for value in ids)
