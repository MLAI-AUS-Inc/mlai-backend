"""Reserve and reconcile an authenticated CF proof of the exact merged source."""
from django.conf import settings
from django.utils import timezone
from . import website_goal as goals
from .website_contract import WebsiteAuthorityError
from .website_connections import contract_for, authority_guard, require_unlocked_remote_call


def enabled(connection):
    """Hosted attestation requires the independent rollout and goal enrollment."""
    return getattr(settings,"ARTICLES_HOSTED_ATTESTATION_ENABLED",False) and getattr(settings,"ARTICLES_HOSTED_VERIFICATION_ENABLED",False) and goals.enrolled(connection.organization_id)


def remote_request(method,path,*,data=None):
    """Only fixed routes on the configured authenticated CF service are fetched."""
    from integrations import http_client
    from .vibe_marketing_views import _content_factory_remote_config,_content_factory_headers
    remote=_content_factory_remote_config()
    if not remote["enabled"]:raise WebsiteAuthorityError("worker_unavailable","Hosted verification is temporarily unavailable.",retryable=True)
    require_unlocked_remote_call()
    try:
        response=getattr(http_client,method)(remote["base_url"]+path,headers=_content_factory_headers(),**({"json":data} if data is not None else {}),timeout=(3,20))
        response.raise_for_status()
        value=response.json()
        if not isinstance(value,dict):raise ValueError("Invalid service outcome")
        return value
    except (http_client.RequestException,ValueError) as exc:
        raise WebsiteAuthorityError("worker_unavailable","Hosted native verification could not respond. It will retry.",retryable=True) from exc


def request_for(op,context,config,target,source):
    """Use original setup lineage and server target identity, never owner-supplied commands."""
    from .website_operations import reserve_workflow_operation
    from workflow_runs.models import ContentFactoryRun
    connection=config.website_connection
    source_run=goals.mapping(op.receipt).get("setup_run_id")
    if not source_run:raise WebsiteAuthorityError("verified_target_required","The original setup verifier inputs are not yet available.",retryable=True)
    payload={**contract_for(connection),"domain":context.organization.domain,"github_repo":connection.github_repo,
        "branch":connection.branch,"expected_source_sha":source,"source_run_id":source_run,
        "target_id":target.target_key,"contract_digest":target.contract.get("contract_digest"),
        "client_request_id":f"goal:{op.pk}:hosted:{source}:{op.payload.get('attempt',1)}:{op.payload.get('retry_epoch','initial')}"}
    from .website_goal_budget import enabled as budget_enabled
    if budget_enabled():
        goal=op.receipt["goal"];used=goals.mapping(goal.get("budget_used"))
        payload["goal_budget"]={"version":1,"goal_id":goal["goal_id"],"scope_digest":goals.digest(goal["scope"]),
            "remaining_active_seconds":settings.ARTICLES_GOAL_ACTIVE_SECONDS-(used.get("active_seconds") or 0),
            "remaining_model_usd":settings.ARTICLES_GOAL_MODEL_USD-(used.get("model_cost") or 0)}
    child=reserve_workflow_operation(connection,workflow="hosted_native_verification",payload=payload)
    payload["run_id"]=str(child.pk)
    with authority_guard(payload,action="read"):
        child.refresh_from_db()
        child.payload={**child.payload,"run_id":payload["run_id"],"source_run_id":source_run,"binding":dict(payload)}
        child.save(update_fields=["payload","updated_at"])
        run,_=ContentFactoryRun.objects.get_or_create(run_id=payload["run_id"],defaults={"organization":context.organization,
            "domain":context.organization.domain,"github_repo":connection.github_repo,"workflow":"hosted_native_verification","status":"queued","run_request":dict(payload)})
    goal=op.receipt["goal"]
    op.receipt["hosted_verification_run_id"]=run.run_id
    from .website_goal_runner import retain_child_references
    op.receipt["goal"]=retain_child_references(goal,op.receipt)
    return child,payload


def advance(op,context,config,target,source):
    """Bounded reconciliation; lost POST/worker/callback is recovered by the saved job."""
    child,payload=request_for(op,context,config,target,source)
    outcome=remote_request("post","/api/runs/hosted-native-verification",data=payload)
    from workflow_runs.models import ContentFactoryRun
    state=outcome.get("status")
    if outcome.get("run_id") != payload["run_id"] or state not in {"pending","queued","running","completed","failed","cancelled","denied"}:
        raise WebsiteAuthorityError("worker_unavailable","Hosted verification has no valid saved status yet.",retryable=True)
    with authority_guard(payload,action="read"):
        child.refresh_from_db()
        if child.state in {"cancelled","denied","deleted"}:raise WebsiteAuthorityError("website_operation_cancelled","The hosted verifier was cancelled.")
        child.receipt={**child.receipt,"status":state,"repository_modified":False}
        child.state=state if state in {"completed","failed","cancelled","denied"} else "running"
        child.save(update_fields=["state","receipt","updated_at"])
        ContentFactoryRun.objects.filter(run_id=payload["run_id"],organization=context.organization).update(status=state,
            result={"verification":outcome.get("verification"),"usage":outcome.get("usage"),"generation":outcome.get("generation"),"state_version":outcome.get("state_version")},updated_at=timezone.now())
    if state=="failed":raise WebsiteAuthorityError("NATIVE_VERIFICATION_FAILED","The hosted build or article render needs repair.",retryable=False)
    if state in {"cancelled","denied"}:raise WebsiteAuthorityError("website_operation_cancelled","Hosted verification was cancelled.")
    if state!="completed":return {"status":"pending"}
    proof=goals.mapping(outcome.get("verification"))
    from .website_verification import record_ci_attestation
    receipt=record_ci_attestation({**proof,"run_id":payload["run_id"],"source_run_id":payload["source_run_id"]},origin="authenticated_hosted")
    return {"status":"completed","operation_id":str(receipt.pk)}


def read_completed_proof(connection,data):
    """Authenticate original service output; caller JSON cannot select its proof origin."""
    from .website_operations import validate_operation
    origin=validate_operation(connection,data)
    binding=goals.mapping(origin.payload.get("binding")) if origin else {}
    if (not origin or origin.payload.get("workflow")!="hosted_native_verification" or origin.state!="completed"
        or any(binding.get(key)!=data.get(key) for key in ("target_id","contract_digest","run_id","source_run_id"))
        or binding.get("expected_source_sha")!=data.get("source_sha")):
        raise WebsiteAuthorityError("ci_attestation_scope_mismatch","Hosted evidence differs from its original verification operation.")
    result=remote_request("get",f"/api/runs/{binding['run_id']}/hosted-native-verification")
    if result.get("status")!="completed" or result.get("run_id")!=binding["run_id"]:
        raise WebsiteAuthorityError("ci_attestation_required","The hosted verifier has not completed this source yet.")
    proof = goals.mapping(result.get("verification"))
    fields = {"schema_version", "repository_id", "github_repo", "source_sha", "website_connection_id", "connection_generation", "operation_id", "operation_attempt", "deletion_epoch", "target_id", "adapter_id", "adapter_version", "contract_digest", "source_tree_sha", "environment_fingerprint", "lockfile_digests", "artifact_digest", "baseline_build", "patched_build", "verified_routes", "content_loading", "listing", "detail", "unknown_slug_404", "canonical", "assets", "browser", "narrow_layout", "wide_layout", "evidence_digest"}
    if (set(proof) != fields or type(proof.get("schema_version")) is not int or proof["schema_version"] != 2
        or any(type(proof.get(key)) is not int or proof[key] < minimum for key, minimum in (("repository_id",1),("connection_generation",1),("operation_attempt",1),("deletion_epoch",0),("adapter_version",1)))
        or not isinstance(proof.get("source_tree_sha"), str) or not goals.SHA.fullmatch(proof["source_tree_sha"])
        or any(not isinstance(proof.get(key), str) or not goals.DIGEST.fullmatch(proof[key]) for key in ("environment_fingerprint", "artifact_digest", "contract_digest", "evidence_digest"))
        or not isinstance(proof.get("lockfile_digests"), dict) or len(proof["lockfile_digests"]) > 20
        or any(not isinstance(value, str) or not goals.DIGEST.fullmatch(value) for value in proof["lockfile_digests"].values())):
        raise WebsiteAuthorityError("ci_attestation_incomplete", "Hosted native proof has incomplete source, environment or artifact evidence.")
    return proof
