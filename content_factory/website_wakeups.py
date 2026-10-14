"""Typed event hints and bounded immediate advances backed by durable operations."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from hashlib import sha256
import json
import random
import threading
import time
from django.conf import settings
from django.core.cache import cache
from django.db import close_old_connections, transaction
from django.utils import timezone
from . import website_goal as goals

_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="articles-goal-wake")
_SLOTS = threading.BoundedSemaphore(32)
CHECK_STATES = {"success", "failure", "cancelled", "timed_out", "neutral", "skipped", "action_required"}


def enabled():
    """Event execution is separately enrolled and remains off by default."""
    return getattr(settings, "ARTICLES_WEBSITE_EVENT_WAKES_ENABLED", False)


def positive(value):
    """GitHub immutable numeric IDs never accept booleans or string coercion."""
    return type(value) is int and value > 0


def signal_for(event, payload):
    """Reduce a verified provider payload to bounded non-authoritative hints."""
    payload = goals.mapping(payload)
    installation = goals.mapping(payload.get("installation")).get("id")
    repo = goals.mapping(payload.get("repository")).get("id")
    base = {"kind": event, "installation_id": str(installation) if positive(installation) else None,
        "repository_ids": [repo] if positive(repo) else []}
    if event == "installation" and payload.get("action") == "new_permissions_accepted" and positive(installation):
        return {**base, "milestone": "access", "success": True}
    if event == "installation_repositories" and payload.get("action") == "added" and positive(installation):
        values = payload.get("repositories_added")
        ids = [row["id"] for row in values[:100] if isinstance(row,dict) and positive(row.get("id"))] if isinstance(values,list) else []
        return {**base, "repository_ids": ids, "milestone": "access", "success": True} if ids else None
    if not positive(repo):
        return None
    if event == "pull_request" and payload.get("action") == "closed":
        row = goals.mapping(payload.get("pull_request"))
        number = row.get("number")
        head = goals.mapping(row.get("head")).get("sha")
        branch = goals.mapping(row.get("base")).get("ref")
        if positive(number) and isinstance(head,str) and goals.SHA.fullmatch(head) and isinstance(branch,str) and len(branch)<=255:
            return {**base,"milestone":"merged","pr_number":number,"sha":head,"branch":branch,"success":row.get("merged") is True}
    if event in {"check_suite","check_run"} and payload.get("action") == "completed":
        row = goals.mapping(payload.get(event))
        sha, conclusion = row.get("head_sha"), row.get("conclusion")
        if isinstance(sha,str) and goals.SHA.fullmatch(sha) and isinstance(conclusion,str) and conclusion in CHECK_STATES:
            return {**base,"milestone":"previewed","sha":sha,"success":conclusion=="success"}
    if event == "deployment_status":
        row, deployment = goals.mapping(payload.get("deployment_status")), goals.mapping(payload.get("deployment"))
        sha, branch, state = deployment.get("sha"), deployment.get("ref"), row.get("state")
        if isinstance(sha,str) and goals.SHA.fullmatch(sha) and isinstance(branch,str) and len(branch)<=255 and state in ("success","failure","error"):
            return {**base,"milestone":"live","sha":sha,"branch":branch,"success":state=="success"}
    return None


def policy_allows(op, signal):
    """Events cannot resurrect deterministic failures, terminal work or spent budgets."""
    receipt = goals.mapping(op.receipt)
    if op.state in {"pending","running","needs_user"}:
        return True
    return (op.state=="needs_attention" and signal.get("success") is True
        and receipt.get("failure_class") in {"transient","waiting"}
        and not receipt.get("budget_stop_reason") and not receipt.get("strategy_observation_invalid"))


def matches(connection, op, signal, child=None):
    """Match current immutable identities and a saved source or owned setup PR."""
    if connection.state != "connected" or op.generation != connection.generation:
        return False
    if signal.get("installation_id") and signal["installation_id"] != str(connection.installation_id):
        return False
    if signal.get("repository_ids") and connection.repository_id not in signal["repository_ids"]:
        return False
    if signal.get("milestone")=="access":
        return bool(signal.get("installation_id"))
    saved = goals.mapping(getattr(child,"run_request",None))
    result = goals.mapping(getattr(child,"result",None))
    intent=goals.mapping(result.get("publish_merge_intent"))
    owned = (child is not None and saved.get("website_connection_id")==str(connection.pk)
        and saved.get("connection_generation")==connection.generation and saved.get("repository_id")==connection.repository_id)
    if signal.get("branch") and signal["branch"] != connection.branch:
        return False
    if signal.get("milestone")=="merged":
        return owned and (result.get("pr_number") or intent.get("pr_number"))==signal.get("pr_number") and (result.get("branch_commit_sha") or intent.get("head_sha"))==signal.get("sha")
    current = goals.mapping(goals.mapping(op.receipt).get("goal")).get("current_source_sha") or connection.verified_sha
    return signal.get("sha")==current or owned and signal.get("sha")==(result.get("branch_commit_sha") or intent.get("head_sha"))


def queue_wake(connection_id, *, signal, expected_generation=None):
    """Coalesce one durable signal per site, then submit only after commit."""
    from .website_models import WebsiteConnection, WebsiteConnectionOperation
    from organizations.models import Organization
    if not enabled():
        return None
    snapshot = WebsiteConnection.objects.select_related("organization").filter(pk=connection_id,state="connected").first()
    if not snapshot or not goals.enrolled(snapshot.organization_id):
        return None
    now = timezone.now()
    with transaction.atomic():
        Organization.objects.select_for_update().get(pk=snapshot.organization_id)
        connection = WebsiteConnection.objects.select_for_update().get(pk=connection_id)
        if connection.state!="connected" or expected_generation is not None and connection.generation!=expected_generation:
            return None
        parent = connection.operations.select_for_update().filter(generation=connection.generation,action="prepare").order_by("-created_at","-pk").first()
        if parent and parent.state in {"pending", "running", "needs_user", "needs_attention"} and not goals.valid_goal(goals.mapping(parent.receipt).get("goal"),goals.scope_for(connection,parent.payload.get("company_id"))):
            from .website_goal_scope import rebind_source_scope
            rebound=rebind_source_scope(goals.mapping(parent.receipt).get("goal"),connection,company_id=parent.payload.get("company_id"),now=now)
            if rebound:
                from .website_prepare import _cancel_prepare_child
                from .models import OrganizationContentConfig
                config=OrganizationContentConfig.objects.get(website_connection=connection)
                _cancel_prepare_child(parent,config)
                from .website_goal_runner import retain_child_references
                rebound=retain_child_references(rebound,parent.receipt)
                parent.receipt={**{key:value for key,value in parent.receipt.items() if key not in {"child_run_id","setup_run_id","scan_run_id","child_resume_pending"}},"goal":rebound}
                parent.save(update_fields=["receipt","updated_at"])
        if not parent or not goals.valid_goal(goals.mapping(parent.receipt).get("goal"),goals.scope_for(connection,parent.payload.get("company_id"))) or not policy_allows(parent,signal):
            return None
        token = goals.digest(signal)
        wake, created = WebsiteConnectionOperation.objects.get_or_create(idempotency_key=f"{connection.pk}:goal-wake:{connection.generation}",defaults={
            "connection":connection,"generation":connection.generation,"action":"goal-wake","state":"pending","next_attempt_at":now,
            "payload":{"prepare_operation_id":str(parent.pk),"signal":signal,"signal_digest":token},"receipt":{"status":"queued"}})
        if not created and wake.payload.get("signal_digest")==token and wake.payload.get("prepare_operation_id")==str(parent.pk):
            return wake
        if not created:
            wake.payload={"prepare_operation_id":str(parent.pk),"signal":signal,"signal_digest":token}
            wake.state,wake.next_attempt_at="pending",now
            wake.save(update_fields=["payload","state","next_attempt_at","updated_at"])
        transaction.on_commit(lambda: submit_immediate(wake.pk),robust=True)
        return wake


def submit_immediate(identifier):
    """A bounded local fast path; the saved due operation is always the fallback."""
    if not enabled() or not _SLOTS.acquire(blocking=False):
        return False
    def run():
        close_old_connections()
        try:
            time.sleep(random.uniform(0,0.5))
            advance_wake(identifier)
        finally:
            close_old_connections()
            _SLOTS.release()
    try:
        _POOL.submit(run)
    except RuntimeError:
        _SLOTS.release()
        return False
    return True


def advance_wake(identifier, *, now=None):
    """Serialize by connection and leave active claims intact until their lease ends."""
    from .website_models import WebsiteConnectionOperation
    from .website_prepare import advance_prepare
    if not enabled():
        return None
    now=now or timezone.now()
    wake=WebsiteConnectionOperation.objects.select_related("connection").filter(pk=identifier,action="goal-wake",state="pending").first()
    if not wake or wake.next_attempt_at and wake.next_attempt_at>now:
        return None
    key=f"articles:goal-wake:{wake.connection_id}:{wake.generation}"
    owner=sha256(f"{identifier}:{time.monotonic_ns()}".encode()).hexdigest()
    if not cache.add(key,owner,timeout=120):
        return None
    try:
        with transaction.atomic():
            wake=WebsiteConnectionOperation.objects.select_for_update().select_related("connection").get(pk=identifier)
            parent=WebsiteConnectionOperation.objects.select_for_update().filter(pk=wake.payload.get("prepare_operation_id"),connection_id=wake.connection_id,generation=wake.generation,action="prepare").first()
            if not parent or wake.connection.generation!=wake.generation or wake.connection.state!="connected" or not goals.enrolled(wake.connection.organization_id) or not goals.valid_goal(goals.mapping(parent.receipt).get("goal"),goals.scope_for(wake.connection,parent.payload.get("company_id"))) or not policy_allows(parent,goals.mapping(wake.payload.get("signal"))):
                wake.state,wake.next_attempt_at="cancelled",None
            elif parent.state=="running" and parent.next_attempt_at and parent.next_attempt_at>now:
                wake.next_attempt_at=min(parent.next_attempt_at,now+timedelta(seconds=60))
                wake.save(update_fields=["next_attempt_at","updated_at"])
                return "pending"
            else:
                parent.state,parent.next_attempt_at="pending",now
                parent.save(update_fields=["state","next_attempt_at","updated_at"])
                wake.state,wake.next_attempt_at="completed",None
            wake.receipt={"status":wake.state,"signal_digest":wake.payload.get("signal_digest")}
            wake.save(update_fields=["state","next_attempt_at","receipt","updated_at"])
        if wake.state=="completed":
            advance_prepare(parent.pk,now=now)
        return wake.state
    finally:
        if cache.get(key)==owner:
            cache.delete(key)


def wake_from_event(event, payload):
    """Handle a verified event without trusting it as a milestone proof."""
    from .website_models import WebsiteConnection
    from workflow_runs.models import ContentFactoryRun
    signal=signal_for(event,payload)
    if not enabled() or signal is None:
        return 0
    rows=WebsiteConnection.objects.filter(state="connected")
    if signal.get("installation_id"):
        rows=rows.filter(installation_id=signal["installation_id"])
    if signal.get("repository_ids"):
        rows=rows.filter(repository_id__in=signal["repository_ids"])
    count=0
    for connection in rows.order_by("pk")[:100]:
        op=connection.operations.filter(generation=connection.generation,action="prepare").order_by("-created_at","-pk").first()
        if not op:
            continue
        child_id=goals.mapping(op.receipt).get("first_article_publish_run_id") or goals.mapping(op.receipt).get("setup_run_id")
        child=ContentFactoryRun.objects.filter(run_id=child_id,organization_id=connection.organization_id).first() if child_id else None
        if matches(connection,op,signal,child) and queue_wake(connection.pk,signal=signal,expected_generation=connection.generation):
            count+=1
    return count
