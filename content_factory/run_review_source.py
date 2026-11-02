"""Backend-only, ticket-bound proof of an independently verified review branch.

A preview branch is a different source from the selected default branch. Only
an admitted review child with this saved proof may use that exact source; the
connection's consent identity and all other workflow checks remain unchanged.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass
from urllib.parse import quote

from django.db import transaction

from .run_ownership import (WIRE_EXECUTION_KEY, WIRE_REQUEST_KEY, current_execution_ticket,
    owner_receipt, require_admission_identity, require_event_execution, wire_identity)
from .website_contract import SHA_PATTERN, WebsiteAuthorityError, connection_contract, evidence_digest

REVIEW_SOURCE_KEY = "review_source_v1"
MAX_REVIEW_PROOFS = 1024
_verified_source = ContextVar("backend_verified_review_source", default=None)


def _denied():
    return WebsiteAuthorityError("review_source_changed", "Reload and verify this exact reviewed source before continuing.")


def _same(left, right):
    return evidence_digest(left) == evidence_digest(right)


def _connection_identity(connection):
    return (str(connection.pk), connection.organization_id, connection.generation,
        connection.repository_id, connection.github_repo, connection.installation_id,
        connection.configuration_version, connection.state, connection.authorized_by_id)


def _valid_ref(value):
    return (type(value) is str and 0 < len(value) <= 255 and not value.startswith(("/", ".", "-"))
        and not value.endswith(("/", ".", ".lock")) and ".." not in value and "@{" not in value
        and all(ord(char) >= 33 and ord(char) != 127 and char not in "~^:?*[\\" for char in value)
        and all(part and not part.startswith(".") and not part.endswith(".lock") for part in value.split("/")))


def verify_review_ref(connection, source_ref, source_sha):
    """Read only the exact bound repository ref, outside every SQL lock."""
    from .website_connections import require_unlocked_remote_call
    require_unlocked_remote_call()
    if not _valid_ref(source_ref) or type(source_sha) is not str or not SHA_PATTERN.fullmatch(source_sha):
        raise _denied()
    try:
        from integrations.services.github_connections import read_bound_repository
        response = read_bound_repository(connection,
            f"https://api.github.com/repos/{connection.github_repo}/branches/{quote(source_ref, safe='')}")
        response.raise_for_status()
        data = response.json()
    except Exception as exc:
        raise WebsiteAuthorityError("review_source_unavailable", "The reviewed branch could not be verified. Retry when GitHub is available.", retryable=True) from exc
    if (not isinstance(data, dict) or data.get("name") != source_ref
            or not isinstance(data.get("commit"), dict) or data["commit"].get("sha") != source_sha):
        raise _denied()


@dataclass(frozen=True)
class VerifiedReviewSource:
    """Short-lived backend provider result; never accepted from an HTTP body."""
    connection_identity: tuple
    parent_run_id: str
    parent_admission_digest: str
    reviewed_revision: str
    reviewed_source_ref: str
    expected_source_sha: str


def verify_review_snapshot(parent, snapshot, *, expected_revision):
    """Bind a trusted current worker snapshot to a fresh independent GitHub read."""
    from .website_connections import authority_guard, scoped_run_contract
    if (owner_receipt(parent) is None or not isinstance(snapshot, dict)
            or type(expected_revision) is not str or snapshot.get("revision") != expected_revision
            or not _valid_ref(snapshot.get("sourceRef")) or type(snapshot.get("baseCommit")) is not str
            or not SHA_PATTERN.fullmatch(snapshot["baseCommit"])):
        raise _denied()
    with authority_guard(scoped_run_contract(parent), action="read") as connection:
        identity = _connection_identity(connection)
    verify_review_ref(connection, snapshot["sourceRef"], snapshot["baseCommit"])
    return VerifiedReviewSource(identity, parent.run_id, evidence_digest(wire_identity(owner_receipt(parent))),
        expected_revision, snapshot["sourceRef"], snapshot["baseCommit"])


def _proof(run, parent, connection, ticket, *, revision, source_ref, source_sha):
    scope = ticket["scope"]
    return {"schema_version": 1, "organization_id": str(run.organization_id),
        "website_connection_id": str(connection.pk), "connection_generation": connection.generation,
        "repository_id": connection.repository_id, "github_repo": connection.github_repo,
        "consent_branch": connection.branch, "consent_app_root": connection.app_root,
        "configuration_revision": connection.configuration_version,
        "operation_id": scope["operation_id"], "operation_attempt": scope["operation_attempt"],
        "deletion_epoch": scope["deletion_epoch"], "parent_run_id": parent.run_id,
        "parent_admission_digest": evidence_digest(wire_identity(owner_receipt(parent))),
        "child_run_id": run.run_id, "child_admission_digest": evidence_digest(wire_identity(owner_receipt(run))),
        "request_digest": ticket["request_digest"], "execution_id": ticket["execution_id"],
        "generation": ticket["generation"], "ticket_digest": evidence_digest(ticket),
        "reviewed_revision": revision, "reviewed_source_ref": source_ref, "expected_source_sha": source_sha}


def _operation_for(run, connection, ticket, *, lock):
    from .website_models import WebsiteConnectionOperation
    from .website_operations import deletion_epoch
    scope = ticket["scope"]
    query = WebsiteConnectionOperation.objects.select_for_update() if lock else WebsiteConnectionOperation.objects
    operation = query.filter(pk=scope["operation_id"], connection=connection, generation=connection.generation).first()
    if (operation is None or operation.action != "workflow" or operation.state in {"cancelled", "deleted", "denied"}
            or operation.payload.get("run_id") != run.run_id
            or not _same(scope["operation_attempt"], operation.payload.get("attempt", 1))
            or not _same(scope["deletion_epoch"], deletion_epoch(connection))):
        raise _denied()
    return operation


def _append(operation, ticket, proof):
    saved = deepcopy(operation.payload.get(REVIEW_SOURCE_KEY, {"schema_version": 1, "proofs": {}}))
    if (not isinstance(saved, dict) or type(saved.get("schema_version")) is not int or saved["schema_version"] != 1
            or set(saved) != {"schema_version", "proofs"} or not isinstance(saved["proofs"], dict)):
        raise _denied()
    old = saved["proofs"].get(ticket["execution_id"])
    if old is not None:
        if not _same(old, proof):
            raise _denied()
        return
    if len(saved["proofs"]) >= MAX_REVIEW_PROOFS:
        raise _denied()
    saved["proofs"][ticket["execution_id"]] = deepcopy(proof)
    operation.payload = {**operation.payload, REVIEW_SOURCE_KEY: saved}
    operation.save(update_fields=["payload"])


def seal_review_source(*, parent, child, connection, verified):
    """Seal only a freshly admitted child inside its original creation transaction."""
    if not transaction.get_connection().in_atomic_block or not isinstance(verified, VerifiedReviewSource):
        raise _denied()
    request = child.run_request or {}
    ticket = current_execution_ticket(child)
    if (verified.connection_identity != _connection_identity(connection)
            or verified.parent_run_id != parent.run_id
            or verified.parent_admission_digest != evidence_digest(wire_identity(owner_receipt(parent)))
            or owner_receipt(child) is None or owner_receipt(child)["authority"].get("event_protocol") != 1
            or child.workflow not in {"article_revision", "article_system_setup"}
            or child.organization_id != parent.organization_id or child.organization_id != connection.organization_id
            or child.github_repo != parent.github_repo or child.github_repo != connection.github_repo
            or request.get("parent_run_id") != parent.run_id
            or request.get("parent_admission_digest") != verified.parent_admission_digest
            or request.get("branch") != connection.branch or request.get("app_root") != connection.app_root
            or request.get("configuration_revision") != connection.configuration_version
            or request.get("reviewed_revision") != verified.reviewed_revision
            or request.get("reviewed_source_ref") != verified.reviewed_source_ref
            or ticket["scope"].get("expected_source_sha") != verified.expected_source_sha
            or connection_contract(request) != connection_contract({**request, "website_connection_id": str(connection.pk),
                "connection_generation": connection.generation, "repository_id": connection.repository_id})):
        raise _denied()
    operation = _operation_for(child, connection, ticket, lock=True)
    _append(operation, ticket, _proof(child, parent, connection, ticket, revision=verified.reviewed_revision,
        source_ref=verified.reviewed_source_ref, source_sha=verified.expected_source_sha))


def _candidate(payload, connection, *, lock=False):
    from workflow_runs.models import ContentFactoryRun
    request_ref = payload.get("reviewed_source_ref")
    if request_ref is None:
        # No exception is granted to ordinary/default-branch work.
        return None
    query = ContentFactoryRun.objects.select_for_update() if lock else ContentFactoryRun.objects
    wire = payload.get(WIRE_REQUEST_KEY)
    key = wire.get("dispatch_key") if isinstance(wire, dict) else None
    run = query.filter(result__run_owner_v1__identity__dispatch_key=key, organization_id=connection.organization_id).first() if type(key) is str else None
    if (run is None or owner_receipt(run) is None or run.workflow not in {"article_revision", "article_system_setup"}
            or owner_receipt(run)["authority"].get("event_protocol") != 1
            or payload.get("run_id", run.run_id) != run.run_id):
        raise _denied()
    try:
        require_admission_identity(run, payload)
        require_event_execution(run, payload, current=True)
        from .run_execution_control import require_current_principal
        require_current_principal(run)
        ticket = current_execution_ticket(run)
        operation = _operation_for(run, connection, ticket, lock=lock)
        parent = query.filter(run_id=run.run_request.get("parent_run_id"), organization_id=run.organization_id).first()
        if parent is None or owner_receipt(parent) is None:
            raise _denied()
        proof = (operation.payload.get(REVIEW_SOURCE_KEY) or {}).get("proofs", {}).get(ticket["execution_id"])
        wanted = _proof(run, parent, connection, ticket, revision=run.run_request.get("reviewed_revision"),
            source_ref=run.run_request.get("reviewed_source_ref"), source_sha=ticket["scope"].get("expected_source_sha"))
        if (not _same(proof, wanted) or request_ref != wanted["reviewed_source_ref"]
                or payload.get("expected_source_sha") != wanted["expected_source_sha"]
                or any(not _same(payload.get(field), ticket["scope"].get(field)) for field in ticket["scope"])
                or run.github_repo != connection.github_repo or not _valid_ref(request_ref)
                or payload.get("branch") != run.run_request.get("branch") or payload.get("branch") != connection.branch
                or payload.get("app_root") != run.run_request.get("app_root") or payload.get("app_root") != connection.app_root
                or not _same(payload.get("configuration_revision"), connection.configuration_version)):
            raise _denied()
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise _denied() from exc
    return {"run": run, "parent": parent, "connection": connection, "proof": wanted}


def review_source_preflight(payload, connection):
    """Return proof only after its exact reviewed ref was freshly checked."""
    context = _candidate(payload, connection)
    if context is not None:
        proof = context["proof"]
        verify_review_ref(connection, proof["reviewed_source_ref"], proof["expected_source_sha"])
    return context


@contextmanager
def reviewed_source_scope(payload, connection, verified):
    """Repeat all saved proof/ticket checks after the authority lock is acquired."""
    current = _candidate(payload, connection, lock=True) if verified is not None else None
    if (verified is None) != (current is None) or current is not None and not _same(current["proof"], verified["proof"]):
        raise _denied()
    token = _verified_source.set(current)
    try:
        yield
    finally:
        _verified_source.reset(token)


def append_review_source_ticket(run, previous, ticket):
    """Append refreshed proof only inside a freshly verified backend guard."""
    if not (run.run_request or {}).get("reviewed_source_ref"):
        return
    context = _verified_source.get()
    if (not transaction.get_connection().in_atomic_block or context is None
            or context["run"].pk != run.pk or not _same(context["proof"]["ticket_digest"], evidence_digest(previous))):
        raise _denied()
    old_scope, new_scope = previous["scope"], ticket["scope"]
    # An authorized retry may refresh only operation attempt/deletion/config
    # context; it never changes original reviewed source or repository consent.
    if any(not _same(old_scope[field], new_scope[field]) for field in old_scope
            if field not in {"operation_id", "operation_attempt", "deletion_epoch"}):
        raise _denied()
    connection = context["connection"]
    operation = _operation_for(run, connection, ticket, lock=True)
    proof = {**context["proof"], "operation_id": new_scope["operation_id"],
        "operation_attempt": new_scope["operation_attempt"], "deletion_epoch": new_scope["deletion_epoch"],
        "execution_id": ticket["execution_id"], "generation": ticket["generation"],
        "request_digest": ticket["request_digest"], "ticket_digest": evidence_digest(ticket)}
    _append(operation, ticket, proof)
