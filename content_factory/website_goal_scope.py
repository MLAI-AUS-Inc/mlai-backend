"""Django-owned source-only scope transitions; identity changes remain fenced."""
from copy import deepcopy
from django.utils import timezone
from . import website_goal as goals


def record_source_transition(connection, *, previous_revision, source_sha):
    """Record a trusted revision edge inside the caller's authority transaction."""
    from .website_wakeups import enabled
    from .website_models import WebsiteConnectionOperation
    if not enabled() or not goals.enrolled(connection.organization_id):
        return
    if not isinstance(source_sha,str) or not goals.SHA.fullmatch(source_sha):
        raise ValueError("A source scope transition needs a verified source identity")
    WebsiteConnectionOperation.objects.get_or_create(idempotency_key=f"{connection.pk}:goal-source-scope:{connection.generation}:{connection.configuration_version}",
        defaults={"connection":connection,"generation":connection.generation,"action":"goal-source-scope","state":"completed",
            "payload":{"from_revision":previous_revision,"to_revision":connection.configuration_version,"source_sha":source_sha},
            "receipt":{"status":"source_scope_changed","at":timezone.now().isoformat()}})


def transition_chain(old_scope, new_scope, records, *, source_sha):
    """Only a complete trusted source-revision chain permits automatic rebinding."""
    if any(old_scope.get(key)!=new_scope.get(key) or type(old_scope.get(key)) is not type(new_scope.get(key))
           for key in goals.SCOPE_FIELDS if key!="configuration_revision"):
        return None
    before,after=old_scope.get("configuration_revision"),new_scope.get("configuration_revision")
    if type(before) is not int or type(after) is not int or not before<after:
        return None
    edges={row.get("from_revision"):row for row in records if isinstance(row,dict)
        and type(row.get("from_revision")) is int and type(row.get("to_revision")) is int
        and row["to_revision"]==row["from_revision"]+1}
    chain=[]
    while before<after and len(chain)<16:
        edge=edges.get(before)
        if not edge:
            return None
        chain.append(edge)
        before=edge["to_revision"]
    if before!=after or not chain or chain[-1].get("source_sha")!=source_sha:
        return None
    return chain


def rebind_source_scope(goal, connection, *, company_id, now=None):
    """Invalidate proof and retain goal identity/counters only for trusted source changes."""
    now=now or timezone.now()
    old=goals.mapping(goal).get("scope")
    current=goals.scope_for(connection,company_id)
    if not goals.valid_goal(goal,old):
        return None
    rows=connection.operations.filter(generation=connection.generation,action="goal-source-scope",state="completed",
        payload__to_revision__gt=old["configuration_revision"],payload__to_revision__lte=current["configuration_revision"]).order_by("created_at","pk")[:16]
    chain=transition_chain(old,current,[goals.mapping(row.payload) for row in rows],source_sha=connection.verified_sha)
    if chain is None:
        return None
    result=deepcopy(goal)
    result["scope"]=current
    result=goals.refresh_goal(result,{},scope=current,source_sha=connection.verified_sha,now=now)
    result["scope_history"]=(list(result.get("scope_history") or [])+[{"from_revision":old["configuration_revision"],
        "to_revision":current["configuration_revision"],"source_sha":connection.verified_sha,"at":now.isoformat(),"reason":"trusted_source_change"}])[-16:]
    result["current_source_sha"]=connection.verified_sha
    return result
