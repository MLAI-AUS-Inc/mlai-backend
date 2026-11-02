"""Durable per-recipient founder transitions and reminders for P2 startups."""
from __future__ import annotations

from copy import deepcopy
from datetime import timedelta
import hashlib
import logging
import time

from django.conf import settings
from django.core.cache import cache
from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Exists, OuterRef, Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from . import founder_journey_p2 as projection, p2_foundation
from .models import ContentFactoryCallbackEvent, OrganizationContentConfig

logger = logging.getLogger(__name__)
EVENT_TYPE = "founder_transition"
LEASE = timedelta(minutes=10)
RETRY_WINDOW = timedelta(hours=23)
OWNER_ASKS = {"approve_setup", "authorize_github", "approve_join_request", "agree_terms", "describe_customer"}
mapping = projection.mapping


def owns_notifications(organization):
    """True only while this module owns the P2 startup's transition sends."""
    return bool(getattr(settings, "MY_STARTUP_TRANSITION_NOTIFICATIONS_ENABLED", False)
                and p2_foundation.p2_applies(organization))


def _owner(organization):
    from founder_tools.services import organization_owner_user_id
    return organization_owner_user_id(organization)


def resolve_recipients(organization, *, item, ask=None):
    """Fresh, active recipients; owner-only asks never inherit a broad team list."""
    owner_id = _owner(organization)
    candidates = projection.optional("founder_tools.startup_team", "notification_recipients",
        organization, item=item, ask=ask)
    if candidates is None:
        candidates = get_user_model().objects.filter(pk=owner_id, is_active=True)
    ids = {getattr(user, "pk", None) for user in candidates}
    owner_only = ask in OWNER_ASKS or (ask == "approve_on_github" and not item.startswith("article:"))
    if ask == "approve_on_github" and item.startswith("article:"):
        from workflow_runs.models import ContentFactoryRun
        run = ContentFactoryRun.objects.filter(run_id=item.split(":", 1)[1], organization=organization).first()
        if (run is None or run.workflow not in {"article_generation", "article_revision", "direct_generate", "confirmed_topic", "content_factory_article"}
                or mapping(mapping(run.result).get("publish_pr_v1")).get("kind") in {"setup", "scaffold", "proxy"}):
            owner_only = True
    if owner_only:
        ids &= {owner_id}
    users = get_user_model().objects.filter(pk__in=ids, is_active=True).order_by("pk")
    allowed = []
    for user in users:
        if user.pk == owner_id:
            allowed.append(user)
            continue
        if not getattr(settings, "MY_STARTUP_TEAM_ENABLED", False):
            continue
        if projection.optional("founder_tools.startup_team", "viewer_role", user, organization) != "member":
            continue
        if ask == "approve_on_github" and not projection.optional("founder_tools.startup_team", "member_may_publish", user, organization, default=False):
            continue
        allowed.append(user)
    return allowed


def logical_key(organization_id, item, event, state, user_id, channel):
    """The contract identity contains only server-owned identifiers and enum values."""
    return f"founder-transition:{organization_id}:{item}:{event}:{state}:{user_id}:{channel}"


def bounded_key(value, maximum):
    """Preserve exact keys when they fit; hash only the representation overflow."""
    return value if len(value) <= maximum else "founder-transition:" + hashlib.sha256(value.encode()).hexdigest()


def _moment(value):
    try:
        parsed = parse_datetime(value) if isinstance(value, str) else None
        return parsed if parsed and not timezone.is_naive(parsed) else None
    except ValueError:
        return None


def _company(organization):
    from founder_tools.models import VibeRaisingCompany
    return VibeRaisingCompany.objects.filter(organization=organization).order_by("created_at").first()


def _copy(event, state, organization):
    ask = state.get("ask")
    run = state.get("run")
    result = mapping(getattr(run, "result", None))
    title = str(result.get("article_title") or mapping(run.run_request if run else {}).get("title") or "Your article")[:180]
    host = str(organization.domain or "your website")[:200]
    if event.startswith("your_turn"):
        count = projection.claims_count(run) if run else None
        rows = {
            "approve_setup": ("Your blog page is ready", "Take a look, then add it to your website.", "Add to my website"),
            "approve_on_github": ("GitHub needs your approval", "Approve one change on GitHub for your blog.", "Approve on GitHub"),
            "authorize_github": ("MLAI needs access to your website on GitHub", "Give MLAI access on GitHub so we can keep going.", "Give access on GitHub"),
            "review_draft": ("Please check your draft", f"Your draft is ready. Please check the {count} highlighted sentences before you use it." if count else "Your draft is ready. Please check it before you use it.", "Check them" if count else "Read your article"),
            "describe_customer": ("Tell us about your customer", "One short answer and we'll start your article.", "Describe your customer"),
            "agree_terms": ("Please agree to our updated terms", "Our terms were just updated. Please tick to agree again.", "Agree to the terms"),
            "approve_join_request": ("Someone asked to join your team", "Review the request in MLAI.", "Review request"),
        }
        if ask == "buy_points":
            from .founder_journey import article_price_and_balance
            owner = get_user_model().objects.filter(pk=_owner(organization)).first()
            cost, balance = article_price_and_balance(organization, owner)
            missing = max(1, cost - balance)
            value = (f"You need {missing} more points", f"This article uses {cost} points. You have {balance}.", f"Get {missing} more points")
        else:
            value = rows.get(ask)
        if value and event != "your_turn":value = ("Reminder: " + value[0], value[1], value[2])
        return value
    if event == "blog_live":return "Your blog page is live", f"New articles can now go live on {host}.", "View your blog"
    if event == "article_live":return "Your article is live!", f"{title} is now on {host}.", "View article"
    if event == "article_stopped":
        outcome = projection.optional("content_factory.no_delivery_refunds", "points_outcome", run)
        line = projection.line("stopped", run=run, outcome=outcome)
        return "We couldn't write this one", line["body"], "Open your startup"
    return {
        "ladder_exhausted": ("Your website's checks didn't accept the blog page", "Your articles are still ready to copy.", "Open your startup"),
        "team_resolved": ("All sorted", "We fixed the problem and picked up where we left off.", "Open your startup"),
        "join_approved": ("You're on the team", "You're on the team for " + str(organization.name)[:120] + ".", "Open your startup"),
        "member_joined": ("Someone joined your team", str(state.get("first_name") or "Someone")[:100] + " joined " + str(organization.name)[:120] + ".", "Review your team"),
    }.get(event)


def _reserve(config, item, event, state, *, now, only_user=None):
    """Reserve metadata and ledger rows in the caller's organization transaction."""
    organization = config.organization
    company = _company(organization)
    copy = _copy(event, state, organization)
    if not company or not copy:
        return 0
    ask = state.get("ask")
    recipients = resolve_recipients(organization, item=item, ask=ask)
    if event in {"ladder_exhausted", "member_joined"}:
        recipients = [user for user in recipients if user.pk == _owner(organization)]
    if only_user is not None:
        if event == "join_approved" and projection.optional("founder_tools.startup_team", "viewer_role", only_user, organization) == "member":
            recipients = [only_user] if only_user.is_active else []
        else:
            recipients = [user for user in recipients if user.pk == only_user.pk]
    link = projection.destination(organization, company.pk, run_id=state.get("run_id"), ask=ask, item=item)
    if not link:
        return 0
    strategy = deepcopy(mapping(config.pillar_strategy))
    notices = strategy.setdefault("founder_notices", {"version": 1, "last": {}})
    outbox = notices.setdefault("outbox", {})
    created = 0
    for user in recipients:
        for channel in ("email", "dm"):
            full = logical_key(organization.pk, item, event, ask or state["state"], user.pk, channel)
            identifier = bounded_key(full, 100)
            if identifier in outbox:continue
            _, added = ContentFactoryCallbackEvent.objects.get_or_create(event_id=identifier, defaults={
                "job_id": str(organization.pk), "event_type": EVENT_TYPE, "emitted_at": now})
            if identifier not in outbox:
                outbox[identifier] = {"key": full, "item": item, "event": event, "state": state["state"], "ask": ask,
                    "user_id": user.pk, "channel": channel, "run_id": state.get("run_id"),
                    "subject": copy[0], "body": copy[1], "button": copy[2], "link": link,
                    "at": now.isoformat(), "next_at": now.isoformat()}
            created += int(added)
    OrganizationContentConfig.objects.filter(pk=config.pk).update(pillar_strategy=strategy)
    config.pillar_strategy = strategy
    return created


def item_state(organization, run, *, config, now):
    """A notification item reads durable state, without the full dashboard queries."""
    decision = projection.run_decision(organization, run, config=config, user=None, now=now)
    receipt = mapping(mapping(run.result).get("publish_pr_v1"))
    if (run.workflow in {"article_system_setup", "hosted_native_verification"}
            and not mapping(run.result).get("merge_v1") and receipt.get("kind") in {"setup", "scaffold", "proxy"}):
        approval = mapping(mapping(run.result).get("owner_merge_approval_v1"))
        if not approval or mapping(mapping(mapping(run.result).get("self_heal_v1")).get("merge_worker")).get("manual"):
            decision = {"state": "your_turn", "action": {"id": "approve_on_github" if approval else "approve_setup"}}
    if decision is None:
        if run.status in {"completed", "awaiting_approval", "approval_required"} and run.approval_state != "approved":
            decision = {"state": "your_turn", "action": {"id": "review_draft"}}
        else:
            decision = {"state": "working", "action": None}
    return {"state": decision["state"], "ask": mapping(decision.get("action")).get("id"),
        "copyId": decision.get("copyId"), "run_id": run.run_id, "run": run}


def website_item_state(config, *, now, setup_run=None):
    """Current website asks/live proof, including goals with no CF run yet."""
    from . import founder_journey_cohort as cohorts, registry_policy
    from .website_goal_notifications import notice_for
    from .website_models import WebsiteConnectionOperation
    organization = config.organization
    connection = getattr(config, "website_connection", None)
    if connection and connection.organization_id == organization.pk:
        if connection.state == "revoked":
            return {"state": "your_turn", "ask": "authorize_github", "run_id": None}
        operation = WebsiteConnectionOperation.objects.filter(connection=connection, generation=connection.generation,
            action="prepare").order_by("-created_at", "-pk").first()
        notice = notice_for(operation, now=now) if operation else None
        if notice and notice["state"] == "needs_you":
            ask = registry_policy.canonical_action_id(notice.get("action"))
            if ask in OWNER_ASKS | {"approve_on_github", "review_draft"}:
                return {"state": "your_turn", "ask": ask, "run_id": None}
        if (notice and notice["state"] == "done") or cohorts.has_connected_website(organization):
            return {"state": "done", "ask": None, "verified_live": True, "run_id": None}
    if setup_run is None:
        if connection is None:
            return None
        from workflow_runs.models import ContentFactoryRun
        setup_run = ContentFactoryRun.objects.filter(organization=organization,
            workflow__in={"article_system_setup", "hosted_native_verification"}).order_by("-created_at", "-pk").first()
    return item_state(organization, setup_run, config=config, now=now) if setup_run else None


def observe(config, item, state, *, now):
    """CAS-like observation under the organization row lock queues a transition once."""
    notices = deepcopy(mapping(mapping(config.pillar_strategy).get("founder_notices")))
    last = notices.setdefault("last", {})
    previous = mapping(last.get(item))
    event_kind = "your_turn" if state["state"] == "your_turn" and state.get("ask") else "article_stopped" if state.get("copyId") == "stopped" else "ladder_exhausted" if state.get("copyId") == "done_copy_ready_ladder_exhausted" else None
    if state["state"] == "done" and mapping(mapping(getattr(state.get("run"), "result", None)).get("deploy_v1")).get("url"):
        event_kind = "article_live" if item.startswith("article:") else "blog_live"
    if state.get("verified_live") and item.startswith("website:"):event_kind = "blog_live"
    same = (previous.get("state"), previous.get("ask"), previous.get("event")) == (state["state"], state.get("ask"), event_kind)
    event = None
    if not same:
        last[item] = {"state": state["state"], "ask": state.get("ask"), "event": event_kind, "at": now.isoformat()}
        event = event_kind
    if same and not (state["state"] == "your_turn" and getattr(settings, "MY_STARTUP_TURN_REMINDERS_ENABLED", False)):
        return 0
    strategy = {**mapping(config.pillar_strategy), "founder_notices": {**notices, "version": 1}}
    OrganizationContentConfig.objects.filter(pk=config.pk).update(pillar_strategy=strategy)
    config.pillar_strategy = strategy
    created = _reserve(config, item, event, state, now=now) if event else 0
    if same and state["state"] == "your_turn" and state.get("ask") and getattr(settings, "MY_STARTUP_TURN_REMINDERS_ENABLED", False):
        since = _moment(previous.get("at"))
        if since:
            for hours in (24, 72):
                if now - since >= timedelta(hours=hours):
                    created += _reserve(config, item, f"your_turn_reminder_{hours}h", state, now=now)
    return created


def _current(config, notice, *, now):
    """Re-project a reminder/current ask and re-resolve recipient rights before sends."""
    organization = config.organization
    if not owns_notifications(organization):return None
    user = next((user for user in resolve_recipients(organization, item=notice["item"], ask=notice.get("ask")) if user.pk == notice["user_id"]), None)
    if notice["event"] == "join_approved":
        candidate = get_user_model().objects.filter(pk=notice["user_id"], is_active=True).first()
        user = candidate if projection.optional("founder_tools.startup_team", "viewer_role", candidate, organization) == "member" else None
    if notice["event"] in {"ladder_exhausted", "member_joined"} and notice["user_id"] != _owner(organization):return None
    if user is None:return None
    if notice["event"] in {"your_turn", "your_turn_reminder_24h", "your_turn_reminder_72h"}:
        from workflow_runs.models import ContentFactoryRun
        if notice["item"].startswith("website:"):
            state = website_item_state(config, now=now)
            return user if state and state.get("state") == "your_turn" and state.get("ask") == notice.get("ask") else None
        run = ContentFactoryRun.objects.filter(run_id=notice.get("run_id"), organization=organization).first()
        if run is not None:
            from .founder_journey import is_hidden_run
            if run.status in {"cancelled", "denied"} or is_hidden_run(run):return None
            state = item_state(organization, run, config=config, now=now)
            if state.get("state") != "your_turn" or state.get("ask") != notice.get("ask"):return None
        elif notice.get("ask") == "approve_join_request":
            pending = projection.optional("founder_tools.startup_team", "pending_request_ids", organization, default=[])
            if notice["item"].removeprefix("team:") not in pending:return None
        else:return None
    return user


def _checkpoint(config, row, notice):
    from organizations.models import Organization
    with transaction.atomic():
        Organization.objects.select_for_update().get(pk=config.organization_id)
        current = OrganizationContentConfig.objects.select_for_update().get(pk=config.pk)
        if not ContentFactoryCallbackEvent.objects.filter(pk=row.pk, claimed_at=row.claimed_at, processed_at__isnull=True).exists():
            raise RuntimeError("Notification claim changed")
        strategy = deepcopy(mapping(current.pillar_strategy))
        strategy.setdefault("founder_notices", {}).setdefault("outbox", {})[row.event_id] = deepcopy(notice)
        OrganizationContentConfig.objects.filter(pk=current.pk).update(pillar_strategy=strategy)


def _deliver(row, config, notice, *, now):
    from . import website_notification_transport as transport
    from .founder_notification_outbox import _devices, dm_event, _sender
    user = _current(config, notice, now=now)
    if user is None:return "skipped"
    if notice["channel"] == "email":
        if not projection.verified_email(user):return "skipped"
        if not notice.get("payload"):
            notice["payload"] = {"from": _sender(), "to": [user.email], "subject": notice["subject"],
                "text": notice["body"] + "\n\n" + notice["button"] + ": " + notice["link"]}
            _checkpoint(config, row, notice)
        # A changed email may not borrow a frozen message or idempotency key.
        user = _current(config, notice, now=timezone.now())
        if not projection.verified_email(user) or notice["payload"].get("to") != [user.email]:return "skipped"
        transport.post_email(notice["payload"], bounded_key(notice["key"], 256), exact_key=True)
        return "sent"
    devices = _devices(user)
    if not devices or len(devices) > 8:return "skipped"
    if notice.get("devices") is not None and notice["devices"] != devices:return "skipped"
    if not notice.get("channel_id"):
        notice["devices"] = devices
        _checkpoint(config, row, notice)
        notice["channel_id"] = transport.open_dm(devices, row.event_id)
    if not notice.get("event_payload"):
        from agent_runtime import native_crypto
        notice["event_payload"] = native_crypto.sign_event(transport.notifier_key(), kind=9,
            created_at=int(row.created_at.timestamp()), tags=[["h", notice["channel_id"]], ["mlai-founder-transition", row.event_id]] + [["p", key] for key in devices],
            content=notice["body"] + "\n\n[" + notice["button"] + "](" + notice["link"] + ")")
        _checkpoint(config, row, notice)
    user = _current(config, notice, now=timezone.now())
    if user is None or _devices(user) != devices:return "skipped"
    transport.post_event(notice["event_payload"])
    return "sent"


def drain(now=None, *, limit=25, clock=time.monotonic):
    """Recover lost acknowledgements with frozen payloads inside the provider window."""
    now = now or timezone.now()
    counts = {"sent": 0, "skipped": 0, "retry": 0}
    rows = ContentFactoryCallbackEvent.objects.filter(event_type=EVENT_TYPE, processed_at__isnull=True).filter(
        Q(claimed_at__isnull=True) | Q(claimed_at__lt=now - LEASE)).order_by("created_at")[:max(0, min(limit, 100))]
    deadline = clock() + 1.5
    for row in rows:
        if clock() >= deadline:break
        config = OrganizationContentConfig.objects.select_related("organization").filter(organization_id=row.job_id).first()
        notice = deepcopy(mapping(mapping(mapping(getattr(config, "pillar_strategy", None)).get("founder_notices")).get("outbox")).get(row.event_id))
        due = _moment(notice.get("next_at"))
        if due and due > now:continue
        claimed = ContentFactoryCallbackEvent.objects.filter(pk=row.pk, processed_at__isnull=True).filter(
            Q(claimed_at__isnull=True) | Q(claimed_at__lt=now - LEASE)).update(claimed_at=now)
        if not claimed:continue
        row.claimed_at = now
        try:
            # Never retry outside Resend's 24-hour idempotency retention.
            if not config or not notice or now - row.created_at >= RETRY_WINDOW:
                outcome = "skipped"
            else:
                outcome = _deliver(row, config, notice, now=now)
        except Exception as exc:
            from .website_notification_transport import NotificationTransportError
            if isinstance(exc, NotificationTransportError) and not exc.retryable:
                ContentFactoryCallbackEvent.objects.filter(pk=row.pk, claimed_at=now).update(processed_at=now, claimed_at=None)
                counts["skipped"] += 1
                continue
            logger.warning("founder_transition_retry event_id=%s error_type=%s", row.event_id, type(exc).__name__)
            if config and notice:
                notice["next_at"] = (now + timedelta(minutes=2)).isoformat()
                _checkpoint(config, row, notice)
            ContentFactoryCallbackEvent.objects.filter(pk=row.pk, claimed_at=now).update(claimed_at=None)
            counts["retry"] += 1
            continue
        ContentFactoryCallbackEvent.objects.filter(pk=row.pk, claimed_at=now).update(processed_at=now, claimed_at=None)
        counts[outcome] += 1
    return counts


def run_transition_sweep(now=None, limit=None):
    """Observe bounded current P2 items, queue once, then drain recipient messages."""
    if not getattr(settings, "MY_STARTUP_TRANSITION_NOTIFICATIONS_ENABLED", False):
        return {"skipped": "flag_off"}
    from organizations.models import Organization
    from workflow_runs.models import ContentFactoryRun
    from . import founder_journey as journeys
    from . import registry_policy
    now = now or timezone.now()
    ceiling = max(0, min(int(limit if limit is not None else 50), 500))
    cursor = cache.get("founder-transitions:cursor", 0)
    cursor = cursor if type(cursor) is int else 0
    query = OrganizationContentConfig.objects.select_related("organization").annotate(
        has_runs=Exists(ContentFactoryRun.objects.filter(organization_id=OuterRef("organization_id")))).filter(
        Q(has_runs=True) | Q(website_connection__isnull=False) | Q(pillar_strategy__founder_notices__isnull=False) | Q(pillar_strategy__startup_team__requests__isnull=False)).order_by("organization_id")
    configs = list(query.filter(organization_id__gt=cursor)[:ceiling])
    if not configs and cursor:configs = list(query[:ceiling])
    if configs:cache.set("founder-transitions:cursor", configs[-1].organization_id, 3600)
    created = checked = 0
    for candidate in configs:
        if not registry_policy.cohort_for_report(candidate.organization, candidate):continue
        with transaction.atomic():
            Organization.objects.select_for_update().get(pk=candidate.organization_id)
            config = OrganizationContentConfig.objects.select_for_update().get(pk=candidate.pk)
            config.organization = candidate.organization
            checked += 1
            runs = ContentFactoryRun.objects.filter(organization=config.organization).order_by("-created_at", "-pk")[:20]
            setup_run = None
            for run in runs:
                if journeys.is_hidden_run(run):continue
                article = run.workflow in {"article_generation", "direct_generate", "confirmed_topic", "article_revision", "content_factory_article"}
                setup = run.workflow in {"article_system_setup", "hosted_native_verification"}
                if not article and not setup:continue
                if setup:
                    setup_run = run
                    break
                with registry_policy.organization_read_scope(config.organization, config):
                    state = item_state(config.organization, run, config=config, now=now)
                created += observe(config, "article:" + run.run_id, state, now=now)
            website = website_item_state(config, now=now, setup_run=setup_run)
            if website is not None:
                created += observe(config, "website:" + str(config.organization_id), website, now=now)
            requests = mapping(mapping(config.pillar_strategy).get("startup_team")).get("requests")
            pending = projection.optional("founder_tools.startup_team", "pending_request_ids", config.organization, default=[]) if requests and getattr(settings, "MY_STARTUP_TEAM_ENABLED", False) else []
            for identifier in pending:
                created += observe(config, "team:" + str(identifier), {"state": "your_turn", "ask": "approve_join_request"}, now=now)
    return {"checked": checked, "queued": created, **drain(now)}


def _notify(organization, item, event, *, only_user=None, first_name=None):
    if not owns_notifications(organization):return 0
    from organizations.models import Organization
    with transaction.atomic():
        Organization.objects.select_for_update().get(pk=organization.pk)
        config = OrganizationContentConfig.objects.select_for_update().filter(organization=organization).first()
        if config is None:return 0
        config.organization = organization
        return _reserve(config, item, event, {"state": event, "first_name": first_name}, now=timezone.now(), only_user=only_user)


def notify_team_resolved(organization, item, *, by_user):
    """Staff's audited resolution queues one private message per current recipient."""
    if not getattr(by_user, "is_active", False) or not getattr(by_user, "is_staff", False):return 0
    return _notify(organization, item, "team_resolved")


def notify_join_approved(organization, *, member_user):
    """Notify only the newly approved member, after current membership resolves."""
    return _notify(organization, "team:approved:" + str(member_user.pk), "join_approved", only_user=member_user)


def notify_member_joined(organization, *, member_user):
    """The owner sees a first name; no member email crosses recipient boundaries."""
    if projection.optional("founder_tools.startup_team", "viewer_role", member_user, organization) != "member":return 0
    first = str(getattr(member_user, "first_name", "") or "").split()
    name = first[0] if first and "@" not in first[0] else "Someone"
    return _notify(organization, "team:joined:" + str(member_user.pk), "member_joined", first_name=name)
