"""Durable first-article resume intents; payment and failure policy stay shared."""
from datetime import timedelta
from uuid import UUID

from django.conf import settings

from . import website_goal as goals,website_goal_budget as budget
from .website_contract import WebsiteAuthorityError

LOCATOR_FIELDS={"run_id","operation_id","operation_attempt","source_sha","generation","state_version",
    "failure_occurrence_id","failure_fingerprint"}


def valid_plan(plan,goal):
    """Reject unknown or incomplete persisted versions before any remote action."""
    from .website_first_article import _immutable_binding
    try:
        return bool(isinstance(plan,dict) and set(plan)=={"version","state","observation","goal","id"}
            and type(plan.get("version")) is int and plan["version"]==1
            and isinstance(plan.get("state"),str) and plan["state"] in {"pending","dispatching","submitted"}
            and set(goals.mapping(plan.get("observation")))==LOCATOR_FIELDS
            and plan.get("goal")==_immutable_binding(goal)
            and plan.get("id")==goals.digest({"goal_id":goal["goal_id"],"observation":plan["observation"]}))
    except (ValueError,TypeError,KeyError):
        return False


def enabled(op):
    """Require the new rollout, enrolled goal, B1 policy and B4 budgets."""
    from .website_failure_scope import taxonomy_enabled
    from .website_setup_consent import valid as valid_consent
    goal=goals.mapping(op.receipt.get("goal"))
    return bool(getattr(settings,"ARTICLES_ARTICLE_RECOVERY_ENABLED",False) and budget.enabled()
        and getattr(settings,"ARTICLES_FIRST_ARTICLE_GOAL_ENABLED",False)
        and taxonomy_enabled(op.connection.organization_id) and goals.enrolled(op.connection.organization_id)
        and op.state in {"pending","running","needs_user","needs_attention"}
        and op.connection.state=="connected" and op.generation==op.connection.generation
        and goal.get("goal_id")==str(op.pk) and goal.get("target")=="first_article_live"
        and goals.valid_goal(goal,goals.scope_for(op.connection,op.payload.get("company_id"))) and valid_consent(goal))


def locator(run):
    """Bind a causal observation to its original run and reserved attempt."""
    from .website_failure_contract import validate_failure,FailureContractError
    result=goals.mapping(run.result);request=goals.mapping(run.run_request)
    try:
        failure=validate_failure(result.get("failure"))
        occurrence=result.get("failure_occurrence_id")
        UUID(occurrence)
    except (FailureContractError,ValueError,TypeError,AttributeError):
        return None
    identity={"run_id":run.run_id,"operation_id":request.get("operation_id"),
        "operation_attempt":request.get("operation_attempt"),"source_sha":request.get("expected_source_sha"),
        "generation":result.get("generation"),"state_version":result.get("state_version"),
        "failure_occurrence_id":occurrence,"failure_fingerprint":failure["failure_fingerprint"]}
    if (not isinstance(identity["operation_id"],str) or not identity["operation_id"]
        or type(identity["operation_attempt"]) is not int or identity["operation_attempt"]<1
        or not isinstance(identity["source_sha"],str) or not goals.SHA.fullmatch(identity["source_sha"])
        or any(type(identity[key]) is not int or identity[key]<0 for key in ("generation","state_version"))):
        return None
    return identity


def observe(op,run):
    """Journal only a failure already selected through the owning controller."""
    if not enabled(op):return
    value=locator(run)
    if value is not None:
        from .website_first_article import _save_fields
        _save_fields(op,{"failed_article_run":value})


def schedule(op,goal,*,classification,fresh,occurrence,now):
    """Replace a failed-run polling loop with one bounded canonical resume intent."""
    if not enabled(op) or op.receipt.get("current_step")!="first-article" or classification not in {"transient","repairable"}:
        return False
    observed=goals.mapping(op.receipt.get("failed_article_run"))
    if (observed.get("failure_occurrence_id")!=occurrence or observed.get("source_sha")!=goal.get("current_source_sha")
        or observed.get("run_id") not in {op.receipt.get("first_article_run_id"),op.receipt.get("first_article_publish_run_id")}):
        return False
    previous=goals.mapping(op.receipt.get("article_recovery"))
    if not fresh:
        if not valid_plan(previous,goal) or previous.get("observation")!=observed:
            return False
        # Another delivery of the same failure must preserve the scheduled
        # repair instead of restoring prepare_failure's temporary attention
        # state or consuming another class/budget occurrence.
        op.receipt["goal"]=goal
        op.state="pending"
        op.next_attempt_at=max(op.next_attempt_at or now,now+timedelta(minutes=1))
        op.receipt.update(status="working",user_action=None)
        return True
    from .website_first_article import _immutable_binding
    op.receipt["article_recovery"]={"version":1,"state":"pending","observation":observed,
        "goal":_immutable_binding(goal),"id":goals.digest({"goal_id":goal["goal_id"],"observation":observed})}
    for key in ("first_article_approval","first_article_live_receipt"):
        op.receipt.pop(key,None)
    proofs={item["name"]:item["proof"] for item in goal["milestones"]
        if item["name"] not in {"first_draft","first_article_live"} and item.get("proof")}
    op.receipt["goal"]=goals.refresh_goal(goal,proofs,scope=goal["scope"],source_sha=goal.get("current_source_sha"),now=now)
    op.state="pending"
    op.next_attempt_at=max(op.next_attempt_at or now,now+timedelta(minutes=1))
    op.receipt.update(status="working",user_action=None,message="Repairing the saved article from its failed check.")
    return True


def resume(op,run,context,*,user,now):
    """Retransmit an uncertain canonical resume with its original pending attempt."""
    if not enabled(op):return False
    plan=goals.mapping(op.receipt.get("article_recovery"));current=locator(run)
    if not plan or current is None:return False
    if not valid_plan(plan,op.receipt["goal"]):
        raise WebsiteAuthorityError("website_operation_changed","The saved article recovery intent is invalid.")
    from .website_first_article import _immutable_binding,_save_fields,_check_response,_wait
    observed=goals.mapping(plan.get("observation"))
    if observed.get("source_sha")!=op.receipt["goal"].get("current_source_sha"):
        raise WebsiteAuthorityError("website_source_changed","The website source changed before the article repair.")
    if plan.get("goal")!=_immutable_binding(op.receipt["goal"]) or any(
        current.get(key)!=value for key,value in observed.items() if key!="operation_attempt"):
        return False
    attempt=observed.get("operation_attempt")
    if (type(attempt) is not int or current["operation_attempt"] not in
        ({attempt} if plan.get("state")=="pending" else {attempt,attempt+1}) or plan.get("state") not in {"pending","dispatching","submitted"}):
        raise WebsiteAuthorityError("website_operation_changed","The saved article recovery attempt changed.")
    if plan["state"]=="submitted":
        _wait(op,now,"Waiting for the saved article repair to report its current outcome.")
        return True
    reason=budget.exhaustion(op.receipt["goal"],now=now,action="first-article-generation")
    if reason:
        from .website_goal_runner import stop_goal
        stop_goal(op,op.receipt["goal"],reason=reason,now=now)
        return True
    plan={**plan,"state":"dispatching"}
    _save_fields(op,{"article_recovery":plan},remove=("first_article_approval","first_article_live_receipt"))
    from .vibe_marketing_views import control_article_run
    response=control_article_run(user=user,context=context,run=run,action="resume",data={},goal_operation_id=op.pk)
    _check_response(response)
    _save_fields(op,{"article_recovery":{**plan,"state":"submitted"}})
    _wait(op,now,"Resuming the saved article from its failed check.")
    return True


def authorize_resume(identifier,*,run,context,user,action):
    """The private control selector cannot be supplied by HTTP/MCP request data."""
    from .website_models import WebsiteConnectionOperation
    op=WebsiteConnectionOperation.objects.select_related("connection__organization").filter(pk=identifier,action="prepare").first()
    request=goals.mapping(run.run_request)
    if op is None:
        raise WebsiteAuthorityError("website_operation_changed","The original article recovery goal is unavailable.")
    plan=goals.mapping(op.receipt.get("article_recovery"))
    latest=op.connection.operations.filter(generation=op.generation,action="prepare").order_by("-created_at","-pk").first()
    if (action!="resume" or not enabled(op) or op.state!="running" or latest is None or latest.pk!=op.pk
        or op.connection.organization_id!=context.organization.pk or str(op.payload.get("company_id"))!=str(context.company.pk)
        or str(op.payload.get("requested_by_user_id"))!=str(user.pk) or not getattr(user,"is_active",False)
        or plan.get("state")!="dispatching" or not valid_plan(plan,op.receipt["goal"])
        or goals.mapping(plan.get("observation")).get("run_id")!=run.run_id
        or request.get("first_article_goal_operation_id")!=str(op.pk) or request.get("first_article_goal_id")!=str(op.pk)
        or request.get("website_connection_id")!=str(op.connection_id) or request.get("connection_generation")!=op.generation
        or run.organization_id!=op.connection.organization_id
        or run.run_id not in {op.receipt.get("first_article_run_id"),op.receipt.get("first_article_publish_run_id")}):
        raise WebsiteAuthorityError("website_operation_changed","The saved article recovery authority changed.")
