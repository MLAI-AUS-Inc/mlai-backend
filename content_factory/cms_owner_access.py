"""Owner-only destinations and actual connection acknowledgements, never keys."""

from copy import deepcopy
from uuid import UUID

from django.core.exceptions import ObjectDoesNotExist

from . import cms_credential_renewal as renewal
from .cms_connection_authority import validate_current, current_credential
from .website_contract import WebsiteAuthorityError
from .website_publishing_channels import saved_record, scope_for
from .cms_access_contract import wait_epoch


def _deny():
    return WebsiteAuthorityError("website_connection_changed", "Refresh the original CMS access request.")


def _uuid(value):
    return isinstance(value, str) and str(UUID(value)) == value


def _refresh(context, config, user):
    for row in (context.organization, context.profile, context.company, user, config): row.refresh_from_db()
    if config.website_connection is None:
        raise _deny()
    config.website_connection.refresh_from_db()


def _receipt(context, config, user):
    binding, _private = current_credential(context, config, config.website_connection, user=user)
    request_id = config.website_connection.capabilities.get("_cms_connect_request_id")
    if request_id is not None and not _uuid(request_id):
        raise _deny()
    return {"version": 1, "companyId": binding["company_id"], "connectionId": binding["connection_id"],
        "connectionGeneration": binding["generation"], "requestId": request_id}


def connection_receipt(context, config, *, user):
    """Return the actual accepted owner nonce under the current selected scope."""
    try:
        _refresh(context, config, user)
        first = _receipt(context, config, user)
        _refresh(context, config, user)
        if _receipt(context, config, user) != first:
            raise _deny()
        return first
    except (ObjectDoesNotExist, ValueError, TypeError, KeyError, AttributeError, RecursionError):
        raise _deny() from None


def for_operation(context, config, *, user, operation_id):
    """Read only this site's actual native access wait and credential destination.

    The operation ID locates an already authorized private record; it never
    supplies consent. No provider request, key export, scheduling or write runs.
    """
    from .website_models import WebsiteConnectionOperation
    from . import cms_goal_authority as native
    from . import cms_setup_authority as setup
    try:
        if not _uuid(operation_id):
            raise _deny()
        _refresh(context, config, user)
        binding = validate_current(context, config, config.website_connection, user=user)
        operation = WebsiteConnectionOperation.objects.filter(pk=operation_id, action__in={native.ACTION, setup.ACTION},
            connection_id=config.website_connection.pk, generation=binding["generation"], payload__company_id=binding["company_id"]).first()
        if operation is None:
            raise _deny()
        value = (native if operation.action == native.ACTION else setup).current(operation, user=user, states={operation.state})
        expected = value.scope["binding"] if operation.action == native.ACTION else value.binding
        if expected != binding:
            raise _deny()
        selected = renewal.candidate(context, config, user=user, proposal=binding)
        if selected is None:
            raise _deny()
        record = saved_record(config.article_system)
        scope = scope_for(context, config)
        if (record is None or record["scope"] != scope or not record["selection"] or
                record["selection"]["channel"] != "cms" or record["selection"]["platform"] not in {"unknown", binding["platform"]}):
            raise _deny()
        epoch = wait_epoch(operation)
        waiting = epoch is not None
        receipt = _receipt(context, config, user)
        form = {"version": 1, "companyId": binding["company_id"], "connectionId": binding["connection_id"],
            "connectionGeneration": binding["generation"], "configurationRevision": config.website_connection.configuration_version,
            "operationId": str(operation.pk), "platform": binding["platform"], "publicOrigin": binding["public_origin"],
            "apiOrigin": binding["api_origin"], "channelScope": deepcopy(scope), "channelRevision": record["revision"],
            "accessEpoch": epoch} if waiting else None
        _refresh(context, config, user)
        operation.refresh_from_db()
        if (validate_current(context, config, config.website_connection, user=user) != binding or
                renewal.candidate(context, config, user=user, proposal=binding) != selected or
                saved_record(config.article_system) != record or _receipt(context, config, user) != receipt or
                epoch != wait_epoch(operation)):
            raise _deny()
        from .cms_access_status import for_root
        progress = for_root(operation)
        if progress is not None and progress["state"] in {"pending", "running"}: form = None
        from .cms_access_authority import enabled
        if not enabled(): form = None
        return {"cmsAccess": form, "cmsConnectionReceipt": receipt,
            **({"cmsAccessOperation": progress} if progress is not None else {})}
    except (ObjectDoesNotExist, ValueError, TypeError, KeyError, AttributeError, RecursionError):
        raise _deny() from None
