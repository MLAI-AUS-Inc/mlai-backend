"""Actual read-only publishing access observations for an unchanged native policy."""

from copy import deepcopy
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from . import cms_publication_policy as policies
from . import cms_goal_contract as goals
from .channel_artifact_contract import digest
from .cms_api_transport import CmsApiError, request_json
from .cms_connection_authority import current_credential
from .website_contract import WebsiteAuthorityError

FIELDS = {"schema_version", "kind", "policy_operation_id", "policy_digest", "credential_operation_id", "checked_at", "digest"}
MAX_SETUP_AGE = timedelta(days=1)


def _deny():
    return WebsiteAuthorityError("website_configuration_changed", "The original CMS publishing access changed.")


def observation(row, saved):
    """Validate only the selected private policy's actual access observation."""
    if (row is None or str(row.pk) != saved["operation_id"] or row.action != policies.ACTION or row.state != "completed" or
            policies.validate_policy(row.payload) != saved["policy"]):
        raise _deny()
    value = row.receipt.get("access_observation")
    if "access_observation" not in row.receipt:
        # Configuration itself completed authenticated native discovery. Use
        # immutable creation time; later receipt/cleanup writes are not access.
        return goals._time(row.created_at.isoformat()), None
    expected = {"schema_version": 1, "kind": "cms_policy_access_observation", "policy_operation_id": saved["operation_id"],
        "policy_digest": saved["policy"]["digest"], "credential_operation_id": saved["policy"]["credential_operation_id"]}
    if (not isinstance(value, dict) or set(value) != FIELDS or
            any(value[key] != item or type(value[key]) is not type(item) for key, item in expected.items()) or
            value["digest"] != digest({key: item for key, item in value.items() if key != "digest"})):
        raise _deny()
    at = goals._time(value["checked_at"])
    if at < row.created_at:
        raise _deny()
    return at, deepcopy(value)


def _verify_author(binding, template, read):
    if binding["platform"] == "wordpress":
        author = read(resource="users", identifier="me", query={"context": "edit"})
        if (not isinstance(author, dict) or not policies._identifier(author.get("id"), "wordpress") or
                not isinstance(author.get("capabilities"), dict)):
            raise CmsApiError("native_defaults_unavailable")
        if (author["id"] != template["author"] or
                any(author["capabilities"].get(key) is not True for key in ("edit_posts", "publish_posts"))):
            raise CmsApiError("credentials_required")
    else:
        response = read(resource="users", query={"limit": 100, "filter": "status:active", "order": "id asc"})
        authors = response.get("users") if isinstance(response, dict) else None
        if (not isinstance(authors, list) or not 0 < len(authors) <= 100 or
                not set(template["authors"]) <= {row["id"] for row in authors if isinstance(row, dict) and
                    policies._identifier(row.get("id"), "ghost") and row.get("status") == "active"}):
            raise CmsApiError("native_defaults_unavailable")


def recheck(dispatch):
    """Reuse recent actual discovery or GET the unchanged native author again.

    The private setup lease/hold and exact current policy fence every request
    and commit. No author/category/tag, policy preference or revision changes.
    """
    from . import cms_setup_authority as authority
    from . import cms_setup_billing as billing
    from .cms_setup_controller import reserve_provider_read
    from .website_connections import require_unlocked_remote_call
    from .website_models import WebsiteConnectionOperation
    from organizations.models import Organization
    if not isinstance(dispatch, authority.CmsSetupDispatch):
        raise _deny()

    def guard(expected=None):
        candidate = WebsiteConnectionOperation.objects.select_related("connection__organization").filter(
            pk=dispatch.operation_id, action=authority.ACTION).first()
        if candidate is None:
            raise _deny()
        # The original spend is row-locked by require_held. Its owner, lease
        # and policy must be checked in the same short owning transaction.
        # Each guard returns before the credential-bearing GET is made.
        with transaction.atomic():
            Organization.objects.select_for_update().get(pk=candidate.connection.organization_id)
            parent = WebsiteConnectionOperation.objects.select_for_update().filter(
                pk=dispatch.operation_id, action=authority.ACTION).first()
            if parent is None:
                raise _deny()
            value = authority.current(parent, lease_id=dispatch.lease_id)
            billing.require_held(parent)
            saved = value.policy
            if saved is None or expected is not None and saved != expected:
                raise _deny()
            binding, credential = current_credential(value.context, value.config, value.connection, user=value.user)
            if binding != saved["policy"]["binding"]:
                raise _deny()
            row = WebsiteConnectionOperation.objects.filter(pk=saved["operation_id"], action=policies.ACTION,
                connection_id=parent.connection_id, generation=parent.generation, state="completed").first()
            at, _observed = observation(row, saved)
            if at > timezone.now() + timedelta(seconds=30):
                raise _deny()
            return parent, value, saved, row, at, binding, credential

    parent, _value, saved, _row, at, binding, _secret = guard()
    if at >= timezone.now() - MAX_SETUP_AGE:
        return at

    def read(**options):
        _parent, _value, _saved, _row, _at, current, secret = guard(saved)
        reserve_provider_read(dispatch)
        # The budget write or a concurrent revocation must not leave a stale
        # secret admitted to HTTP. Recheck after reserving the real allowance.
        _parent, _value, _saved, _row, _at, current, secret = guard(saved)
        require_unlocked_remote_call()
        result, _pagination = request_json(current, secret, method="GET", **options)
        guard(saved)
        return result

    try:
        _verify_author(binding, saved["policy"]["native_template"], read)
    except CmsApiError as exc:
        if exc.category in {"transient", "origin_unavailable"}:
            raise WebsiteAuthorityError("website_prepare_dependency_transient", "The CMS access check is temporarily unavailable.",
                retryable=True, status=503) from None
        raise WebsiteAuthorityError("missing_or_expired_credentials" if exc.category == "credentials_required" else "publishing_adapter_required",
            "The original CMS author could not be verified for publishing.") from None

    with transaction.atomic():
        Organization.objects.select_for_update().get(pk=parent.connection.organization_id)
        parent, _value, _saved, _row, _at, _binding, _secret = guard(saved)
        row = WebsiteConnectionOperation.objects.select_for_update().filter(pk=saved["operation_id"], action=policies.ACTION,
            connection_id=parent.connection_id, generation=parent.generation, state="completed").first()
        observation(row, saved)
        at = timezone.now()
        value = {"schema_version": 1, "kind": "cms_policy_access_observation", "policy_operation_id": saved["operation_id"],
            "policy_digest": saved["policy"]["digest"], "credential_operation_id": saved["policy"]["credential_operation_id"],
            "checked_at": at.isoformat()}
        row.receipt = {**row.receipt, "access_observation": {**value, "digest": digest(value)}}
        row.save(update_fields=["receipt", "updated_at"])
        return observation(row, saved)[0]
