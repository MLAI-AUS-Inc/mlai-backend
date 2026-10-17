"""Accept one explicit status-only withdrawal, fencing its original publishers."""

from copy import deepcopy
from uuid import uuid4

from django.core.exceptions import ObjectDoesNotExist
from django.db import transaction
from django.utils import timezone

from . import cms_unpublish_authority as authority
from . import cms_unpublish_contract as contract
from .cms_capture import _consent
from .cms_owner_connection import _request_id
from .website_contract import WebsiteAuthorityError


def _refresh(context, config, user):
    for row in (context.organization, context.profile, context.company, user, config): row.refresh_from_db()
    if config.website_connection is None: raise authority.deny()
    config.website_connection.refresh_from_db()


def fence(original, *, now):
    """Under the owning org lock, stop only this writing source's future work.

    Completed historical achievements and unrelated sources are retained. No
    credit, wallet reservation or generation admission belongs to withdrawal.
    """
    if not transaction.get_connection().in_atomic_block:
        raise authority.deny()
    from .website_models import WebsiteConnectionOperation
    rows = WebsiteConnectionOperation.objects.select_for_update().filter(connection_id=original.operation.connection_id,
        generation=original.operation.generation, action="cms-goal", payload__source_run_id=original.intent["source_run_id"])
    for row in [original.operation, *rows]:
        if row.state not in {"pending", "running", "needs_user", "needs_attention", "blocked", "stopped"}:
            continue
        row.payload = {key: value for key, value in row.payload.items() if key != "lease_id"}
        row.state, row.next_attempt_at = "cancelled", None
        row.receipt = {**row.receipt, "status": "owner_withdrawal_requested", "user_action": None}
        row.save(update_fields=["payload", "state", "next_attempt_at", "receipt", "updated_at"])


def start(context, config, *, user, data, now=None):
    """Persist one owner-confirmed original post before any provider request."""
    if not authority.enabled():
        raise WebsiteAuthorityError("publishing_adapter_required", "CMS article withdrawal is unavailable.", status=404)
    from organizations.models import Organization
    from .website_models import WebsiteConnectionOperation
    request_id, now = _request_id(data), now or timezone.now()
    try:
        contract._uuid(data.get("publication_operation_id"))
        if data.get("confirmed") is not True: raise authority.deny()
        with transaction.atomic():
            Organization.objects.select_for_update().get(pk=context.organization.pk)
            _refresh(context, config, user)
            binding = _consent(context, config, user, data)
            original = authority.target(WebsiteConnectionOperation.objects.select_for_update().filter(
                pk=data["publication_operation_id"], action="cms-publish").first(), binding=binding)
            if (data.get("publication_intent_digest") != original.intent["digest"] or
                    data.get("public_url") != original.state["public_url"]):
                raise authority.deny()
            identifier = uuid4()
            intent = contract.make_intent(operation_id=str(identifier), request_id=request_id,
                publication=original.intent, state=original.state)
            prior = WebsiteConnectionOperation.objects.filter(idempotency_key=authority.key(intent)).first()
            if prior is not None:
                saved = authority.historical(prior)
                if saved.intent["publication_operation_id"] != str(original.operation.pk): raise authority.deny()
                return prior
            other = WebsiteConnectionOperation.objects.filter(action=authority.ACTION, connection_id=binding["connection_id"],
                generation=binding["generation"], payload__intent__publication_operation_id=str(original.operation.pk)).order_by("-created_at", "-pk").first()
            # A different nonce cannot reset an unfinished or uncertain write.
            # After a proven completed withdrawal, the owner may explicitly
            # request another if they subsequently republished the same post.
            if other is not None:
                authority.historical(other)
                proof = other.payload["public_observation"]
                if (other.state != "completed" or proof is None or other.receipt.get("status") != "article_withdrawn" or
                        other.receipt.get("withdrawal_verified") is not True or other.receipt.get("publicProofDigest") != proof["digest"]):
                    raise WebsiteAuthorityError("operation_key_conflict", "Read the original article withdrawal request.")
            fence(original, now=now)
            operation, _created = WebsiteConnectionOperation.objects.get_or_create(idempotency_key=authority.key(intent), defaults={
                "id": identifier, "connection": config.website_connection, "generation": binding["generation"], "action": authority.ACTION,
                "state": "pending", "next_attempt_at": now,
                "payload": {"schema_version": 1, "binding": deepcopy(binding),
                    "credential_operation_id": config.website_connection.capabilities["_cms_credential_operation_id"],
                    "intent": intent, "journal": contract.new_state(intent, publication=original.intent, published_state=original.state),
                    "accepted_at": now.isoformat(), "deadline": (now + authority.TTL).isoformat(), "provider_reads": 0,
                    "public_reads": 0, "failures": 0, "last_failure_code": None, "public_observation": None},
                "receipt": {"status": "withdrawal_pending", "channel": "cms", "provider_modified": False, "publishing_verified": False}})
            authority.current(operation, user=user, now=now)
            return operation
    except (ObjectDoesNotExist, contract.CmsApiError, ValueError, TypeError, KeyError, AttributeError, RecursionError):
        raise authority.deny() from None
