"""Renew the same native principal without replacing an original paid article."""

from dataclasses import dataclass
from copy import deepcopy
import re

from django.utils import timezone

from .cms_connection_authority import current_credential, PRIVATE_CREDENTIAL_ACTION
from .cms_connection_contract import seal_credential
from .channel_artifact_contract import digest
from .website_contract import WebsiteAuthorityError

IDENTITY_FIELDS = {"schema_version", "platform", "principal_id", "can_edit_posts", "can_publish_posts", "can_upload_media"}


def _deny():
    return WebsiteAuthorityError("website_connection_changed", "Reconnect the original CMS account to keep its saved article.")


def _identity(value, *, platform, publishing=False):
    if (not isinstance(value, dict) or set(value) != IDENTITY_FIELDS or type(value["schema_version"]) is not int or
            value["schema_version"] != 1 or value["platform"] != platform or
            any(type(value[key]) is not bool for key in ("can_edit_posts", "can_publish_posts", "can_upload_media")) or
            value["can_edit_posts"] is not True or publishing and value["can_publish_posts"] is not True or
            not isinstance(value["principal_id"], str) or
            (re.fullmatch(r"[1-9][0-9]{0,18}", value["principal_id"]) is None if platform == "wordpress"
                else re.fullmatch(r"[a-f0-9]{24}", value["principal_id"]) is None)):
        raise _deny()
    return value["principal_id"]


@dataclass(frozen=True)
class Renewal:
    """Private pre-HTTP fence for the current native key, never an input grant."""

    binding: dict
    credential_operation_id: str
    credential_digest: str
    principal_id: str
    configuration_revision: int


def candidate(context, config, *, user, proposal):
    """Select only a still-connected identical owner/site/provider installation."""
    connection = config.website_connection
    if connection is None or connection.state != "connected" or "_cms_binding" not in (connection.capabilities or {}):
        return None
    binding, _secret = current_credential(context, config, connection, user=user)
    if any(binding[key] != proposal[key] for key in ("platform", "public_origin", "api_origin")):
        return None
    principal = _identity(connection.capabilities.get("_cms_identity"), platform=binding["platform"])
    identifier = connection.capabilities["_cms_credential_operation_id"]
    row = connection.operations.filter(pk=identifier, action=PRIVATE_CREDENTIAL_ACTION,
        generation=binding["generation"], state="completed").first()
    if row is None:
        raise _deny()
    return Renewal(deepcopy(binding), identifier, digest(row.payload), principal, connection.configuration_version)


def renew(selected, context, config, *, user, proposal, credential, identity, request_id, operation_key):
    """Commit under the caller's organization lock after actual identity GETs.

    Preserve connection/generation, policy, selection revision and private key
    operation ID. The original article envelope, hold and one-create journal
    therefore stay bound. A different principal is not credential renewal.
    """
    if not isinstance(selected, Renewal):
        raise _deny()
    current = candidate(context, config, user=user, proposal=proposal)
    if current != selected:
        raise _deny()
    principal = _identity(identity, platform=selected.binding["platform"], publishing=True)
    if principal != selected.principal_id:
        raise _deny()
    connection = config.website_connection
    from .website_models import WebsiteConnectionOperation
    row = WebsiteConnectionOperation.objects.select_for_update().filter(pk=selected.credential_operation_id,
        connection_id=connection.pk, action=PRIVATE_CREDENTIAL_ACTION, generation=connection.generation, state="completed").first()
    if row is None or digest(row.payload) != selected.credential_digest:
        raise _deny()
    # A freshly proposed UUID cannot rebind ciphertext from another scope.
    # Seal the owner's verified replacement for the actual original binding.
    row.payload = {"schema_version": 1, "binding": deepcopy(selected.binding), "sealed_credential": seal_credential(
        selected.binding, username=credential.username, secret=credential.secret)}
    row.save(update_fields=["payload", "updated_at"])
    connection.capabilities = {**connection.capabilities, "_cms_connect_request_id": request_id, "_cms_identity": deepcopy(identity)}
    connection.save(update_fields=["capabilities", "updated_at"])
    WebsiteConnectionOperation.objects.create(connection=connection, generation=connection.generation, action="cms-connect", state="completed",
        idempotency_key=operation_key, payload={}, receipt={"status": "connected", "channel": "cms", "platform": selected.binding["platform"],
            "credential_stored": True, "publishing_verified": False})
    # Wake only an actual original key-access wait. Never reset a running
    # lease, revive cancelled work or bypass an owner's article review.
    WebsiteConnectionOperation.objects.filter(connection_id=connection.pk, generation=connection.generation,
        action__in={"cms-goal", "cms-setup"}, state="needs_user", payload__binding=selected.binding,
        receipt__user_action__id="connect_cms").update(next_attempt_at=timezone.now())
    # Resume only the original status-only request's explicit key wait. Its
    # deadline, read allowances and issued-write journal remain unchanged.
    WebsiteConnectionOperation.objects.filter(connection_id=connection.pk, generation=connection.generation,
        action="cms-unpublish", state="needs_user", payload__binding=selected.binding,
        receipt__user_action__id="connect_cms").update(state="pending", next_attempt_at=timezone.now())
    return connection


def require_replaceable(connection):
    """Do not abandon original paid/issued native work for another binding."""
    if connection is not None and "_cms_binding" in (connection.capabilities or {}) and connection.operations.filter(
            action__in={"cms-setup", "cms-goal", "cms-artifact", "cms-publish", "cms-unpublish"}).exclude(state="deleted").exists():
        raise _deny()
