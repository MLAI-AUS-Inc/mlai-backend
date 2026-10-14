"""Owner answers sealed to one reserved goal; callbacks can never manufacture consent."""
from copy import deepcopy
from django.conf import settings
from . import website_goal as goals
from .website_contract import WebsiteAuthorityError
from .website_article_page import choose_page, route


def enabled(organization_id):
    """Consent rollout shares the server-owned goal enrollment boundary."""
    return getattr(settings,"ARTICLES_SETUP_CONSENT_ENABLED",False) and goals.enrolled(organization_id)


def required_grants(*, channel="repository", ci_mode="hosted", strategy=None):
    """Compute the complete selected-channel grant set before dispatching writes."""
    if strategy is not None and (not isinstance(strategy,str) or strategy not in {"adopt","native_collection","deterministic","model_adapter","agent","hosted"}):
        raise ValueError("Unknown articles strategy")
    if strategy == "hosted":channel = "hosted"
    if not isinstance(channel,str) or not isinstance(ci_mode,str) or channel not in {"repository","hosted","wordpress","ghost"} or ci_mode not in {"hosted","repository"}:
        raise ValueError("Unknown articles channel or verification mode")
    if channel!="repository":return {}
    result={"contents":"write","pull_requests":"write"}
    if ci_mode=="repository":result.update(workflows="write",checks="read",statuses="read")
    return result


def answers(value, *, expected_points):
    """Reject partial/coerced answers and changed price before reserving any work."""
    value=goals.mapping(value)
    billing=goals.mapping(value.get("billing"))
    keys={"version","publish_mode","page_policy","page_mode","page_route","ci_mode","billing"}
    if (set(value)!=keys or type(value.get("version")) is not int or value["version"]!=1
        or not isinstance(value.get("publish_mode"),str) or value["publish_mode"] not in {"auto","review"}
        or not isinstance(value.get("page_policy"),str) or value["page_policy"] not in {"auto","pick","explicit"}
        or not isinstance(value.get("page_mode"),str) or value["page_mode"] not in {"existing","none"}
        or not route(value.get("page_route"))
        or not isinstance(value.get("ci_mode"),str) or value["ci_mode"] not in {"hosted","repository"}
        or set(billing)!={"policy","expected_points","accepted"} or billing.get("policy")!="reserved"
        or type(billing.get("expected_points")) is not int or billing["expected_points"]!=expected_points
        or billing.get("accepted") is not True):
        raise WebsiteAuthorityError("website_prepare_blocked","Review the setup choices and current article price before starting.",status=422)
    return {**deepcopy(value),"page_route":route(value["page_route"])}


def seal(goal, value, *, user_id, now):
    """Only the owner start path calls this with validated answers and trusted authority."""
    content={"version":1,"goal_id":goal["goal_id"],"scope":deepcopy(goal["scope"]),"owner_user_id":str(user_id),
        "accepted_at":now.isoformat(),"answers":deepcopy(value)}
    return {**content,"digest":goals.digest(content)}


def valid(goal):
    """Require an exact original goal/scope digest and complete typed answer set."""
    consent=goals.mapping(goals.mapping(goal).get("consent"))
    value=goals.mapping(consent.get("answers"));billing=goals.mapping(value.get("billing"))
    if (set(consent)!={"version","goal_id","scope","owner_user_id","accepted_at","answers","digest"}
        or type(consent.get("version")) is not int or consent["version"]!=1
        or consent.get("goal_id")!=goal.get("goal_id") or not goals.scope_matches(consent.get("scope"),goal.get("scope"))
        or not isinstance(consent.get("owner_user_id"),str) or not consent["owner_user_id"] or len(consent["owner_user_id"])>100
        or not goals.stamp(consent.get("accepted_at")) or type(billing.get("expected_points")) is not int or billing["expected_points"]<0):return False
    try:
        return answers(value,expected_points=billing["expected_points"])==value and consent["digest"]==goals.digest({key:value for key,value in consent.items() if key!="digest"})
    except (ValueError,TypeError,OverflowError):return False


def rebind_trusted_source(goal, new_scope):
    """After B6 verifies the source-only revision chain, rebind without changing answers."""
    if not valid(goal):return None
    result=deepcopy(goal["consent"]);result["scope"]=deepcopy(new_scope)
    result["digest"]=goals.digest({key:value for key,value in result.items() if key!="digest"})
    return result


def setup_plan(context, config, journey):
    """A bounded local plan; grant evidence is already in the read-only journey."""
    connection=config.website_connection
    if connection is None or not enabled(connection.organization_id):return None
    from .billing import get_content_factory_article_cost_points
    selection=choose_page(goals.mapping(journey).get("discovery"))
    grants=goals.mapping(goals.mapping(journey).get("githubPermissions"))
    ci_mode="hosted" if getattr(settings,"ARTICLES_HOSTED_ATTESTATION_ENABLED",False) else "repository"
    required=required_grants(ci_mode=ci_mode)
    url=grants.get("settingsUrl")
    from urllib.parse import urlsplit
    try:
        parsed=urlsplit(url) if isinstance(url,str) else None
        url=url if parsed and parsed.scheme=="https" and parsed.hostname=="github.com" and not parsed.username and not parsed.password and not parsed.query and not parsed.fragment else None
    except ValueError:url=None
    return {"version":1,"companyId":str(context.company.pk),"connectionId":str(connection.pk),"connectionGeneration":connection.generation,
        "configurationRevision":connection.configuration_version,"goalTarget":"first_article_live" if getattr(settings,"ARTICLES_FIRST_ARTICLE_GOAL_ENABLED",False) else "ready",
        "articlePoints":get_content_factory_article_cost_points(context.organization.domain),"page":selection,
        "github":{"approvalUrl":url,"required":required,"missing":[name for name in required if grants.get(name) is not True],"lookupUnavailable":grants.get("retryable") is True},
        "ciMode":ci_mode}
