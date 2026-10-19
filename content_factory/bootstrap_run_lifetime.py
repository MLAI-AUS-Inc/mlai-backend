"""Release request-owned ORM presentation graphs after bootstrap serialization."""


def release_projection_runs(runs):
    """Break only bootstrap's prefetch edges, without changing any saved run data.

    Even an empty reverse prefetch has a QuerySet -> known-parent -> QuerySet
    cycle. Large result JSON then survives until cyclic collection rather than
    being released at the end of this request. Feedback's to_attr list can form
    the same cycle through each comment's cached run foreign key.
    """
    for run in runs:
        attributes = getattr(run, "__dict__", {})
        if not isinstance(attributes, dict):
            continue
        prefetched = attributes.get("_prefetched_objects_cache")
        if isinstance(prefetched, dict):
            prefetched.pop("steps", None)
        attributes.pop("_bootstrap_component_comments", None)
