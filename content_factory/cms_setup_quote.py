"""Read-only native setup choices; a quote never authorizes work or payment."""

from django.conf import settings

from . import cms_goal_authority as goals
from . import cms_publication_policy as policies
from .channel_artifact_contract import digest
from .cms_connection_authority import current_credential
from .website_contract import WebsiteAuthorityError


def _current(context, config, user):
    for item in (context.organization, context.profile, context.company, user, config):
        item.refresh_from_db()
    connection = config.website_connection
    if connection is None:
        raise ValueError("The native setup connection changed")
    connection.refresh_from_db()
    if type(connection.configuration_version) is not int or connection.configuration_version < 1:
        raise ValueError("The native setup configuration changed")
    binding, _credential = current_credential(context, config, connection, user=user)
    policy = policies.current_policy(context, config, user=user)
    if policy is not None and policy["policy"]["binding"] != binding:
        raise ValueError("The native setup policy changed")
    return binding, connection.configuration_version, policy


def for_context(context, config, *, user, expected_binding):
    """Quote this current owner/site without provider reads, scheduling or a hold.

    The digest identifies displayed public choices. It is not signed consent;
    the eventual setup coordinator must reload authority and validate acceptance.
    """
    if (not getattr(settings, "ARTICLES_CMS_STATUS_ENABLED", False) or user is None or
            not goals.enabled(context.organization.pk)):
        return None
    try:
        from .billing import get_content_factory_article_cost_points
        binding, revision, policy = _current(context, config, user)
        if binding != expected_binding or config.website_connection.operations.filter(action=goals.ACTION,
                generation=binding["generation"], payload__company_id=binding["company_id"]).exists():
            return None
        price = get_content_factory_article_cost_points(context.organization.domain)
        if type(price) is not int or not 0 <= price < 10**9:
            return None
        value = {"version": 1, "kind": "cms_setup_quote", "companyId": binding["company_id"],
            "connectionId": binding["connection_id"], "connectionGeneration": binding["generation"],
            "configurationRevision": revision, "platform": binding["platform"], "goalTarget": "first_article_live",
            "firstArticlePoints": price, "setupPoints": 0, "imagePlacement": "article",
            "policy": policies.projection(policy) if policy is not None else None}
        # Do not return an old owner's choice or a quote whose policy changed
        # while reading its price. This guard still performs no external I/O.
        current = _current(context, config, user)
        identity = lambda item: (item["operation_id"], item["policy"]["digest"]) if item is not None else None
        if (current[0] != binding or current[1] != revision or identity(current[2]) != identity(policy) or
                get_content_factory_article_cost_points(context.organization.domain) != price or
                config.website_connection.operations.filter(action=goals.ACTION, generation=binding["generation"],
                    payload__company_id=binding["company_id"]).exists()):
            return None
        return {**value, "digest": digest(value)}
    except (WebsiteAuthorityError, ValueError, TypeError, KeyError, AttributeError, RecursionError):
        return None
