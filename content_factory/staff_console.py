"""Staff startup projections and audited recovery actions; no founder authority bypass."""
from __future__ import annotations

from dataclasses import is_dataclass, replace
from datetime import date, datetime, time, timezone as utc
import hashlib
import importlib
from types import SimpleNamespace

from django.conf import settings
from django.core import signing
from django.db import transaction
from django.db.models import F, Q, Window
from django.db.models.functions import RowNumber
from django.utils import timezone

from organizations.models import Organization
from founder_tools.models import VibeRaisingCompany
from workflow_runs.models import ContentFactoryRun
from community_chat.admin_access import AdminError, require_mlai_admin
from . import registry_policy
from .models import OrganizationContentConfig
from .staff_alert_rules import mapping, failure_code, open_alert, overdue, global_alerts

ACTIONS = {"retry_now", "switch_to_draft", "release_or_refund", "resolve_and_notify"}
RECEIPT_KEYS = ("self_heal_v1", "points_v1", "channel_v1", "re_pin_v1", "model_spend", "snapshot_drift_v1", "event_progress")


def require_staff(user):
    """Guard direct service calls as well as the verified-installation HTTP views."""
    require_mlai_admin(user)
    if not getattr(settings, "MY_STARTUP_STAFF_CONSOLE_ENABLED", False) or not getattr(user, "is_staff", False):
        raise AdminError()


def provider(module, name):
    """Resolve an optional rollout package without caching a missing provider."""
    try:
        return getattr(importlib.import_module("content_factory."+module), name)
    except (ImportError, AttributeError):
        return None


def _orgs():
    return Organization.objects.select_related("content_config", "content_config__website_connection", "billing_user")


def _config(org):
    try:
        return org.content_config
    except OrganizationContentConfig.DoesNotExist:
        return None


def _alerts(org):
    return [v for v in mapping(mapping(getattr(_config(org), "pillar_strategy", None)).get("ops_alerts")).values() if isinstance(v, dict)]


def _companies(orgs):
    result = {}
    for company in VibeRaisingCompany.objects.filter(organization_id__in=[o.pk for o in orgs]).select_related("profile__user").order_by("created_at", "pk"):
        result.setdefault(company.organization_id, company)
    return result


def _latest_runs(orgs):
    return {run.organization_id: run for run in ContentFactoryRun.objects.filter(organization_id__in=[o.pk for o in orgs])
            .annotate(staff_rank=Window(RowNumber(), partition_by=[F("organization_id")],
                                      order_by=[F("created_at").desc(), F("pk").desc()])).filter(staff_rank=1)}


def startup_row(org, company, run, *, now):
    """A pure projection over preloaded rows; never issue a query per startup."""
    config = _config(org)
    strategy = mapping(getattr(config, "pillar_strategy", None))
    marker = mapping(strategy.get("founder_journey"))
    result = mapping(getattr(run, "result", None))
    code = failure_code(run) if run else None
    heal = mapping(result.get("self_heal_v1"))
    # Read the P2 registry directly here: staff inspect every cohort, without changing founder projection gates.
    entry = registry_policy.load_registry()["by_code_v2"].get(code) if isinstance(code, str) else None
    ask = registry_policy.canonical_action_id(entry.get("action_id")) if entry and entry.get("failure_class") == "needs_user" else None
    status = getattr(run, "status", "")
    delivered = status in {"completed", "awaiting_approval", "approval_required"} or bool(result.get("draft_ready_v1"))
    state = "your_turn" if ask or status in {"awaiting_approval", "approval_required"} else "done" if delivered else "working"
    if state == "your_turn" and not ask:
        ask = "review_draft"
    workflow = getattr(run, "workflow", "")
    stage = ("setup" if workflow == "article_system_setup" else "website" if workflow in {"scan", "startup_autofill", "website_scan"}
             else "review" if delivered else "writing" if run else "first_article" if config and config.website_connection_id else "website")
    copy_id = "ask_"+ask if ask else None if delivered else registry_policy.founder_copy_id(code)
    if entry and status in {"failed", "blocked", "denied", "stopped"} and not ask and not (heal.get("mode") == "policy" and heal.get("next_at") and not heal.get("capped")):
        copy_id = "stopped"
    alerts = [r for r in _alerts(org) if open_alert(r)]
    owner = company.profile.user if company else org.billing_user
    transitions = result.get("last_transitions", heal.get("history", []))
    transitions = transitions if isinstance(transitions, list) else []
    return {"organizationId": org.pk, "companyId": str(company.pk) if company else None, "startupName": org.name,
            "domain": org.domain, "createdAt": org.created_at.isoformat(), "cohort": marker.get("cohort", "legacy"),
            "state": state, "stage": stage, "ask": ask, "copyId": copy_id,
            "code": code, "runId": getattr(run, "run_id", None), "attempts": heal.get("attempts", 0), "capped": bool(heal.get("capped")),
            "lastTransitions": [{k: t.get(k) for k in ("at", "from", "to")} for t in transitions[-3:] if isinstance(t, dict)],
            "openAlerts": len(alerts), "oldestOpenAlertAt": min((r.get("queued_at", "") for r in alerts), default=None),
            "overdueAlerts": sum(overdue(r, now) for r in alerts), "ownerEmail": getattr(owner, "email", None),
            "supportCode": hashlib.sha256(str(getattr(run, "run_id", org.pk)).encode()).hexdigest()[:10]}


def startup_list(params, *, now=None):
    """50 results and a signed cursor using three queries, including overdue filtering."""
    now = now or timezone.now()
    since = date.fromisoformat(params.get("since") or "2026-09-29")
    query = _orgs().filter(created_at__gte=datetime.combine(since, time.min, tzinfo=utc.utc)).order_by("pk")
    search = str(params.get("q") or "").strip()[:200]
    if search:
        query = query.filter(Q(name__icontains=search) | Q(domain__icontains=search) | Q(billing_user__email__icontains=search))
    orgs = list(query)
    overdue_count = sum(overdue(a, now) for org in orgs for a in _alerts(org))
    mode = params.get("alerts")
    if mode in {"open", "overdue"}:
        orgs = [o for o in orgs if any(overdue(a, now) if mode == "overdue" else open_alert(a) for a in _alerts(o))]
    offset = 0
    if params.get("cursor"):
        value = signing.loads(params["cursor"], salt="staff-startups", max_age=86400)
        offset = max(0, int(value["offset"]))
    # State filtering needs a run projection; keep its single batched query and then paginate.
    companies, runs = _companies(orgs), _latest_runs(orgs)
    rows = [startup_row(org, companies.get(org.pk), runs.get(org.pk), now=now) for org in orgs]
    if params.get("state"):
        rows = [r for r in rows if r["state"] == params["state"]]
    page = rows[offset:offset+50]
    cursor = signing.dumps({"offset": offset+50}, salt="staff-startups") if len(rows) > offset+50 else None
    return {"results": page, "next": cursor, "overdueAlerts": overdue_count}


def startup_detail(organization_id, *, now=None):
    """Resolve personal identity and website support evidence only for authorized staff."""
    now = now or timezone.now()
    org = _orgs().get(pk=organization_id)
    company = _companies([org]).get(org.pk)
    runs = list(ContentFactoryRun.objects.filter(organization=org).order_by("-created_at", "-pk")[:10])
    row = startup_row(org, company, runs[0] if runs else None, now=now)
    config = _config(org)
    website = config.website_connection if config else None
    row["runs"] = [{"runId": r.run_id, "status": r.status, "code": failure_code(r),
                    **{k: mapping(r.result).get(k) for k in RECEIPT_KEYS}} for r in runs]
    row["website"] = ({"id": str(website.pk), "state": website.state, "verifiedSha": website.verified_sha,
                       "repository": website.github_repo, "siteUrl": website.site_url,
                       "targets": list(website.targets.filter(generation=website.generation).values("target_key", "source_sha", "adapter", "contract")),
                       "doctor": None} if website else None)
    receipts = _alerts(org)
    row["alerts"] = [a for a in receipts if open_alert(a)] + sorted([a for a in receipts if not open_alert(a)], key=lambda a: a.get("acknowledged_at", ""), reverse=True)[:20]
    if website and company:
        from .website_doctor import read_doctor
        from .website_goal import scope_for
        op = website.operations.select_related("connection").filter(generation=website.generation,
            action="prepare", payload__company_id=str(company.pk)).order_by("-created_at", "-pk").first()
        if op:
            row["website"]["doctor"] = read_doctor(op, scope=scope_for(website, company.pk), source_sha=website.verified_sha)
    row["owner"] = {"userId": company.profile.user_id, "email": company.profile.user.email,
                    "name": company.profile.user.full_name} if company else None
    # Explicit allow-list: credentials and provider tokens never leave the config object.
    strategy = mapping(getattr(config, "pillar_strategy", None))
    row["support"] = {key: strategy.get(key) for key in ("website_support", "website_goal", "website_doctor", "self_heal")}
    return row


def alert_list(*, state="open", now=None):
    """Resolve organization labels at read time; global rule receipts have no startup."""
    now = now or timezone.now()
    result = []
    for org in _orgs().filter(content_config__isnull=False).order_by("pk"):
        for alert in _alerts(org):
            if overdue(alert, now) if state == "overdue" else open_alert(alert):
                result.append({**alert, "organizationId": org.pk, "startupName": org.name,
                               "ownerEmail": getattr(org.billing_user, "email", None)})
    result.extend(a for a in global_alerts(now=now) if (overdue(a, now) if state == "overdue" else open_alert(a)))
    return {"results": result, "overdueAlerts": sum(overdue(a, now) for a in result)}


def acknowledge_alert(alert_id, *, by_user):
    """Find the ids-only receipt and use the shared acknowledgement store."""
    from .founder_journey_alerts import acknowledge_founder_journey_alert
    from .staff_alert_rules import acknowledge_global
    require_staff(by_user)
    if alert_id.startswith("staff_alerts:global:"):
        return acknowledge_global(alert_id, by_user=by_user)
    row = OrganizationContentConfig.objects.select_related("organization").filter(pillar_strategy__ops_alerts__has_key=alert_id).first()
    return acknowledge_founder_journey_alert(row.organization, alert_id, by_user=by_user) if row else None


def _start_copy_ready(run, organization, *, key, carry):
    """Reserve a P1 portable continuation with carried billing before any provider effect."""
    from . import vibe_marketing_views as views
    from .dispatch_outbox import reserve_dispatch
    from .dispatch_models import ContentFactoryDispatchOutbox
    from founder_tools.services import founder_actor_id_for_user
    company = _companies([organization]).get(organization.pk)
    if company is None:
        return {"status": "refused", "detail": "This startup has no owning founder."}
    context = SimpleNamespace(organization=organization, company=company, profile=company.profile)
    config = _config(organization)
    from .run_ownership import admission_enabled
    from .run_staff_support import issued_staff_user, staff_support_required
    staff_user = issued_staff_user() if staff_support_required(run) else None
    requester = staff_user or company.profile.user
    actor = founder_actor_id_for_user(requester)
    try:
        payload = views._restart_article_payload_from_run(run=run, context=context, config=config, actor_id=actor)
    except ValueError:
        payload = None
    if not payload:
        return {"status": "refused", "detail": "Review the article topic and audience first."}
    payload.update(client_request_id=key, delivery_mode="content_only", delivery_mode_explicit=True, delivery_mode_confirmed=True,
                   roo_points_requested_by_user_id=requester.pk, mcp_company_id=str(company.pk))
    from .website_contract import CONNECTION_FIELDS
    for field in (*CONNECTION_FIELDS, "github_token", "source_sha", "expected_source_sha", "repo_head_sha", "branch", "app_root"):
        payload.pop(field, None)
    payload["github_repo"] = ""
    error = views._refresh_article_editorial_payload(organization=organization, payload=payload)
    if error is not None:
        return {"status": "refused", "detail": str(error.data.get("detail", "Review the article audience first."))}
    with transaction.atomic():
        Organization.objects.select_for_update().get(pk=organization.pk)
        source = ContentFactoryRun.objects.select_for_update().get(pk=run.pk, organization_id=organization.pk)
        if not VibeRaisingCompany.objects.filter(pk=company.pk, organization=organization,
                profile__user_id=company.profile.user_id).exists():
            raise ValueError("The startup owner changed before dispatch.")
        error = views._refresh_article_editorial_payload(organization=organization, payload=payload)
        if error is not None:
            raise ValueError("Review the current article audience before dispatch.")
        source_match = Q(payload__request__staff_switch_source_run_id=source.run_id)
        if staff_user is not None:
            source_match |= Q(payload__request__staff_switch_source_pk=source.pk)
        existing = ContentFactoryDispatchOutbox.objects.filter(source_match, organization=organization).first()
        if existing:
            if staff_user is not None:
                from .run_staff_support import require_staff_support
                from .run_ownership import admitted_run_for_key
                saved_child = admitted_run_for_key(existing.client_request_id, lock=True)
                if saved_child is None:
                    raise ValueError("The saved staff continuation has no current admission.")
                require_staff_support(saved_child)
            return {"status": "already_done", "detail": "Your draft is already queued.",
                    "receipt": {"runId": existing.run_id or existing.client_request_id, "channel": "copy_ready", "charged": False}}
        if staff_user is not None and not admission_enabled():
            raise ValueError("Starting a new admitted staff continuation is currently disabled.")
        prefix = f"staff-switch:{organization.pk}:"
        key = prefix + str(ContentFactoryDispatchOutbox.objects.filter(organization=organization,
            client_request_id__startswith=prefix).count() + 1)
        payload.update(client_request_id=key, staff_switch_source_run_id=source.run_id)
        if staff_user is not None:
            payload["staff_switch_source_pk"] = source.pk
        child, created = ContentFactoryRun.objects.get_or_create(run_id=key, defaults={"organization": organization,
            "domain": organization.domain, "workflow": "article_generation", "status": "queued", "run_request": payload})
        if child.organization_id != organization.pk:
            raise ValueError("The continuation belongs to another startup.")
        carried = carry(source_run=source, new_run=child)
        if carried.get("state") != "carried":
            raise ValueError("The original payment could not be carried.")
        payload = {**child.run_request, "dispatch_outbox_reserved": True}
        if staff_user is not None:
            from .p2_foundation import stamp_run_request
            from .run_ownership import seal_new_run_admission, user_principal
            from .run_staff_support import seal_staff_support
            payload["roo_points_requested_by_user_id"] = staff_user.pk
            stamp_run_request(payload, organization=organization, requester_user_id=staff_user.pk)
            ContentFactoryRun.objects.filter(pk=child.pk).update(run_request=payload)
            child.run_request = payload
            seal_new_run_admission(child, created=created, principal=user_principal(staff_user.pk, actor), fixed_run_id=False)
            seal_staff_support(child, source=source, company=company)
            payload = dict(child.run_request)
        else:
            ContentFactoryRun.objects.filter(pk=child.pk).update(run_request=payload)
        reserve_dispatch(organization=organization, payload=payload,
            billing_user_id=payload.get("roo_points_billing_user_id") or company.profile.user_id, actor_id=actor,
            **({"_staff_support_run": child} if staff_user is not None else {}))
    return {"status": "done", "detail": "Your draft is queued.", "receipt": {"runId": child.run_id, "channel": "copy_ready", "charged": False}}


def held_article_quality(run) -> bool:
    """An article whose hosted quality check stopped and is waiting on staff."""
    quality = mapping(mapping(run.result).get("article_preview_quality"))
    return bool(quality.get("status") == "blocking_findings" and quality.get("self_heal_exhausted"))


def _recheck_article_quality(run):
    """Run the article's hosted quality check again; a pass unlocks approval for the founder.

    This never waives a finding. It asks Content Factory for a fresh review of
    the current preview, which also clears a hold that a since-fixed rule set.
    """
    from .vibe_marketing_views import _call_content_factory_run_action
    remote = _call_content_factory_run_action(run_id=run.run_id, action="retry-preview-quality",
                                              payload={}, workflow=run.workflow)
    remote = remote if isinstance(remote, dict) else {}
    if remote.get("error"):
        return {"status": "refused", "detail": str(remote.get("error"))[:300]}
    with transaction.atomic():
        current = ContentFactoryRun.objects.select_for_update().get(pk=run.pk)
        result = dict(mapping(current.result))
        if isinstance(remote.get("article_preview_quality"), dict):
            result["article_preview_quality"] = remote["article_preview_quality"]
        result.pop("approval_blocker", None)
        ContentFactoryRun.objects.filter(pk=current.pk).update(result=result)
    return {"status": "queued", "action": "recheck_article_quality"}


def _perform(action, run, org, user, *, now, command_key=None):
    unavailable = {"status": "refused", "detail": "Not available yet.", "receipt": None}
    if action == "retry_now" and held_article_quality(run):
        receipt = _recheck_article_quality(run)
    elif action == "retry_now":
        decide, apply = provider("failure_policy_engine", "decide"), provider("failure_policy_engine", "apply")
        if not decide or not apply:
            return unavailable
        decision = decide(run, now=now)
        if is_dataclass(decision):
            decision = replace(decision, at=now)
        elif isinstance(decision, dict):
            decision = {**decision, "at": now}
        receipt = apply(run, decision, now=now)
    elif action == "switch_to_draft":
        switch, carry = provider("channel_switch", "switch_to_copy_ready"), provider("no_delivery_refunds", "carry_charge")
        if not switch or not carry:
            return unavailable
        from copy import deepcopy
        result = deepcopy(mapping(run.result))
        from .run_reconciliation import authoritative_run
        from .run_verified_delivery import verified_draft_ready
        delivered = verified_draft_ready(run) if authoritative_run(run) else bool(
            result.get("draft_ready_v1") or result.get("content_package") or run.status in {"completed", "awaiting_approval", "approval_required"})
        if delivered:
            receipt = switch(run=run, reason="staff_switch", now=now)
        else:
            previous = mapping(result.get("staff_switch_v1"))
            key = previous.get("client_request_id") or f"staff-switch:{org.pk}:{1+sum(a.get('action') == action for a in result.get('staff_actions_v1', []) if isinstance(a, dict))}"
            result["staff_switch_v1"] = {"client_request_id": key}
            from .run_provider_facts import save_provider_result
            save_provider_result(run, result, baseline=mapping(run.result), legacy_fields=["result"])
            try:
                from .run_staff_support import staff_action_authority, staff_support_required
                if staff_support_required(run):
                    with staff_action_authority(run, user=user, command_key=command_key):
                        return _start_copy_ready(run, org, key=key, carry=carry)
                return _start_copy_ready(run, org, key=key, carry=carry)
            except ValueError as exc:
                return {"status": "refused", "detail": str(exc), "receipt": None}
    elif action == "release_or_refund":
        decide, apply = provider("no_delivery_refunds", "refund_decision"), provider("no_delivery_refunds", "apply_refund_decision")
        if not decide or not apply:
            return unavailable
        receipt = apply(run, decide(run, now=now), now=now)
    else:
        notify = provider("founder_transitions", "notify_team_resolved")
        if not notify:
            return unavailable
        from .founder_journey_alerts import acknowledge_founder_journey_alert
        item = "website" if run.workflow == "article_system_setup" else "article"
        for alert in _alerts(org):
            if alert.get("run_id") == run.run_id or mapping(alert.get("details")).get("item") == item:
                acknowledge_founder_journey_alert(org, alert["alert_id"], by_user=user)
        with transaction.atomic():
            config = OrganizationContentConfig.objects.select_for_update().get(organization=org)
            strategy = mapping(config.pillar_strategy)
            alerts = dict(mapping(strategy.get("ops_alerts")))
            for alert_id, alert in alerts.items():
                if isinstance(alert, dict) and (alert.get("run_id") == run.run_id or mapping(alert.get("details")).get("item") == item):
                    alerts[alert_id] = {**alert, "resolved_at": now.isoformat(), "resolved_by_user_id": user.pk}
            OrganizationContentConfig.objects.filter(pk=config.pk).update(pillar_strategy={**strategy, "ops_alerts": alerts})
            count = notify(org, item, by_user=user)
        receipt = {"notified": count, "item": item}
    refused = isinstance(receipt, dict) and (receipt.get("status") in {"refused", "observe"} or receipt.get("action") in {"observe", "stop"})
    return {"status": "refused" if refused else "done", "detail": "Action refused by its safety policy." if refused else "Done.", "receipt": receipt}


def perform_action(organization_id, data, *, by_user):
    """Claim a scoped idempotency key before executing an audited staff operation."""
    from founder_tools import idempotency as idem
    from uuid import uuid4
    require_staff(by_user)
    action = data.get("action")
    if action not in ACTIONS or not isinstance(data.get("runId"), str):
        raise ValueError("Choose an action and runId.")
    scope = ("staff_action", organization_id, by_user.pk)
    key = data.get("idempotencyKey")
    digest = idem.request_sha256(data)
    state, previous = idem.begin(scope, key, digest)
    if state == idem.REPLAY:
        return previous["status"], previous["body"], True
    if state != idem.NEW:
        return 409, {"status": "refused", "detail": idem.CONFLICT_DETAIL, "reason": idem.CONFLICT_REASON}, False
    try:
        org = _orgs().get(pk=organization_id)
        run = ContentFactoryRun.objects.get(organization=org, run_id=data["runId"])
        now = timezone.now()
        # Serialize different action keys for the same run with a cache claim; never hold DB locks across providers.
        from django.core.cache import cache
        lock = f"staff-action:run:{run.pk}"
        lock_token = uuid4().hex
        if not cache.add(lock, lock_token, 300):
            idem.abandon(scope, key, digest)
            return 409, {"status": "refused", "detail": idem.CONFLICT_DETAIL, "reason": idem.CONFLICT_REASON}, False
        try:
            response = _perform(action, run, org, by_user, now=now, command_key=key)
            with transaction.atomic():
                current = ContentFactoryRun.objects.select_for_update().get(pk=run.pk, organization_id=org.pk)
                result = mapping(current.result)
                history = result.get("staff_actions_v1", [])
                history = history if isinstance(history, list) else []
                history = history[-19:] + [{"action": action, "at": now.isoformat(), "by_user_id": by_user.pk,
                    "note": str(data.get("note") or "")[:500], "outcome": response["status"]}]
                ContentFactoryRun.objects.filter(pk=current.pk).update(result={**result, "staff_actions_v1": history})
            idem.finish(scope, key, digest, status=200, body=response)
        finally:
            if cache.get(lock) == lock_token:
                cache.delete(lock)
        return 200, response, False
    except Exception:
        idem.abandon(scope, key, digest)
        raise
