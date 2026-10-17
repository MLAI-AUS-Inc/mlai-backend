"""Private pre-policy setup authority, independent of a browser's selected company."""

from dataclasses import dataclass, field
from datetime import timedelta
from types import SimpleNamespace
from uuid import UUID

from django.conf import settings
from django.utils import timezone

from . import cms_goal_authority as goals
from . import cms_publication_policy as policies
from .cms_operation_scope import current_scope
from .cms_setup_contract import validate_intent
from .cms_goal_contract import _time
from .channel_artifact_contract import canonical_bytes, digest
from .website_contract import WebsiteAuthorityError
from .website_connections import contract_for

ACTION = "cms-setup"
INTENT_ACTION = "cms-setup-intent"
ACTIVE = frozenset({"pending", "running", "needs_user"})
FIELDS = {"schema_version", "binding", "credential_operation_id", "company_id", "requested_by_user_id",
    "intent_operation_id", "intent_digest"}


@dataclass(frozen=True)
class CmsSetupDispatch:
    """A server-only expiring setup lease; never construct it from public JSON."""

    operation_id: str
    lease_id: str = field(repr=False)


def _deny():
    return WebsiteAuthorityError("website_operation_changed", "The original native setup or its current owner changed.")


def enabled(organization_id):
    """Preparatory setup has an independent default-off rollout gate."""
    return bool(getattr(settings, "ARTICLES_CMS_SETUP_ENABLED", False) and goals.enabled(organization_id))


def historical(operation):
    """Read the original private accepted intent, without granting provider access."""
    from .website_models import WebsiteConnectionOperation
    try:
        payload = operation.payload
        if (operation.action != ACTION or not isinstance(payload, dict) or set(payload) - {"lease_id"} != FIELDS or
                type(payload["schema_version"]) is not int or payload["schema_version"] != 1 or
                not isinstance(payload["intent_operation_id"], str) or
                str(UUID(payload["intent_operation_id"])) != payload["intent_operation_id"] or
                "lease_id" in payload and (not isinstance(payload["lease_id"], str) or str(UUID(payload["lease_id"])) != payload["lease_id"])):
            raise _deny()
        child = WebsiteConnectionOperation.objects.filter(pk=payload["intent_operation_id"], action=INTENT_ACTION,
            connection_id=operation.connection_id, generation=operation.generation, state="completed").first()
        if child is None or child.idempotency_key != "cms-setup-intent:" + str(operation.pk):
            raise _deny()
        intent = validate_intent(child.payload, expected_binding=payload["binding"])
        if (intent["operation_id"] != str(operation.pk) or intent["digest"] != payload["intent_digest"] or
                operation.idempotency_key != f"cms-setup:{operation.connection_id}:{operation.generation}:{intent['request_id']}" or
                intent["credential_operation_id"] != payload["credential_operation_id"] or
                intent["binding"]["connection_id"] != str(operation.connection_id) or
                intent["binding"]["generation"] != operation.generation or
                intent["binding"]["company_id"] != payload["company_id"] or
                intent["owner_user_id"] != payload["requested_by_user_id"]):
            raise _deny()
        return intent
    except WebsiteAuthorityError:
        raise
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
        raise _deny() from None


def policy_data(value):
    """Build the fixed provider-discovery request from the private accepted intent."""
    return {**contract_for(value.connection), "client_request_id": value.intent["request_id"],
        "expected_policy_id": (value.intent["quote"]["policy"] or {}).get("id"),
        **{key: value.intent["answers"][key] for key in ("review_policy", "publication_confirmed", "native_defaults_confirmed")}}


def current(operation, *, user=None, lease_id=None, states=ACTIVE):
    """Reload the actual owner/site and only an original or idempotent own policy."""
    intent = historical(operation)
    if (not enabled(operation.connection.organization_id) or operation.state not in states or
            lease_id is not None and (operation.state != "running" or operation.payload.get("lease_id") != lease_id or
                operation.next_attempt_at is None or operation.next_attempt_at <= timezone.now())):
        raise _deny()
    scoped = current_scope(operation, allowed_states=states)
    if (user is not None and str(user.pk) != str(scoped.user.pk) or
            _time(intent["accepted_at"]) > timezone.now() + timedelta(seconds=30)):
        raise _deny()
    saved = policies.current_policy(scoped.context, scoped.config, user=scoped.user)
    initial = intent["quote"]["policy"]
    revision = scoped.connection.configuration_version
    if type(revision) is not int:
        raise _deny()
    if revision == intent["quote"]["configurationRevision"]:
        if (policies.projection(saved) if saved is not None else None) != initial:
            raise _deny()
    else:
        # A successful discovery may commit just before its acknowledgement is
        # lost. Only that exact private request may advance the config revision.
        from .website_models import WebsiteConnectionOperation
        key = f"cms-policy:{operation.connection_id}:{operation.generation}:{intent['request_id']}"
        own = WebsiteConnectionOperation.objects.filter(idempotency_key=key, action=policies.ACTION,
            connection_id=operation.connection_id, generation=operation.generation, state="completed").first()
        request_digest = digest({key: intent["answers"][key] for key in ("review_policy", "publication_confirmed", "native_defaults_confirmed")})
        if (revision != intent["quote"]["configurationRevision"] + 1 or saved is None or own is None or
                saved["operation_id"] != str(own.pk) or saved["policy"]["request_digest"] != request_digest or
                saved["policy"]["credential_operation_id"] != intent["credential_operation_id"]):
            raise _deny()
    return SimpleNamespace(**scoped.__dict__, intent=intent, policy=saved)


def for_policy(dispatch, *, context, config, user, data):
    """Require the exact still-live private lease before each read and policy commit."""
    if not isinstance(dispatch, CmsSetupDispatch):
        raise _deny()
    from .website_models import WebsiteConnectionOperation
    operation = WebsiteConnectionOperation.objects.select_related("connection__organization").filter(
        pk=dispatch.operation_id, action=ACTION).first()
    if operation is None:
        raise _deny()
    value = current(operation, user=user, lease_id=dispatch.lease_id)
    if (str(value.context.organization.pk) != str(context.organization.pk) or str(value.context.company.pk) != str(context.company.pk) or
            str(value.config.pk) != str(config.pk) or canonical_bytes(policy_data(value)) != canonical_bytes(data)):
        raise _deny()
    return value
