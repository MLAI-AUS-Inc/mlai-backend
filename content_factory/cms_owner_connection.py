"""Explicit owner-only CMS consent, with provider reads outside database locks."""

from copy import deepcopy
import hmac
from types import SimpleNamespace
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from django.core.exceptions import ImproperlyConfigured
from django.db import transaction

from .cms_api_transport import CmsApiError
from .cms_connection_authority import enabled, current_credential, PRIVATE_CREDENTIAL_ACTION
from .cms_connection_contract import canonical_origin, validate_binding, seal_credential, open_credential, CmsContractError
from .cms_provider_identity import inspect_identity
from .website_contract import WebsiteAuthorityError
from .website_publishing_channels import _require_current, saved_record, scope_for


def _owner(context, config, user):
    if (str(context.profile.user_id) != str(user.pk) or getattr(user, "is_active", False) is not True or
            context.profile.role != "founder" or str(context.company.profile_id) != str(context.profile.pk)):
        raise WebsiteAuthorityError("website_scope_mismatch", "Choose your current founder company.")
    return scope_for(context, config)


def _request_id(data):
    value = data.get("client_request_id")
    try:
        if not isinstance(value, str) or str(UUID(value)) != value:
            raise ValueError("Invalid request ID")
    except (ValueError, TypeError, AttributeError):
        raise WebsiteAuthorityError("invalid_operation_key", "A current CMS connection request ID is required.", status=422) from None
    return value


def _proposal(context, config, user, data):
    scope = _owner(context, config, user)
    if data.get("credential_origin_confirmed") is not True:
        raise WebsiteAuthorityError("website_prepare_blocked", "Confirm the CMS credential destination.", status=422)
    try:
        public = canonical_origin(data.get("public_origin"))
        api = canonical_origin(data.get("api_origin"))
        if (len(public) > 512 or urlsplit(public).hostname.removeprefix("www.") != scope["domain"].removeprefix("www.")):
            raise CmsContractError("CMS website changed")
        binding = validate_binding({"schema_version": 1, "channel": "cms", "platform": data.get("platform"),
            "company_id": str(context.company.pk), "organization_id": str(config.organization_id),
            "owner_user_id": str(user.pk), "connection_id": str(uuid4()), "generation": 1,
            "public_origin": public, "api_origin": api})
        sealed = seal_credential(binding, username=data.get("username", ""), secret=data.get("secret"))
        return binding, sealed, open_credential(sealed, binding=binding)
    except (CmsContractError, ImproperlyConfigured):
        raise WebsiteAuthorityError("missing_or_expired_credentials", "Check the CMS origins and application or integration credential.", status=422) from None


def _existing(context, config, user, request_id, binding, credential):
    connection = config.website_connection
    if not connection or (connection.capabilities or {}).get("_cms_connect_request_id") != request_id:
        return None
    current, saved = current_credential(context, config, connection, user=user)
    if (any(current[key] != binding[key] for key in ("platform", "public_origin", "api_origin")) or
            not hmac.compare_digest(saved.username.encode(), credential.username.encode()) or
            not hmac.compare_digest(saved.secret.encode(), credential.secret.encode())):
        raise WebsiteAuthorityError("operation_key_conflict", "This CMS connection request has different settings.")
    return connection


def connect(context, config, *, user, data):
    """Verify an explicit owner destination, then commit one encrypted binding.

    A repeated acknowledged request reuses the current receipt. Reconnection
    creates new consent and cannot replay a prior generation's credential.
    """
    if not enabled():
        raise WebsiteAuthorityError("publishing_adapter_required", "CMS connections are not available.", status=404)
    request_id = _request_id(data)
    # Refresh before even read-only authenticated provider traffic. Recheck the
    # same selection and founder after HTTP, while holding the organization lock.
    config.refresh_from_db()
    context.organization.refresh_from_db()
    context.profile.refresh_from_db()
    context.company.refresh_from_db()
    user.refresh_from_db(fields=["is_active"])
    binding, sealed, credential = _proposal(context, config, user, data)
    from .website_models import WebsiteConnectionOperation
    operation_key = f"cms-connect:{config.organization_id}:{context.company.pk}:{request_id}"
    prior = WebsiteConnectionOperation.objects.filter(idempotency_key=operation_key).first()
    if prior and (not config.website_connection or str(prior.connection_id) != str(config.website_connection.pk)):
        raise WebsiteAuthorityError("operation_key_conflict", "Start a new request for this CMS connection.")
    existing = _existing(context, config, user, request_id, binding, credential)
    if existing:
        return existing
    expected_scope, revision = data.get("channel_scope"), data.get("channel_revision")
    _require_current(context, config, expected_scope, revision)
    record = saved_record(config.article_system)
    if not record or not record["selection"] or record["selection"]["channel"] != "cms":
        raise WebsiteAuthorityError("website_prepare_blocked", "Choose the CMS publishing channel first.", status=422)
    if record["selection"]["platform"] not in {"unknown", binding["platform"]}:
        raise WebsiteAuthorityError("website_configuration_changed", "Review the selected CMS platform.")
    from .website_connections import require_unlocked_remote_call
    require_unlocked_remote_call()
    try:
        identity = inspect_identity(binding, credential)
    except CmsApiError as exc:
        if exc.category == "transient":
            raise WebsiteAuthorityError("website_prepare_unavailable", "The CMS is temporarily unavailable. Retry this connection.", retryable=True) from None
        raise WebsiteAuthorityError("missing_or_expired_credentials", "Verify the CMS site and application or integration credential.", status=422) from None
    from organizations.models import Organization
    from founder_tools.models import VibeRaisingCompany
    from .website_models import WebsiteConnection, WebsiteConnectionOperation
    with transaction.atomic():
        Organization.objects.select_for_update().get(pk=config.organization_id)
        config.refresh_from_db()
        context.organization.refresh_from_db()
        context.profile.refresh_from_db()
        company = VibeRaisingCompany.objects.select_for_update().get(pk=context.company.pk)
        user.refresh_from_db(fields=["is_active"])
        locked_context = SimpleNamespace(organization=context.organization, profile=context.profile, company=company)
        _owner(locked_context, config, user)
        # Concurrent copies of the same request can both have read the provider.
        existing = _existing(locked_context, config, user, request_id, binding, credential)
        if existing:
            return existing
        _require_current(locked_context, config, expected_scope, revision)
        record = saved_record(config.article_system)
        if config.website_connection:
            from .website_disconnect import start_disconnect
            from .website_connections import contract_for
            start_disconnect(config, user=user, data={**contract_for(config.website_connection), "remove_setup": False})
        from .website_connections import invalidate_repository_config
        invalidate_repository_config(config)
        connection = WebsiteConnection.objects.create(pk=binding["connection_id"], organization=context.organization,
            authorized_by=user, github_repo="", repository_id=None, installation_id="", branch="", app_root="",
            site_url=binding["public_origin"], capabilities={"_cms_binding": binding,
                "_cms_connect_request_id": request_id, "_cms_identity": identity}, blockers=[])
        private = WebsiteConnectionOperation.objects.create(connection=connection, generation=1,
            action=PRIVATE_CREDENTIAL_ACTION, state="completed", idempotency_key=f"cms-key:{connection.pk}:1",
            payload={"schema_version": 1, "binding": binding, "sealed_credential": sealed}, receipt={})
        connection.capabilities["_cms_credential_operation_id"] = str(private.pk)
        connection.save(update_fields=["capabilities", "updated_at"])
        WebsiteConnectionOperation.objects.create(connection=connection, generation=1, action="cms-connect", state="completed",
            idempotency_key=operation_key, payload={},
            receipt={"status": "connected", "channel": "cms", "platform": binding["platform"],
                "credential_stored": True, "publishing_verified": False})
        config.website_connection = connection
        config.github_repo, config.github_installation_id = "", ""
        config.github_token_encrypted, config.github_refresh_token_encrypted = "", ""
        config.publish_targets, config.default_publish_target_id = [], None
        config.auto_publish, config.daily_discovery_enabled = False, False
        choice = {**deepcopy(record), "scope": scope_for(locked_context, config), "revision": revision + 1}
        config.article_system = {**(config.article_system or {}), "publishing_channel": choice}
        config.save(update_fields=["website_connection", "github_repo", "github_installation_id", "github_token_encrypted",
            "github_refresh_token_encrypted", "publish_targets", "default_publish_target_id", "auto_publish",
            "daily_discovery_enabled", "article_system", "updated_at"])
        return connection
