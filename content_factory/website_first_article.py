"""Continue one consented website goal through its owned first article."""

from datetime import timedelta
from urllib.parse import urlsplit

from django.conf import settings
from django.db import transaction

from . import website_goal as goals
from .article_publish_approval import (RECEIPT_KEY,article_review_identity,
    article_review_identity_is_complete,article_publish_approval_receipt_matches)
from .website_contract import WebsiteAuthorityError
from .website_setup_consent import valid as valid_consent


def active(op):
    """Require the explicit first-article rollout and original sealed consent."""
    goal=goals.mapping(op.receipt.get("goal"))
    return bool(getattr(settings,"ARTICLES_FIRST_ARTICLE_GOAL_ENABLED",False)
        and op.state in {"pending","running","needs_user"} and goal.get("goal_id")==str(op.pk)
        and goals.enrolled(op.connection.organization_id) and goal.get("target")=="first_article_live"
        and goals.valid_goal(goal,goals.scope_for(op.connection,op.payload.get("company_id"))) and valid_consent(goal))


def _save_fields(op, fields, *, remove=()):
    """Journal an intent before effects without changing the prepare claim's goal revision."""
    from .website_connections import authority_guard,contract_for
    from .website_models import WebsiteConnectionOperation
    with authority_guard(contract_for(op.connection),action="read") as connection:
        if not goals.scope_matches(op.receipt["goal"]["scope"],goals.scope_for(connection,op.payload.get("company_id"))):
            raise WebsiteAuthorityError("website_operation_changed","The website changed before its first-article intent was saved.")
        saved=WebsiteConnectionOperation.objects.select_for_update().filter(pk=op.pk,action="prepare",state="running",
            attempts=op.attempts,updated_at=op._claimed_at,receipt__goal__revision=op._claimed_goal_revision).first()
        if saved is None:
            raise WebsiteAuthorityError("website_operation_changed","The article goal changed before its next effect.")
        saved.receipt={**{key:value for key,value in saved.receipt.items() if key not in remove},**fields}
        saved.save(update_fields=["receipt"])
        op.receipt={**{key:value for key,value in op.receipt.items() if key not in remove},**fields}


def article_run(op, config):
    """Recover only the exact tenant/goal/payment-owned generation, including lost replies."""
    from workflow_runs.models import ContentFactoryRun
    from .website_operations import validate_operation,deletion_epoch
    connection=config.website_connection
    billing=goals.mapping(op.receipt.get("first_article_billing"))
    if not billing.get("client_request_id"):
        return None
    query={"organization_id":connection.organization_id,"workflow":"article_generation",
        "run_request__client_request_id":billing["client_request_id"],
        "run_request__first_article_goal_operation_id":str(op.pk)}
    identifier=op.receipt.get("first_article_run_id")
    from .dispatch_models import ContentFactoryDispatchOutbox
    dispatch=ContentFactoryDispatchOutbox.objects.filter(client_request_id=billing["client_request_id"],
        organization_id=connection.organization_id,state="delivered").first()
    if dispatch is not None and dispatch.run_id:
        identifier=dispatch.run_id
    if identifier:query["run_id"]=identifier
    run=ContentFactoryRun.objects.filter(**query).order_by("created_at","pk").first()
    if run is None:
        if identifier:
            raise WebsiteAuthorityError("website_prepare_dependency_transient","Waiting for the original first-article dispatch record.",retryable=True)
        return None
    saved=goals.mapping(run.run_request)
    if (saved.get("first_article_goal_id")!=op.receipt["goal"]["goal_id"]
        or saved.get("website_connection_id")!=str(connection.pk) or saved.get("connection_generation")!=connection.generation
        or saved.get("repository_id")!=connection.repository_id or saved.get("deletion_epoch")!=deletion_epoch(connection)
        or run.domain.casefold()!=connection.organization.domain.casefold() or run.github_repo.casefold()!=connection.github_repo.casefold()
        or str(saved.get("roo_points_ledger_id") or "")!=str(billing.get("spend_ledger_id") or "")
        or str(saved.get("roo_points_billing_user_id"))!=billing.get("payer_user_id")):
        raise WebsiteAuthorityError("website_operation_changed","The saved first article does not match this goal.")
    original=validate_operation(connection,{**saved,"run_id":run.run_id})
    if original is None or original.action!="workflow" or original.payload.get("run_id")!=run.run_id:
        raise WebsiteAuthorityError("website_operation_changed","The first article has no original reserved workflow operation.")
    return run


def _immutable_binding(goal):
    return {"goal_id":goal["goal_id"],"authority_digest":goals.digest({key:value for key,value in goal["scope"].items()
        if key!="configuration_revision"}),"answers_digest":goals.digest(goal["consent"]["answers"])}


def approval_matches(op, run):
    """Require both canonical exact-review approval and the parent goal's saved binding."""
    binding=goals.mapping(op.receipt.get("first_article_approval"))
    return bool(article_publish_approval_receipt_matches(run) and binding.get("goal")==_immutable_binding(op.receipt["goal"])
        and binding.get("review")==article_review_identity(run)
        and binding.get("source_sha")==goals.mapping(run.run_request).get("expected_source_sha")
        and binding.get("approval_digest")==goals.digest(goals.mapping(run.run_request).get(RECEIPT_KEY)))


def goal_auto_merge(runs):
    """Return None for ordinary runs; a goal run cannot fall through to legacy opt-ins."""
    from .website_models import WebsiteConnectionOperation
    for run in runs:
        request=goals.mapping(getattr(run,"run_request",None))
        parent_id=request.get("first_article_goal_operation_id")
        if not parent_id:continue
        op=WebsiteConnectionOperation.objects.select_related("connection__organization").filter(pk=parent_id,action="prepare").first()
        if op is None or op.state not in {"pending","running","needs_user"} or not active(op):return False
        latest=op.connection.operations.filter(generation=op.generation,action="prepare").order_by("-created_at","-pk").first()
        if (latest is None or latest.pk!=op.pk or run.organization_id!=op.connection.organization_id
            or request.get("website_connection_id")!=str(op.connection.pk)
            or request.get("connection_generation")!=op.generation):return False
        source=next((candidate for candidate in runs if getattr(candidate,"run_id",None)==op.receipt.get("first_article_run_id")),None)
        if source is None:
            from workflow_runs.models import ContentFactoryRun
            source=ContentFactoryRun.objects.filter(run_id=op.receipt.get("first_article_run_id"),organization_id=op.connection.organization_id).first()
        return source is not None and approval_matches(op,source)
    return None


def merged_source(op, run, context):
    """Accept a source change only from this article's backend-owned merge intent."""
    from .vibe_marketing_views import _publish_child_for_run_state,_publish_source_run_for_child
    child=_publish_child_for_run_state(run,context=context)
    if child is None:return None
    source=_publish_source_run_for_child(child,context)
    if source is None or source.run_id!=run.run_id:return None
    result=goals.mapping(child.result);intent=goals.mapping(result.get("publish_merge_intent"));response=goals.mapping(result.get("merge_response"))
    connection=op.connection
    sha=response.get("sha")
    if (result.get("merge_status")!="merged" or response.get("merged") is not True
        or not isinstance(sha,str) or not goals.SHA.fullmatch(sha)
        or intent.get("run_id")!=child.run_id or intent.get("website_connection_id")!=str(connection.pk)
        or intent.get("connection_generation")!=connection.generation or intent.get("repository_id")!=connection.repository_id
        or intent.get("base_branch")!=connection.branch or intent.get("source_sha")!=goals.mapping(run.run_request).get("expected_source_sha")
        or not isinstance(intent.get("head_sha"),str) or not goals.SHA.fullmatch(intent["head_sha"])):
        return None
    return {"source_sha":sha,"run_id":child.run_id,"intent":intent,"response":response}


def observe_manual_merge(op, child, *, now):
    """Read the exact owned PR after an external reviewer merges it; never issue a merge."""
    from .website_connections import authority_guard,contract_for,require_unlocked_remote_call
    from integrations.services.github_app import create_installation_access_token
    from integrations import http_client
    from workflow_runs.models import ContentFactoryRun
    intent=goals.mapping(goals.mapping(child.result).get("publish_merge_intent"))
    number=intent.get("pr_number")
    if type(number) is not int or number<=0:return False
    connection=op.connection;credential=None;headers={}
    binding=contract_for(connection)
    try:
        with authority_guard(binding,action="read"):pass
        require_unlocked_remote_call()
        credential=create_installation_access_token(installation_id=connection.installation_id,repository=connection.github_repo,
            repository_id=connection.repository_id,permission_mode="read",use_cache=False)
        headers={"Authorization":f"Bearer {credential.token}","Accept":"application/vnd.github+json"}
        response=http_client.get(f"https://api.github.com/repos/{connection.github_repo}/pulls/{number}",headers=headers,timeout=(3,10))
        response.raise_for_status();pull=response.json()
        head=goals.mapping(pull.get("head"));base=goals.mapping(pull.get("base"));sha=pull.get("merge_commit_sha")
        if (pull.get("merged") is not True or pull.get("number")!=number or not isinstance(sha,str) or not goals.SHA.fullmatch(sha)
            or intent.get("website_connection_id")!=str(connection.pk) or intent.get("connection_generation")!=connection.generation
            or intent.get("repository_id")!=connection.repository_id or intent.get("run_id")!=child.run_id
            or head.get("sha")!=intent.get("head_sha") or head.get("ref")!=intent.get("head_branch")
            or base.get("ref")!=connection.branch or intent.get("base_branch")!=connection.branch
            or any(goals.mapping(side.get("repo")).get("id")!=connection.repository_id
                or str(goals.mapping(side.get("repo")).get("full_name") or "").casefold()!=connection.github_repo.casefold()
                for side in (head,base))):return False
        with authority_guard(binding,action="read"):
            saved=ContentFactoryRun.objects.select_for_update().get(pk=child.pk)
            if goals.mapping(saved.result).get("publish_merge_intent")!=intent:return False
            saved.result={**saved.result,"merge_status":"merged","merge_response":{"merged":True,"sha":sha},
                "merge_sha":sha,"merged_at":now.isoformat()}
            saved.status="completed";saved.save(update_fields=["result","status","updated_at"])
            child.result,child.status=saved.result,saved.status
        return True
    finally:
        if credential is not None:
            try:http_client.delete("https://api.github.com/installation/token",headers=headers,timeout=(3,10))
            except Exception:pass


def proofs(op, context, config, *, source, now):
    """Collect real draft and live-body evidence without dispatching or granting approval."""
    if goals.mapping(op.receipt.get("goal")).get("target")!="first_article_live":return {}
    run=article_run(op,config)
    if run is None or run.status in {"failed","blocked","cancelled","denied"} or not article_review_identity_is_complete(run):return {}
    original=goals.mapping(run.run_request).get("expected_source_sha")
    merge=merged_source(op,run,context) if source!=original else None
    if source!=original and (merge is None or merge["source_sha"]!=source):return {}
    identity=article_review_identity(run)
    result={"first_draft":goals.make_proof("first_draft","article_draft",scope=op.receipt["goal"]["scope"],source_sha=source,
        sha=identity["commit_sha"],run_id=run.run_id,evidence={"goal":op.receipt["goal"]["goal_id"],"review":identity},at=run.updated_at)}
    receipt=goals.mapping(op.receipt.get("first_article_live_receipt"))
    checked=goals.stamp(receipt.get("checked_at"))
    if (approval_matches(op,run) and merge is not None and receipt.get("status")=="passed" and receipt.get("provider_verified") is True
        and receipt.get("source_sha")==source and receipt.get("run_id")==run.run_id
        and receipt.get("merge_run_id")==merge["run_id"] and checked and now-timedelta(hours=24)<=checked<=now+timedelta(minutes=5)
        and receipt.get("expected_body_sha256")==receipt.get("observed_body_sha256")
        and isinstance(receipt.get("expected_body_sha256"),str) and goals.DIGEST.fullmatch(receipt["expected_body_sha256"])
        and receipt.get("method")=="http_article_body" and isinstance(receipt.get("response_sha256"),str)
        and goals.DIGEST.fullmatch(receipt["response_sha256"]) and _public_url(receipt.get("public_url"),op.connection.organization.domain)):
        result["first_article_live"]=goals.make_proof("first_article_live","article_deployment_marker",scope=op.receipt["goal"]["scope"],
            source_sha=source,sha=source,run_id=run.run_id,evidence=receipt,at=checked)
    return result


def _public_url(value, domain):
    if not isinstance(value,str) or len(value)>2048 or any(ord(char)<33 or char in "[]()<>" for char in value):return False
    try:
        parsed=urlsplit(value);apex=domain.lower().rstrip(".").removeprefix("www.")
        return (parsed.scheme=="https" and parsed.hostname in {apex,"www."+apex} and parsed.port in {None,443}
            and not parsed.username and not parsed.password and not parsed.query and not parsed.fragment and parsed.path not in {"","/"})
    except ValueError:return False


def _wait(op, now, message):
    op.state,op.next_attempt_at="pending",now+timedelta(minutes=1)
    op.receipt.update(status="working",current_step="first-article",message=message,user_action=None)
    return op


def _check_response(response):
    if response.status_code>=400:
        body=goals.mapping(response.data)
        raise WebsiteAuthorityError(body.get("code") or "website_prepare_step_failed",body.get("detail") or "The first article step could not finish.",
            status=response.status_code,retryable=response.status_code>=500)


def advance(op, context, config, *, user, now):
    """Advance through the common channel while retaining original Git controls."""
    from .publishing_channels import enabled, execute
    if enabled():
        from .publishing_channel_contract import ChannelRequest
        return execute("publish", ChannelRequest(context=context, config=config, user=user,
            operation=op, step="first-article", now=now))
    return _advance_git_first_article(op, context, config, user=user, now=now)


def _advance_git_first_article(op, context, config, *, user, now):
    """Run the existing generation/review/publication controls from one durable goal."""
    if not active(op):
        raise WebsiteAuthorityError("website_prepare_blocked","First-article execution is disabled or its consent changed.")
    from . import vibe_marketing_views as views
    run=article_run(op,config)
    if run is None:
        from . import website_goal_budget as budget
        if budget.enabled():
            reason=budget.exhaustion(op.receipt["goal"],now=now,action="first-article-generation")
            if reason:
                from .website_goal_runner import stop_goal
                stop_goal(op,op.receipt["goal"],reason=reason,now=now)
                return op
        from .website_first_article_topics import current_selection
        plan=goals.mapping(op.receipt.get("first_article_selection"))
        source=op.receipt["goal"].get("current_source_sha")
        if plan.get("source_sha")!=source or not isinstance(plan.get("data"),dict):
            plan={"source_sha":source,"data":current_selection(context,config)}
            _save_fields(op,{"first_article_selection":plan})
        auto=op.receipt["goal"]["consent"]["answers"]["publish_mode"]=="auto" and not config.requires_review
        data={**plan["data"],"delivery_mode":"publish_code" if auto else "review_draft","delivery_mode_explicit":True}
        from .website_connections import contract_for
        contract={**contract_for(op.connection),"expected_source_sha":source}
        if budget.enabled():
            used=goals.mapping(op.receipt["goal"].get("budget_used"))
            contract["goal_budget"]={"version":1,"goal_id":op.receipt["goal"]["goal_id"],"scope_digest":goals.digest(op.receipt["goal"]["scope"]),
                "remaining_active_seconds":settings.ARTICLES_GOAL_ACTIVE_SECONDS-(used.get("active_seconds") or 0),
                "remaining_model_usd":settings.ARTICLES_GOAL_MODEL_USD-(used.get("model_cost") or 0)}
        response=views.start_article_generation(user=user,context=context,data=data,website_contract=contract,goal_operation_id=op.pk)
        if response.status_code>=400 and goals.mapping(response.data).get("field")=="topicCandidateId":
            # Coverage may move after the inventory read. No generation charge or
            # dispatch occurs before this canonical candidate/coverage guard.
            _save_fields(op,{"first_article_selection":{"source_sha":source,"data":current_selection(context,config)}})
            return _wait(op,now,"Selecting the next uncovered article topic.")
        _check_response(response)
        run_id=goals.mapping(response.data).get("run_id") or goals.mapping(response.data).get("runId")
        if not isinstance(run_id,str) or not run_id:
            raise WebsiteAuthorityError("website_prepare_step_failed","The first article dispatch did not return its durable identity.",retryable=True)
        _save_fields(op,{"first_article_run_id":run_id,"child_run_id":run_id})
        return _wait(op,now,"Writing the first article and checking its preview.")
    if op.receipt.get("first_article_run_id")!=run.run_id:
        _save_fields(op,{"first_article_run_id":run.run_id,"child_run_id":run.run_id})
    if run.status in {"failed","blocked","cancelled","denied"}:
        from .website_article_recovery import observe,resume
        if run.status in {"failed","blocked"}:
            if resume(op,run,context,user=user,now=now):return op
            observe(op,run)
        error=WebsiteAuthorityError(goals.mapping(run.result).get("error_code") or "website_prepare_step_failed",
            "The first article is repairing its failed checks.",retryable=goals.mapping(run.result).get("retryable") is True)
        error.result=goals.mapping(run.result)
        raise error
    if not article_review_identity_is_complete(run):return _wait(op,now,"Checking the first article's exact preview and quality gates.")
    if not article_publish_approval_receipt_matches(run):
        if config.requires_review or op.receipt["goal"]["consent"]["answers"]["publish_mode"]!="auto":
            op.state,op.next_attempt_at="needs_user",now+timedelta(minutes=5)
            op.receipt.update(status="needs_you",current_step="first-article",message="Review the first article.",
                user_action={"id":"review_draft","label":"Review first article","run_id":run.run_id})
            return op
        response=views.control_article_run(user=user,context=context,run=run,action="approve",data={"auto_merge":True})
        _check_response(response);run.refresh_from_db()
        if not article_publish_approval_receipt_matches(run):return _wait(op,now,"Waiting for the first article approval receipt.")
    if not approval_matches(op,run):
        _save_fields(op,{"first_article_approval":{"goal":_immutable_binding(op.receipt["goal"]),"review":article_review_identity(run),
            "source_sha":goals.mapping(run.run_request).get("expected_source_sha"),
            "approval_digest":goals.digest(goals.mapping(run.run_request).get(RECEIPT_KEY))}})
    child=views._publish_child_for_run_state(run,context=context)
    if child is None:
        response=views.control_article_run(user=user,context=context,run=run,action="publish-pr",data={"auto_merge":True})
        _check_response(response)
        return _wait(op,now,"Opening the first article publication pull request.")
    if op.receipt.get("first_article_publish_run_id")!=child.run_id:
        _save_fields(op,{"first_article_publish_run_id":child.run_id,"child_run_id":child.run_id})
    if child.status in {"failed","blocked"}:
        from .website_article_recovery import observe,resume
        if resume(op,child,context,user=user,now=now):return op
        observe(op,child)
        error=WebsiteAuthorityError(goals.mapping(child.result).get("error_code") or "website_prepare_step_failed",
            "The article publication is repairing its failed checks.",retryable=goals.mapping(child.result).get("retryable") is True)
        error.result=goals.mapping(child.result)
        raise error
    views._maybe_auto_merge_publish_child(child=child,viewed_run=run,context=context)
    child.refresh_from_db()
    if goals.mapping(child.result).get("merge_status")=="manual_merge_required":
        try:observed=observe_manual_merge(op,child,now=now)
        except Exception:observed=False
        if not observed:
            op.state,op.next_attempt_at="needs_user",now+timedelta(minutes=5)
            op.receipt.update(status="needs_you",current_step="first-article",message="Merge the first article pull request.",
                user_action={"id":"merge_pr","label":"Merge article PR","run_id":child.run_id,
                    "pr_number":views._pull_request_number_from_run(child)})
            return op
    merge=merged_source(op,run,context)
    if merge is None:return _wait(op,now,"Waiting for the first article pull request to merge.")
    if merge["source_sha"]!=op.receipt["goal"].get("current_source_sha"):
        return _wait(op,now,"Verifying the merged article source.")
    if op.receipt.get("first_article_deployment_source_sha")!=merge["source_sha"]:
        _save_fields(op,{"first_article_deployment_source_sha":merge["source_sha"]},remove=("deployment_wait_started_at","deployment_attempts"))
    views._apply_publish_child_evidence_to_article(context.organization,run,child)
    from .article_publish_status import refresh_publish_statuses,latest_live_observation
    articles=refresh_publish_statuses(context.organization,config,limit=6,force=True,source_run_ids=(run.run_id,child.run_id))
    for article in articles:
        if article.source_run_id not in {run.run_id,child.run_id}:continue
        receipt=latest_live_observation(article)
        if (receipt.get("state")=="verified" and receipt.get("merge_commit_sha")==merge["source_sha"]
            and receipt.get("deployment_state")=="matching_content_served"):
            _save_fields(op,{"first_article_live_receipt":{"status":"passed","provider_verified":True,
                "source_sha":merge["source_sha"],"run_id":run.run_id,"merge_run_id":child.run_id,
                "public_url":receipt["canonical_url"],"checked_at":receipt["checked_at"],"method":receipt["method"],
                "expected_body_sha256":receipt["expected_body_sha256"],"observed_body_sha256":receipt["observed_body_sha256"],
                "response_sha256":receipt["response_sha256"]}})
            return _wait(op,now,"Confirming the first article's live publication proof.")
    raise WebsiteAuthorityError("deployment_marker_unverified","Waiting for the first article's exact content to appear on the live site.",retryable=True)
