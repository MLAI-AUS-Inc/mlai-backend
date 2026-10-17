"""Explicit owned withdrawal authority, separate from paid generation admission."""

from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace

from django.conf import settings
from django.utils import timezone

from . import cms_article_publication as publishing
from . import cms_unpublish_contract as contract
from . import cms_unpublish_public as public
from .channel_artifact_contract import canonical_bytes
from .cms_api_transport import CmsApiError
from .cms_connection_authority import current_credential, enabled as cms_enabled
from .cms_operation_scope import current_scope
from .website_contract import WebsiteAuthorityError

ACTION = "cms-unpublish"
ACTIVE = frozenset({"pending", "running"})
TTL = timedelta(minutes=15)
FIELDS = {"schema_version", "binding", "credential_operation_id", "intent", "journal", "accepted_at", "deadline",
    "provider_reads", "public_reads", "failures", "last_failure_code", "public_observation"}


def deny():
    """Return an opaque refusal without exporting the original article or key."""
    return WebsiteAuthorityError("website_operation_changed", "The original CMS article or withdrawal request changed.")


def enabled():
    """Withdrawals require their own opt-in as well as current CMS execution."""
    return cms_enabled() and getattr(settings, "ARTICLES_CMS_UNPUBLISH_ENABLED", False)


def requested(publication):
    """An accepted owner direction fences fresh readiness and future repairs.

    Keep this barrier when rollout is paused or the request stops. It grants no
    absence proof and leaves the original historical publication record intact.
    """
    from .website_models import WebsiteConnectionOperation
    return WebsiteConnectionOperation.objects.filter(action=ACTION, connection_id=publication.connection_id,
        generation=publication.generation, payload__binding=publication.payload["binding"],
        payload__intent__publication_operation_id=str(publication.pk)).exclude(state="deleted").exists()


def target(operation, *, binding):
    """Read a backend-owned confirmed journal without paid-work or freshness gates.

    Ownership is still checked against the provider immediately before an update.
    An uncertain original create, copied URL or caller-supplied post ID cannot
    select a withdrawal target. Expired live-health evidence is not permission.
    """
    try:
        p = operation.payload if operation is not None else None
        if (operation is None or operation.action != publishing.ACTION or operation.state == "deleted" or
                not isinstance(p, dict) or set(p) - {"lease_id"} != publishing.FIELDS or
                type(p["schema_version"]) is not int or p["schema_version"] != 1 or
                str(operation.connection_id) != binding["connection_id"] or operation.generation != binding["generation"] or
                canonical_bytes(p["binding"]) != canonical_bytes(binding)):
            raise deny()
        intent, state = publishing.journal.validate_intent(p["intent"]), p["post_state"]
        contract._original(intent, state)
        if (intent["operation_id"] != str(operation.pk) or canonical_bytes(intent["binding"]) != canonical_bytes(binding) or
                intent["artifact_sha256"] != p["staged_revision"]):
            raise deny()
        return SimpleNamespace(operation=operation, intent=intent, state=deepcopy(state))
    except (CmsApiError, ValueError, TypeError, KeyError, AttributeError, RecursionError):
        raise deny() from None


def historical(operation):
    """Validate the closed private request and its original publication identity."""
    from .website_models import WebsiteConnectionOperation
    try:
        p = operation.payload
        if (operation.action != ACTION or not isinstance(p, dict) or set(p) - {"lease_id"} != FIELDS or
                type(p["schema_version"]) is not int or p["schema_version"] != 1):
            raise deny()
        intent = p["intent"]
        original = target(WebsiteConnectionOperation.objects.filter(pk=intent["publication_operation_id"],
            action=publishing.ACTION).first(), binding=p["binding"])
        contract.validate_intent(intent, publication=original.intent, published_state=original.state)
        journal = contract.validate_state(p["journal"], intent, publication=original.intent, published_state=original.state)
        accepted, deadline = public._time(p["accepted_at"]), public._time(p["deadline"])
        if (intent["operation_id"] != str(operation.pk) or operation.idempotency_key != key(intent) or
                operation.generation != intent["binding"]["generation"] or str(operation.connection_id) != intent["binding"]["connection_id"] or
                deadline - accepted != TTL or any(type(p[name]) is not int or not 0 <= p[name] <= maximum
                    for name, maximum in (("provider_reads", 12), ("public_reads", 8), ("failures", 20))) or
                p["last_failure_code"] is not None and not isinstance(p["last_failure_code"], str)):
            raise deny()
        contract._uuid(p["credential_operation_id"])
        if "lease_id" in p: contract._uuid(p["lease_id"])
        if p["public_observation"] is not None:
            public.validate_observation(p["public_observation"], intent, journal,
                publication=original.intent, published_state=original.state)
            if not accepted <= public._time(p["public_observation"]["checked_at"]) < deadline: raise deny()
        return SimpleNamespace(intent=deepcopy(intent), journal=journal, original=original, accepted=accepted, deadline=deadline)
    except (CmsApiError, ValueError, TypeError, KeyError, AttributeError, RecursionError):
        raise deny() from None


def key(intent):
    """Scope one owner nonce to the original site generation and publication."""
    return f"cms-unpublish:{intent['binding']['connection_id']}:{intent['binding']['generation']}:{intent['request_id']}"


def current(operation, *, user=None, lease_id=None, states=ACTIVE, now=None):
    """Reload actual current owner/key and the winning private, unexpired lease."""
    now = now or timezone.now()
    value = historical(operation)
    if (not enabled() or operation.state not in states or not value.accepted <= now < value.deadline or
            lease_id is not None and (operation.state != "running" or operation.payload.get("lease_id") != lease_id or
                operation.next_attempt_at is None or operation.next_attempt_at <= now)):
        raise deny()
    scope = current_scope(operation, lease_id=lease_id, allowed_states=states)
    binding, credential = current_credential(scope.context, scope.config, scope.connection, user=scope.user)
    if (canonical_bytes(binding) != canonical_bytes(value.intent["binding"]) or
            user is not None and str(user.pk) != str(scope.user.pk)):
        raise deny()
    return SimpleNamespace(**value.__dict__, scope=scope, credential=credential)
