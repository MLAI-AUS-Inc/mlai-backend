"""CMS authority is separate from GitHub and from informational channel choices."""

from django.conf import settings

from .cms_connection_contract import validate_binding, open_credential, CmsContractError
from .website_channel_observation import canonical_domain
from .website_contract import WebsiteAuthorityError
from urllib.parse import urlsplit

PRIVATE_CREDENTIAL_ACTION = "channel-credential"


def public_summary(connection, *, company_id=None):
    """Keep provider facts visible without claiming an unfinished live pipeline."""
    from .website_contract import CAPABILITY_KEYS
    try:
        binding = validate_binding((connection.capabilities or {}).get("_cms_binding"))
        platform = binding["platform"]
    except CmsContractError:
        platform = "unknown"
    latest = connection.operations.exclude(action__in=["channel-credential", "cms-artifact", "cms-review", "cms-media", "cms-stage", "cms-policy", "cms-approval", "cms-publish", "cms-goal", "cms-goal-refund", "cms-setup", "cms-setup-intent", "cms-setup-refund", "cms-credential-renewal", "goal-usage", "goal-alert", "usage-refresh"]).only("id", "action", "state", "receipt").order_by("-created_at").first()
    return {"channel": "cms", "platform": platform, "connectionId": str(connection.pk),
        "connectionGeneration": connection.generation, "companyId": str(company_id) if company_id else None,
        "repositoryId": None, "githubRepo": "", "appRoot": "", "branch": "", "siteUrl": connection.site_url,
        "status": connection.state, "capabilities": {key: False for key in CAPABILITY_KEYS},
        "allowedActions": ["cms-connect", "cms-configure", "disconnect"] if connection.state == "connected" else ["cms-connect", "disconnect"] if connection.state == "paused" else ["cms-connect"],
        "blockers": [], "verifiedSha": None, "scannedSha": None, "observedSha": None,
        "configurationVersion": connection.configuration_version,
        "latestOperation": {"id": str(latest.pk), "action": latest.action, "state": latest.state, "receipt": latest.receipt} if latest else None}


def enabled():
    """Require independent execution and CMS rollouts before credential use."""
    from .publishing_channels import enabled as channels_enabled
    return channels_enabled() and getattr(settings, "ARTICLES_CMS_CHANNEL_ENABLED", False)


def validate_current(context, config, connection, *, user):
    """Require the same current founder/company/provider generation, without HTTP."""
    if not enabled():
        raise WebsiteAuthorityError("publishing_adapter_required", "CMS publishing is not available.")
    try:
        binding = validate_binding((connection.capabilities or {}).get("_cms_binding"))
        company_domain = canonical_domain(context.company.domain).removeprefix("www.")
        organization_domain = canonical_domain(context.organization.domain).removeprefix("www.")
        public_domain = urlsplit(binding["public_origin"]).hostname.removeprefix("www.")
    except (CmsContractError, ValueError, AttributeError):
        raise WebsiteAuthorityError("website_configuration_changed", "Refresh the current CMS connection.") from None
    if (connection.state != "connected" or str(connection.organization_id) != binding["organization_id"] or
            connection.github_repo or connection.repository_id is not None or
            connection.installation_id or connection.branch or connection.app_root or
            str(config.website_connection_id) != str(connection.pk) or
            connection.generation != binding["generation"] or str(connection.pk) != binding["connection_id"] or
            str(config.organization_id) != binding["organization_id"] or str(context.organization.pk) != binding["organization_id"] or
            str(context.company.organization_id) != binding["organization_id"] or str(context.company.pk) != binding["company_id"] or
            str(context.company.profile_id) != str(context.profile.pk) or context.profile.role != "founder" or
            str(context.profile.user_id) != binding["owner_user_id"] or str(getattr(user, "pk", "")) != binding["owner_user_id"] or
            getattr(user, "is_active", False) is not True or str(connection.authorized_by_id) != binding["owner_user_id"] or
            company_domain != organization_domain or company_domain != public_domain or connection.site_url != binding["public_origin"]):
        raise WebsiteAuthorityError("website_connection_changed", "The CMS connection, owner or website changed.")
    return binding


def current_credential(context, config, connection, *, user):
    """Load only this current binding's private encrypted operation, never a default.

    Callers must recheck immediately before every provider action and fence their
    own result after HTTP. This read does not grant a transferable authority lease.
    """
    binding = validate_current(context, config, connection, user=user)
    from uuid import UUID
    reference = (connection.capabilities or {}).get("_cms_credential_operation_id")
    try:
        if not isinstance(reference, str) or str(UUID(reference)) != reference:
            raise ValueError("Invalid reference")
    except (ValueError, TypeError, AttributeError):
        raise WebsiteAuthorityError("missing_or_expired_credentials", "Reconnect the CMS with its current application credential.") from None
    operation = connection.operations.filter(pk=reference, action=PRIVATE_CREDENTIAL_ACTION,
        generation=connection.generation, state="completed").first()
    payload = operation.payload if operation and isinstance(operation.payload, dict) else {}
    try:
        if (operation is None or operation.action != PRIVATE_CREDENTIAL_ACTION or operation.state != "completed" or
                operation.generation != connection.generation or str(operation.connection_id) != str(connection.pk) or
                set(payload) != {"schema_version", "binding", "sealed_credential"} or
                type(payload.get("schema_version")) is not int or payload["schema_version"] != 1 or
                validate_binding(payload["binding"]) != binding):
            raise CmsContractError("CMS credential reference changed")
        return binding, open_credential(payload["sealed_credential"], binding=binding)
    except (CmsContractError, KeyError):
        raise WebsiteAuthorityError("missing_or_expired_credentials", "Reconnect the CMS with its current application credential.") from None
