"""Reserve an independent operation for a reviewed article revision."""

from .website_connections import authority_guard, owner_operation_scope, scoped_run_contract
from .website_contract import connection_contract
from .website_operations import OPERATION_FIELDS, reserve_workflow_operation


def reserve_revision_operation(source, payload):
    """Retain source consent while fencing the child independently of its parent.

    A completed or failed source remains readable. Its terminal operation must
    never be reused for a queued revision, or changed to revive the parent.
    The feedback batch keys retries; the operation also namespaces the physical
    child so a legacy, partially initialized child cannot be acknowledged.
    """
    original = scoped_run_contract(source)
    binding = connection_contract(original)
    if not binding:
        return None
    with authority_guard(original, action="read") as website:
        child = {**payload, **binding, "domain": source.domain, "github_repo": source.github_repo}
        if original.get("expected_source_sha"):
            child["expected_source_sha"] = original["expected_source_sha"]
        for key in OPERATION_FIELDS:
            child.pop(key, None)
        child["client_request_id"] = f"component-revision:{source.run_id}:{payload['feedback_batch_id']}"
    # Reservation extends only this child's context, preserving the caller's
    # source guard for its subsequent feedback/history writes.
    with owner_operation_scope(child):
        operation = reserve_workflow_operation(website, workflow="article_revision", payload=child)
    child["requested_run_id"] = f"{payload['requested_run_id']}-{operation.pk.hex[:12]}"
    payload.update(child)
    return operation
