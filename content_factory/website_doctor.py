"""One bounded diagnostic contract; never serialize arbitrary receipts or results."""
import math
import re
from django.conf import settings
from django.utils import timezone
from . import website_goal as goals
from .website_failure_redaction import redact_secret_text

CREDENTIAL_PREFIX = re.compile(r"(?:sk-|xox[baprs]-|AKIA|ASIA)")
ID = re.compile(r"[A-Za-z0-9_.:-]{1,100}")
CODE = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,127}")
STATES = {"pending", "running", "queued", "waiting", "awaiting_approval", "code_review_ready", "completed", "failed", "blocked", "cancelled", "denied", "needs_user", "needs_attention", "deleted", "stopped"}
RUNGS = {"adopt", "native_collection", "deterministic", "model_adapter", "agent", "hosted"}
REASONS = {"inventory_selected", "source_changed", "strategy_exhausted", "fingerprint_guard", "inventory_exhausted"}


def safe_identifier(value):
    """Keep safe references; token-shaped values are omitted even if syntactically IDs."""
    return value if isinstance(value,str) and ID.fullmatch(value) and not CREDENTIAL_PREFIX.match(value) and redact_secret_text(value)==value else None


def safe_code(value):
    """Allow code identifiers, not provider messages or credential assignments."""
    return value if isinstance(value,str) and CODE.fullmatch(value) and redact_secret_text(value)==value else None


def number(value, *, integer=False):
    """Unknown/invalid measurements stay null rather than becoming a zero sample."""
    if type(value) not in ({int} if integer else {int,float}):
        return None
    try:
        return value if math.isfinite(value) and 0<=value<=1e12 else None
    except OverflowError:
        return None


def stamp(value):
    """Expose only aware timestamps; malformed values contain no useful proof."""
    parsed=goals.stamp(value)
    return parsed.isoformat() if parsed else None


def digest(value):
    """A proof reference is a digest, never source content."""
    return value if isinstance(value,str) and goals.DIGEST.fullmatch(value) else None


def sha(value):
    """Keep exact source identities only."""
    return value if isinstance(value,str) and goals.SHA.fullmatch(value) else None


def project_doctor(op, *, scope, source_sha, milestones=None, children=(), receipts=()):
    """Project allowlisted saved facts; this function has no reads or side effects."""
    receipt,payload=goals.mapping(op.receipt),goals.mapping(op.payload)
    goal=goals.mapping(receipt.get("goal"))
    valid=goals.valid_goal(goal,scope)
    failure=goals.mapping(receipt.get("failure"))
    used=goals.mapping(goal.get("budget_used")) if valid else {}
    pin=goals.mapping(goal.get("strategy_pin")) if valid else {}
    history=[]
    raw_history=goal.get("strategy_history") if valid else None
    if isinstance(raw_history,list):
        for item in raw_history[-16:]:
            row=goals.mapping(item)
            if isinstance(row.get("to"),str) and row["to"] in RUNGS and isinstance(row.get("reason"),str) and row["reason"] in REASONS and (row.get("from") is None or isinstance(row["from"],str) and row["from"] in RUNGS):
                history.append({"from":row.get("from"),"to":row["to"],"reason":row["reason"],"at":stamp(row.get("at")),"revision":number(row.get("revision"),integer=True)})
    checks=[]
    saved_checks = goal.get("milestones", []) if valid else []
    selected_checks = milestones if isinstance(milestones, list) else saved_checks
    for item in selected_checks[:9]:
        row=goals.mapping(item)
        if not isinstance(row.get("name"),str) or row["name"] not in goals.MILESTONES or not isinstance(row.get("state"),str) or row["state"] not in {"passed","pending","not_required"}:
            continue
        saved = next((entry for entry in saved_checks if entry["name"] == row["name"]), {})
        proof=goals.mapping(saved.get("proof"))
        reference=None
        if row["state"]=="passed" and goals.valid_proof(row["name"], proof, scope=scope, source_sha=source_sha, now=timezone.now()):
            reference={"kind":proof["kind"],"sha":sha(proof.get("sha")),"runId":safe_identifier(proof.get("run_id")),
                "digest":digest(proof.get("evidence_digest")),"at":stamp(proof.get("at"))}
        checks.append({"name":row["name"],"state":row["state"] if reference or row["state"] != "passed" else "pending","proof":reference})
    safe_children=[]
    for value in (children if isinstance(children,(list,tuple)) else [])[:3]:
        item=goals.mapping(value)
        if safe_identifier(item.get("runId")) and isinstance(item.get("status"),str) and item["status"] in STATES:
            safe_children.append({"runId":item["runId"],"status":item["status"]})
    safe_receipts=[]
    for value in (receipts if isinstance(receipts,(list,tuple)) else [])[:2]:
        item=goals.mapping(value)
        if isinstance(item.get("kind"),str) and item["kind"] in {"ci","deployment"} and safe_identifier(item.get("operationId")):
            safe_receipts.append({"kind":item["kind"],"operationId":item["operationId"],"status":item.get("status") if isinstance(item.get("status"),str) and item["status"] in {"passed","verified","failed","blocked"} else "unknown",
                "sourceSha":sha(item.get("sourceSha")),"digest":digest(item.get("digest")),"at":stamp(item.get("at"))})
    attempts=goals.mapping(goal.get("attempts_by_class")) if valid else {}
    classification=failure.get("failure_class") or receipt.get("failure_class")
    effective=receipt.get("effective_failure_class")
    fingerprint=failure.get("failure_fingerprint") or receipt.get("failure_fingerprint")
    return {"version":1,"companyId":safe_identifier(scope["company_id"]),"connectionId":safe_identifier(scope["connection_id"]),
        "connectionGeneration":scope["connection_generation"],"configurationRevision":scope["configuration_revision"],
        "operationId":safe_identifier(str(op.pk)),"goalId":safe_identifier(goal.get("goal_id")) if valid else None,
        "observedSourceSha":sha(source_sha),"verifiedSourceSha":sha(getattr(op.connection,"verified_sha",None)),
        "attempt":number(payload.get("attempt"),integer=True),"retryEpoch":safe_identifier(payload.get("retry_epoch")),
        "deploymentAttempts":number(receipt.get("deployment_attempts"),integer=True),
        "failure":{"code":safe_code(failure.get("code") or receipt.get("reason_code")),
            "class":classification if isinstance(classification,str) and classification in goals.CLASSES else None,
            "effectiveClass":effective if isinstance(effective,str) and effective in goals.CLASSES else None,
            "fingerprint":fingerprint if isinstance(fingerprint,str) and goals.FINGERPRINT.fullmatch(fingerprint) else None},
        "checks":checks,"children":safe_children,"receipts":safe_receipts,
        "strategy":{"rung":pin["rung"],"revision":number(pin.get("revision"),integer=True),"history":history} if isinstance(pin.get("rung"),str) and pin["rung"] in RUNGS else None,
        "budget":{"activeSeconds":number(used.get("active_seconds")),"modelUsd":number(used.get("model_cost")),
            "modelCalls":number(used.get("model_calls"),integer=True),"tokens":number(used.get("tokens"),integer=True),
            "providerSeconds":number(used.get("provider_seconds")),"builds":number(used.get("builds"),integer=True),"previews":number(used.get("previews"),integer=True),
            "attemptsByClass":{name:number(attempts.get(name),integer=True) for name in goals.CLASSES},
            "limits":{"wallSeconds":number(getattr(settings,"ARTICLES_GOAL_WALL_SECONDS",None)),"activeSeconds":number(getattr(settings,"ARTICLES_GOAL_ACTIVE_SECONDS",None)),"modelUsd":number(getattr(settings,"ARTICLES_GOAL_MODEL_USD",None))}}}


def read_doctor(op, *, scope, source_sha, milestones=None):
    """Read only scoped child/status and CI/deploy references; never contact providers."""
    from workflow_runs.models import ContentFactoryRun
    from .website_operations import deletion_epoch
    children=[]
    for key in ("child_run_id","setup_run_id","scan_run_id"):
        identifier=safe_identifier(goals.mapping(op.receipt).get(key))
        if not identifier or any(item["runId"]==identifier for item in children):
            continue
        child=ContentFactoryRun.objects.filter(run_id=identifier,organization_id=op.connection.organization_id).first()
        saved=goals.mapping(getattr(child,"run_request",None))
        if child and saved.get("website_connection_id")==str(op.connection_id) and saved.get("connection_generation")==op.generation and saved.get("repository_id")==op.connection.repository_id and saved.get("deletion_epoch")==deletion_epoch(op.connection):
            children.append({"runId":identifier,"status":child.status})
    receipts=[]
    for action,kind in (("ci-verify","ci"),("deployment-verify","deployment")):
        row=op.connection.operations.filter(generation=op.connection.generation,action=action,state="completed",receipt__source_sha=source_sha).order_by("-created_at","-pk").first()
        if row:
            raw=goals.mapping(row.receipt)
            receipts.append({"kind":kind,"operationId":str(row.pk),"status":raw.get("status"),"sourceSha":raw.get("source_sha"),
                "digest":raw.get("evidence_digest") or raw.get("ci_evidence_digest"),"at":raw.get("checked_at")})
    return project_doctor(op,scope=scope,source_sha=source_sha,milestones=milestones,children=children,receipts=receipts)
