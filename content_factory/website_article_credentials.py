"""Secret-free durable inventory for the website-token broker only.

This is not full credential/resource coverage or a replacement/quiet grant.
Bearers remain in cache. Missing records and uncertain mints stay unknown.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from datetime import datetime
import re
from uuid import UUID, uuid4

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .website_contract import WebsiteAuthorityError, evidence_digest, SHA_PATTERN

MARKER = "article_credential_inventory_v1"
INVENTORY_ACTION = "article-credential-inventory"
TOKEN_ACTION = "article-credential"
PRIVATE_ACTIONS = ("article-source-fence", "article-source-stop", "article-source-history", "article-source-publication-read", INVENTORY_ACTION, TOKEN_ACTION)
MAX_TOKENS = 512
SOURCE_PAGE_SIZE = 2
_current_operation = ContextVar("website_credential_operation", default=None)


def _deny():
    raise WebsiteAuthorityError("website_operation_changed", "The original article credential inventory is unavailable or changed.")


@contextmanager
def operation_scope(operation):
    """Carry only the canonical guard's operation while its lock is held."""
    token = _current_operation.set(operation)
    try:
        yield
    finally:
        _current_operation.reset(token)


def _uuid(value):
    try:
        return isinstance(value, str) and str(UUID(value)) == value
    except (ValueError, TypeError, AttributeError):
        return False


def channel_scope(row):
    """Validate retained v1/v2 broker metadata without inventing legacy scope."""
    from . import website_goal as goals
    from .website_article_source_fence import _scope as valid_source_scope
    scope = getattr(row, "payload", None)
    receipt = getattr(row, "receipt", None)
    fields = {"schema_version", "channel", "workflow_operation_id", "goal_id", "connection_id",
        "connection_generation", "repository_id", "deletion_epoch", "source_sha", "client_request_id"}
    if (not isinstance(scope, dict) or type(scope.get("schema_version")) is not int or scope["schema_version"] not in {1, 2}
            or set(scope) != fields | ({"source_scope", "source_scope_digest"} if scope["schema_version"] == 2 else set())
            or getattr(row, "action", None) != INVENTORY_ACTION or not _uuid(str(row.pk))
            or scope["channel"] != "website_token_broker" or not _uuid(scope["workflow_operation_id"])
            or not _uuid(scope["goal_id"]) or not _uuid(scope["connection_id"])
            or scope["connection_id"] != str(row.connection_id) or scope["connection_generation"] != row.generation
            or type(scope["connection_generation"]) is not int or scope["connection_generation"] < 1
            or type(scope["deletion_epoch"]) is not int or scope["deletion_epoch"] < 0
            or not isinstance(scope["repository_id"], str) or re.fullmatch(r"[1-9][0-9]{0,19}", scope["repository_id"]) is None
            or not isinstance(scope["source_sha"], str) or SHA_PATTERN.fullmatch(scope["source_sha"]) is None
            or not isinstance(scope["client_request_id"], str) or re.fullmatch(r"articles-first:[a-f0-9]{64}", scope["client_request_id"]) is None
            or not isinstance(receipt, dict) or set(receipt) != {"schema_version", "from_operation_start", "token_count"}
            or type(receipt.get("schema_version")) is not int or receipt["schema_version"] != 1
            or receipt["from_operation_start"] is not True or type(receipt.get("token_count")) is not int
            or not 0 <= receipt["token_count"] <= MAX_TOKENS):
        _deny()
    if scope["schema_version"] == 2:
        source = scope["source_scope"]
        if (not valid_source_scope(source) or scope["source_scope_digest"] != goals.digest(source)
                or any(type(source[key]) is not type(scope[key]) or source[key] != scope[key]
                    for key in ("connection_id", "connection_generation", "repository_id", "deletion_epoch"))):
            _deny()
    return scope


def _birth_source_scope(original, connection):
    """Select the exact locked server goal; copied request budgets cannot enroll."""
    from . import website_goal as goals
    from .website_article_recovery import enabled
    from .website_article_source_fence import KEY as SOURCE_FENCE_KEY
    parent = connection.operations.select_for_update().filter(pk=original["first_article_goal_operation_id"],
        action="prepare", generation=connection.generation).first()
    if (parent is None or not enabled(parent) or SOURCE_FENCE_KEY in (parent.receipt or {})
            or parent.receipt["goal"].get("current_source_sha") != original["expected_source_sha"]):
        _deny()
    return deepcopy(goals.scope_for(connection, parent.payload.get("company_id")))


def enroll(operation, request, connection, *, created):
    """Record broker coverage only at a new server-enrolled article operation's birth."""
    from .setup_delivery_enrollment import article_stream
    if MARKER in operation.payload:
        return False
    original = operation.payload.get("article_delivery_request")
    if (not created or not getattr(settings, "ARTICLES_ARTICLE_SOURCE_REFRESH_ENABLED", False)
            or not article_stream(original or {})):
        return False
    if (operation.action != "workflow" or type(operation.payload.get("attempt")) is not int or operation.payload["attempt"] != 1
            or operation.payload.get("first_article_goal_operation_id") != original["first_article_goal_operation_id"]
            or not _uuid(original.get("first_article_goal_operation_id")) or not _uuid(str(operation.pk))
            or not _uuid(str(connection.pk)) or type(operation.payload.get("deletion_epoch")) is not int
            or operation.payload["deletion_epoch"] < 0 or type(connection.generation) is not int or connection.generation < 1
            or not isinstance(original.get("expected_source_sha"), str) or not SHA_PATTERN.fullmatch(original["expected_source_sha"])
            or not isinstance(original.get("client_request_id"), str)
            or re.fullmatch(r"articles-first:[a-f0-9]{64}", original["client_request_id"]) is None
            or str(operation.connection_id) != str(connection.pk) or operation.generation != connection.generation):
        _deny()
    from . import website_goal as goals
    source_scope = _birth_source_scope(original, connection)
    scope = {"schema_version": 2, "channel": "website_token_broker", "workflow_operation_id": str(operation.pk),
        "goal_id": original["first_article_goal_operation_id"], "connection_id": str(connection.pk),
        "connection_generation": connection.generation, "repository_id": str(connection.repository_id),
        "deletion_epoch": operation.payload["deletion_epoch"], "source_sha": original["expected_source_sha"],
        "client_request_id": original["client_request_id"], "source_scope": source_scope,
        "source_scope_digest": goals.digest(source_scope)}
    from .website_models import WebsiteConnectionOperation
    row, fresh = WebsiteConnectionOperation.objects.get_or_create(
        idempotency_key=f"{connection.pk}:credential-inventory:{operation.pk}",
        defaults={"connection": connection, "generation": connection.generation, "action": INVENTORY_ACTION,
            "state": "recorded", "payload": scope,
            "receipt": {"schema_version": 1, "from_operation_start": True, "token_count": 0}})
    if not fresh or row.payload != scope:
        _deny()  # A reused row cannot invent zero work for a new enrollment.
    channel_scope(row)
    operation.payload = {**operation.payload, MARKER: {"schema_version": 1,
        "inventory_operation_id": str(row.pk), "scope_digest": evidence_digest(scope)}}
    return True


def _inventory(operation, connection, *, lock=True):
    marker = operation.payload.get(MARKER)
    if (not isinstance(marker, dict) or set(marker) != {"schema_version", "inventory_operation_id", "scope_digest"}
            or type(marker.get("schema_version")) is not int or marker["schema_version"] != 1
            or not _uuid(marker.get("inventory_operation_id"))):
        _deny()
    from .website_models import WebsiteConnectionOperation
    query = WebsiteConnectionOperation.objects.select_for_update() if lock else WebsiteConnectionOperation.objects
    row = query.filter(pk=marker["inventory_operation_id"], connection_id=connection.pk, action=INVENTORY_ACTION).first()
    original = operation.payload.get("article_delivery_request") or {}
    scope = channel_scope(row)
    if (evidence_digest(scope) != marker["scope_digest"] or scope["workflow_operation_id"] != str(operation.pk)
            or scope["connection_id"] != str(connection.pk) or not _uuid(scope["workflow_operation_id"]) or not _uuid(scope["goal_id"])
            or type(scope["connection_generation"]) is not int or scope["connection_generation"] < 1
            or type(scope["deletion_epoch"]) is not int or scope["deletion_epoch"] < 0
            or scope["connection_generation"] != connection.generation
            or scope["repository_id"] != str(connection.repository_id) or scope["deletion_epoch"] != operation.payload.get("deletion_epoch")
            or scope["source_sha"] != original.get("expected_source_sha") or scope["goal_id"] != original.get("first_article_goal_operation_id")
            or scope["client_request_id"] != original.get("client_request_id")):
        _deny()
    if scope["schema_version"] == 2:
        from . import website_goal as goals
        if not goals.scope_matches(scope["source_scope"], goals.scope_for(connection, scope["source_scope"]["company_id"])):
            _deny()
    return row, marker


def reserve(connection, data, *, mode, action, profile):
    """Persist an unknown-outcome row before provider HTTP under the canonical guard."""
    operation = _current_operation.get()
    if operation is None:
        return None
    if MARKER not in (operation.payload or {}):
        if getattr(operation, "action", None) == "workflow":
            from .website_models import WebsiteConnectionOperation
            prior = WebsiteConnectionOperation.objects.filter(
                idempotency_key=f"{connection.pk}:credential-inventory:{operation.pk}").first()
            if prior is not None:
                _deny()  # Losing a known marker cannot downgrade an original to legacy.
        return None
    if (str(operation.pk) != str(data.get("operation_id")) or str(operation.connection_id) != str(connection.pk)
            or str(operation.payload.get("attempt")) != str(data.get("operation_attempt"))
            or operation.generation != connection.generation
            or mode not in {"read", "write"} or profile not in {"repository", "workflow_files", "ci_evidence"}):
        _deny()
    inventory, marker = _inventory(operation, connection)
    if inventory.receipt["token_count"] >= MAX_TOKENS:
        _deny()
    run_id = str(data.get("run_id") or data.get("job_id") or "")
    if not run_id or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", run_id) is None:
        _deny()
    reference = str(uuid4())
    scope = {"schema_version": 1, "inventory_operation_id": str(inventory.pk), "scope_digest": marker["scope_digest"],
        "workflow_operation_id": str(operation.pk), "operation_attempt": operation.payload["attempt"],
        "run_id": run_id, "mode": mode, "action": action, "profile": profile}
    from .website_models import WebsiteConnectionOperation
    row = WebsiteConnectionOperation.objects.create(pk=reference, connection=connection, generation=connection.generation,
        action=TOKEN_ACTION, state="recorded", idempotency_key=f"{connection.pk}:article-credential:{reference}", payload=scope,
        receipt={"schema_version": 1, "mint_state": "pending", "expires_at": None, "delivery_possible": False,
            "revocation_state": "not_attempted", "revocation_attempts": 0})
    inventory.receipt = {**inventory.receipt, "token_count": inventory.receipt["token_count"] + 1}
    inventory.save(update_fields=["receipt", "updated_at"])
    return {"id": str(row.pk), "connection_id": str(connection.pk), "generation": connection.generation, "scope": deepcopy(scope)}


@contextmanager
def _record(reservation):
    """Settle already-reserved metadata after cancellation without granting work."""
    from .website_models import WebsiteConnection, WebsiteConnectionOperation
    with transaction.atomic():
        connection = WebsiteConnection.objects.select_for_update().filter(pk=reservation["connection_id"]).first()
        if connection is None:
            _deny()
        row = WebsiteConnectionOperation.objects.select_for_update().filter(pk=reservation["id"],
            connection_id=reservation["connection_id"], action=TOKEN_ACTION).first()
        if (row is None or row.generation != reservation["generation"] or row.payload != reservation["scope"]
                or not valid_receipt(row.receipt)):
            _deny()
        yield row
        row.save(update_fields=["state", "receipt", "updated_at"])


def valid_receipt(value):
    """Missing or malformed metadata never establishes revoked/expired coverage."""
    basic = (isinstance(value, dict) and set(value) == {"schema_version", "mint_state", "expires_at", "delivery_possible", "revocation_state", "revocation_attempts"}
        and type(value.get("schema_version")) is int and value["schema_version"] == 1
        and isinstance(value.get("mint_state"), str) and value["mint_state"] in {"pending", "issued", "uncertain"}
        and type(value.get("delivery_possible")) is bool and isinstance(value.get("revocation_state"), str) and value["revocation_state"] in {"not_attempted", "confirmed", "uncertain"}
        and type(value.get("revocation_attempts")) is int and 0 <= value["revocation_attempts"] <= 10000
        and (value.get("expires_at") is None or _expiry(value["expires_at"]) is not None))
    if not basic:
        return False
    if value["mint_state"] == "issued":
        return _expiry(value["expires_at"]) is not None
    return value["expires_at"] is None and value["delivery_possible"] is False



def _expiry(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) and len(value) <= 128 else None
        return parsed if parsed is not None and parsed.tzinfo is not None and parsed.utcoffset() is not None else None
    except (ValueError, TypeError, OverflowError):
        return None


def issued(reservation, credential):
    """Record the provider's actual expiry before a fresh delivery authorization."""
    if reservation is None:
        return
    expires = getattr(credential, "expires_at", None)
    valid = isinstance(expires, datetime) and timezone.is_aware(expires) and expires > timezone.now()
    with _record(reservation) as row:
        if (row.receipt["mint_state"] != "pending" or row.receipt["delivery_possible"]
                or row.receipt["revocation_state"] != "not_attempted"):
            _deny()
        row.receipt = {**row.receipt, "mint_state": "issued" if valid else "uncertain", "expires_at": expires.isoformat() if valid else None}
        row.state = "recorded"
    if not valid:
        _deny()  # The minted bearer must be revoked, never delivered.


def delivery_possible(reservation):
    """Commit possible exposure before returning a bearer; a lost reply stays exposed."""
    if reservation is None:
        return
    with _record(reservation) as row:
        if (row.receipt["mint_state"] != "issued" or row.receipt["revocation_state"] != "not_attempted"
                or _expiry(row.receipt["expires_at"]) <= timezone.now()):
            _deny()
        row.receipt = {**row.receipt, "delivery_possible": True}


def uncertain(reservation):
    """Retain an unknown mint outcome, including lost replies and invalid credentials."""
    if reservation is not None:
        with _record(reservation) as row:
            if row.receipt["mint_state"] == "pending":
                row.receipt = {**row.receipt, "mint_state": "uncertain"}


def revocation(reservation, *, confirmed):
    """Only an observed successful provider DELETE settles revocation, never cache loss."""
    if reservation is not None:
        with _record(reservation) as row:
            row.receipt = {**row.receipt, "revocation_state": "confirmed" if confirmed or row.receipt["revocation_state"] == "confirmed" else "uncertain",
                "revocation_attempts": min(10000, row.receipt["revocation_attempts"] + 1)}
            if confirmed:
                row.state = "completed"


def settled(reservation, receipt, *, now=None):
    """Only confirmed revocation or the recorded actual expiry settles a known token."""
    if not valid_receipt(receipt):
        _deny()
    expires = _expiry(receipt["expires_at"])
    return receipt["revocation_state"] == "confirmed" or (expires is not None and expires <= (now or timezone.now()))


def discover(connection_id, generation):
    """Recover bounded references independently of cache; this is broker-only inventory."""
    from .website_models import WebsiteConnectionOperation
    query = WebsiteConnectionOperation.objects.filter(connection_id=connection_id, action=TOKEN_ACTION)
    scope = str(generation)
    if scope.startswith("operation:"):
        identifier = scope.removeprefix("operation:")
        if not _uuid(identifier):
            _deny()
        query = query.filter(payload__workflow_operation_id=identifier)
    else:
        if not scope.isascii() or not scope.isdecimal() or not 0 < int(scope) < 10**12:
            _deny()
        query = query.filter(generation=int(scope)).exclude(state="completed")
    rows = list(query.order_by("id")[:MAX_TOKENS + 1])
    overflow = len(rows) > MAX_TOKENS
    result, channels = {}, {}
    for row in rows[:MAX_TOKENS]:
        payload = row.payload
        if (not isinstance(payload, dict) or set(payload) != {"schema_version", "inventory_operation_id", "scope_digest",
                "workflow_operation_id", "operation_attempt", "run_id", "mode", "action", "profile"}
                or type(payload.get("schema_version")) is not int or payload["schema_version"] != 1
                or not _uuid(str(row.pk)) or not _uuid(payload.get("inventory_operation_id"))
                or not _uuid(payload.get("workflow_operation_id"))
                or not isinstance(payload.get("scope_digest"), str) or re.fullmatch(r"[a-f0-9]{64}", payload["scope_digest"]) is None
                or type(payload.get("operation_attempt")) is not int or payload["operation_attempt"] < 1
                or not isinstance(payload.get("run_id"), str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", payload["run_id"]) is None
                or not isinstance(payload.get("action"), str) or not 0 < len(payload["action"]) <= 100
                or payload.get("mode") not in ("read", "write") or payload.get("profile") not in ("repository", "workflow_files", "ci_evidence")
                or not valid_receipt(row.receipt)):
            _deny()
        channel_id = payload["inventory_operation_id"]
        if channel_id not in channels:
            channels[channel_id] = WebsiteConnectionOperation.objects.filter(pk=channel_id,
                connection_id=connection_id, action=INVENTORY_ACTION).first()
        channel = channels[channel_id]
        scope = channel_scope(channel)
        if (evidence_digest(scope) != payload["scope_digest"]
                or scope.get("channel") != "website_token_broker" or scope.get("workflow_operation_id") != payload["workflow_operation_id"]
                or scope.get("connection_id") != str(connection_id) or scope.get("connection_generation") != row.generation
                or type(scope.get("connection_generation")) is not int or scope["connection_generation"] < 1
                or not isinstance(channel.receipt, dict) or channel.receipt.get("from_operation_start") is not True):
            _deny()
        result[str(row.pk)] = ({"id": str(row.pk), "connection_id": str(connection_id), "generation": row.generation,
            "scope": deepcopy(payload)}, deepcopy(row.receipt))
    return result, overflow


def source_page(connection_id, fence, *, after=None):
    """Enumerate only freshly recorded exact original source scopes, two per pass.

    Caller must supply its freshly authenticated private causal fence while
    holding the site lock. Legacy channels cannot acquire an inferred scope.
    This page grants effect removal only, never complete coverage or quiet.
    """
    from . import website_goal as goals
    from .website_models import WebsiteConnectionOperation
    from .website_article_source_fence import _scope as valid_source_scope
    try:
        binding = fence["binding"]
        source = binding["source_scope"]
        if (not valid_source_scope(source) or str(connection_id) != source["connection_id"]
                or not _uuid(binding["goal"]["goal_id"]) or after is not None and not _uuid(after)):
            _deny()
        captured = list({node["operation_id"] for node in binding["runs"]})
        query = WebsiteConnectionOperation.objects.filter(connection_id=connection_id, generation=source["connection_generation"],
            action=INVENTORY_ACTION, payload__schema_version=2, payload__goal_id=binding["goal"]["goal_id"],
            payload__source_sha=binding["source_sha"], payload__source_scope_digest=goals.digest(source)).exclude(
                payload__workflow_operation_id__in=captured)
        if after is not None:
            query = query.filter(pk__gt=after)
        rows = list(query.order_by("pk")[:SOURCE_PAGE_SIZE + 1])
        for row in rows:
            scope = channel_scope(row)
            if (scope["schema_version"] != 2 or not goals.scope_matches(scope["source_scope"], source)
                    or scope["goal_id"] != binding["goal"]["goal_id"] or scope["source_sha"] != binding["source_sha"]
                    or scope["workflow_operation_id"] in captured):
                _deny()
        selected = rows[:SOURCE_PAGE_SIZE]
        return [row.payload["workflow_operation_id"] for row in selected], str(selected[-1].pk) if len(rows) > SOURCE_PAGE_SIZE else None
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
        _deny()


def mark_expired(reservation):
    """Retire enumeration work only after validating an original expiry/receipt."""
    with _record(reservation) as row:
        if not settled(reservation, row.receipt):
            _deny()
        row.state = "completed"
