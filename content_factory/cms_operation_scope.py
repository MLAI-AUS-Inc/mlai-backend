"""Fresh backend-owned CMS operation scope, without granting an HTTP lease."""

from types import SimpleNamespace

from django.contrib.auth import get_user_model

from .cms_connection_authority import validate_current
from .cms_connection_contract import validate_binding, CmsContractError
from .website_contract import WebsiteAuthorityError


def current_scope(operation, *, lease_id=None):
    """Reload the current company/owner/binding and this original reservation.

    Call immediately before each provider request and again before persisting
    its result. Provider JSON and owner parameters cannot supply this context.
    """
    from founder_tools.models import VibeRaisingCompany
    from .models import OrganizationContentConfig
    payload = operation.payload if isinstance(operation.payload, dict) else {}
    try:
        binding = validate_binding(payload.get("binding"))
        if (operation.state not in {"pending", "running"} or operation.generation != binding["generation"] or
                str(operation.connection_id) != binding["connection_id"] or
                lease_id is not None and (operation.state != "running" or payload.get("lease_id") != lease_id)):
            raise CmsContractError("CMS operation changed")
        config = OrganizationContentConfig.objects.select_related("website_connection__organization").filter(
            organization_id=binding["organization_id"], website_connection_id=binding["connection_id"]).first()
        company = VibeRaisingCompany.objects.select_related("profile").filter(pk=binding["company_id"], organization_id=binding["organization_id"]).first()
        user = get_user_model().objects.filter(pk=binding["owner_user_id"], is_active=True).first()
        if config is None or company is None or user is None:
            raise CmsContractError("CMS owner changed")
        context = SimpleNamespace(company=company, profile=company.profile, organization=config.website_connection.organization)
        if (validate_current(context, config, config.website_connection, user=user) != binding or
                (config.website_connection.capabilities or {}).get("_cms_credential_operation_id") != payload.get("credential_operation_id")):
            raise CmsContractError("CMS credential changed")
        return SimpleNamespace(context=context, config=config, connection=config.website_connection, user=user, binding=binding)
    except (CmsContractError, ValueError, TypeError, AttributeError):
        raise WebsiteAuthorityError("website_connection_changed", "The CMS owner, connection or credential changed.") from None
