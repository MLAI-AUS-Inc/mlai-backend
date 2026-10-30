"""P1.1 and P1.2: the server-owned founder journey (IF-P1-03).

One poll replaces the dashboard's parallel reads. For a new-journey startup it
says which of 7 stages the founder is at, which of the 3 states that is
(Working on it, Your turn, Done), and the single next button: an ask when it
is the founder's turn (only the 8 asks of contract section 3), otherwise one
next step. Copy is the exact founder copy of contract section 11.2, rendered
verbatim by the client.

The legacy cohort short-circuits before any expensive read, so La Vie,
TalaThrive, Vyavos, PawShare, mlai.au and every other legacy organisation get
``stage: "legacy"`` and the client keeps today's pages.

The body never carries a failure-registry key (no ``code``-style keys): a
registry action is mapped to an ask here, and a run's fingerprint is exposed
only as the opaque ``supportCode``.
"""
from __future__ import annotations

import hashlib
import importlib
import logging
import math
from datetime import timedelta

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from . import founder_journey_cohort as cohorts

logger = logging.getLogger(__name__)

VERSION = 1

# The 8 asks (contract section 3). Nothing else is ever sent as action.id.
FOUNDER_ASKS = ("review_draft", "describe_customer", "agree_terms", "authorize_github", "approve_setup",
                "approve_on_github", "buy_points", "approve_join_request")
NEXT_KINDS = ("add_website", "write_first_article", "write_article", "view_progress", "view_draft", "view_article")
STAGES = ("website", "first_article", "writing", "review", "setup", "publishing", "done")
HOLDING_REASONS = ("slow", "website_update", "busy_today")

# Failure-registry action ids mapped to founder asks (contract section 3).
REGISTRY_ASKS = {
    "review_setup": "approve_setup",
    "merge_pr": "approve_on_github",
    "review_github_permissions": "authorize_github",
    "review_github_app_permissions": "authorize_github",
    "authorize_github": "authorize_github",
    "review_draft": "review_draft",
    "buy_points": "buy_points",
}
# Registry actions the new journey no longer asks the founder for. They show
# Working on it (slow) and log one founder_journey_retired_action line.
RETIRED_ACTIONS = frozenset({"choose_articles_page", "configure_website", "deploy_site", "remove_setup_example",
                             "provide_credentials"})
SETUP_ASKS = frozenset({"approve_setup", "authorize_github", "approve_on_github"})

POLL_AFTER_MS = {"working": 5000, "publishing": 15000, "your_turn": 30000, None: 30000, "done": 60000,
                 "legacy": 60000}
SLOW_AFTER_CAP_SECONDS = 1800
GITHUB_RETURN_WINDOW = timedelta(minutes=15)
WEBSITE_CACHE_SECONDS = 15
WEBSITE_CACHE_PREFIX = "founder-journey:website:"
LATEST_RUN_SCAN = 20

ACTIVE_STATUSES = frozenset({"queued", "running", "processing", "pending", "starting", "in_progress",
                             "awaiting_delivery_mode", "awaiting_confirmation"})
FAILED_STATUSES = frozenset({"failed", "blocked", "denied"})
DELIVERED_STATUSES = frozenset({"completed", "awaiting_approval", "approval_required"})
SETUP_WORKFLOWS = ("article_system_setup",)
SETUP_PREVIEW_READY_STATUSES = frozenset({"awaiting_approval", "approval_required"})
SETUP_STOPPED_STATUSES = frozenset({"failed", "cancelled", "denied"})
HIDDEN_CANCELLATIONS = frozenset({"superseded", "article_discarded", "draft_deleted"})
OWNER_CANCELLATIONS = frozenset({"owner_cancelled", "draft_deleted", "article_discarded"})

# Founder copy, contract section 11.2 (exact strings; no em or en dashes).
EMAIL_CLAUSE = "We'll email you when it's ready."
COPY = {
    "website": ("What's your startup's website?", "We'll read it and fill in the rest.", "Add your website"),
    "first_free_topic": ("Your first article is on us", "Here's your first article: {topic}", "Write my first article · free"),
    "first_free": ("Your first article is on us", "Pick a topic and we'll write it.", "Write my first article · free"),
    "first_paid": ("Your articles", "Pick a topic and we'll write it.", "Write an article · {price} points"),
    "daily_cap": ("Your first article is on us", "Free first articles are busy today. Please try again tomorrow.", None),
    "failed_free": ("Your first article is on us",
                    "We couldn't write this one. Your free article is still waiting for you.",
                    "Write my first article · free"),
    "failed_refunded": ("Your articles", "We couldn't write this one. Your points are back.",
                        "Write an article · {price} points"),
    "failed_refund_pending": ("Your articles", "We couldn't write this one. We're returning your points.",
                              "Write an article · {price} points"),
    "failed": ("Your articles", "We couldn't write this one.", "Write an article · {price} points"),
    "describe_customer": ("Who is your typical customer?", "We'll write your first article for them.",
                          "Describe your customer"),
    "buy_points": ("This article uses {price} points. You have {balance}.", None, "Get {shortfall} more points"),
    "agree_terms": ("Our terms were just updated", "Please tick to agree again.", "Review the terms"),
    "writing_queued": ("Working on it", "You're {ordinal} in line · about {minutes} minutes.", "See progress"),
    "writing_queued_no_eta": ("Working on it", "You're {ordinal} in line.", "See progress"),
    "writing_running": ("Working on it", "Usually about {minutes} minutes.", "See progress"),
    "writing_running_no_eta": ("Working on it", "Writing your article.", "See progress"),
    "writing_slow": ("Working on it", "Taking a little longer than usual.", "See progress"),
    "writing_retrying": ("Working on it", "Taking a little longer than usual.", "See progress"),
    "review": ("Your draft is ready", "Tap anything to suggest a change, then copy it to your website.",
               "Review your draft"),
    "approve_setup": ("Your blog page is ready", "Take a look, then add it to your website.", "Take a look"),
    "authorize_github": ("We need permission to read your website on GitHub", None, "Give access on GitHub"),
    "approve_on_github": ("Your change is waiting on GitHub", "Approve it on GitHub when you're ready.",
                          "Open on GitHub"),
    "publishing": ("Publishing…", "Waiting for your website to update.", None),
    "done_live": ("Your article is live!", None, "View article"),
    "done_copy_ready": ("Your article is ready to post", None, "Open your article"),
    # Shared fallback for an unknown or retired action (contract section 11.1).
    "still_working": ("Working on it", "Still working.", "See progress"),
}
# Bodies that carry the email clause when the draft-ready outbox can email.
EMAIL_CLAUSE_CASES = frozenset({"writing_queued", "writing_queued_no_eta", "writing_running",
                                "writing_running_no_eta", "writing_slow"})


def api_enabled() -> bool:
    """True when ``MY_STARTUP_FOUNDER_JOURNEY_API_ENABLED`` is on (code default off)."""
    return bool(getattr(settings, "MY_STARTUP_FOUNDER_JOURNEY_API_ENABLED", False))


def _mapping(value):
    return value if isinstance(value, dict) else {}


def ordinal(number: int) -> str:
    """1st, 2nd, 3rd, 4th, ... 11th, 12th, 13th, 21st."""
    number = int(number)
    if 10 <= number % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(number % 10, "th")
    return f"{number}{suffix}"


def minutes(seconds) -> int:
    """Whole minutes, rounded up, at least 1."""
    return max(1, math.ceil(max(0, float(seconds or 0)) / 60))


def founder_ask(registry_action_id) -> str | None:
    """The founder ask for a registry action id (contract section 3), else None."""
    return REGISTRY_ASKS.get(str(registry_action_id or "").strip())


def etag_for(payload: dict) -> str:
    """First 32 hex chars of sha256 over the canonical bytes of the payload without its etag."""
    from .website_failure_contract import canonical_bytes
    body = {key: value for key, value in payload.items() if key != "etag"}
    return hashlib.sha256(canonical_bytes(body)).hexdigest()[:32]


def matches_if_none_match(header, etag: str) -> bool:
    """Whether an ``If-None-Match`` header names this ETag (quoted, ``W/`` tolerated, lists and ``*``)."""
    if not header or not etag:
        return False
    for item in str(header).split(","):
        value = item.strip()
        if value == "*":
            return True
        if value[:2] in {"W/", "w/"}:
            value = value[2:]
        if value.strip().strip('"') == etag:
            return True
    return False


def _with_etag(payload: dict) -> dict:
    payload["etag"] = etag_for(payload)
    return payload


def legacy_payload(company_id) -> dict:
    """The legacy short-circuit body (no reads beyond the cohort)."""
    return _with_etag({
        "version": VERSION, "companyId": str(company_id), "cohort": cohorts.LEGACY, "stage": "legacy",
        "state": None, "headline": None, "body": None, "holdingReason": None, "action": None, "next": None,
        "runId": None, "etaSeconds": None, "queuePosition": None, "draftReadyNotifications": False,
        "switchTopicAvailable": False, "githubReturn": None, "website": None, "firstArticle": None,
        "pollAfterMs": POLL_AFTER_MS["legacy"], "supportCode": None,
    })


# ---------------------------------------------------------------------------
# Lazy sibling modules (be-draft and be-github-b are not in this base).


def _optional_module(name):
    try:
        return importlib.import_module(name)
    except ImportError:
        return None


def draft_ready_notifications(organization, user) -> bool:
    """IF-P1-11 ``can_notify`` through a lazy import; a missing module or an error means False."""
    module = _optional_module("content_factory.founder_notification_outbox")
    can_notify = getattr(module, "can_notify", None)
    if can_notify is None:
        return False
    try:
        return bool(can_notify(organization, user))
    except Exception:
        logger.warning("founder_journey_can_notify_failed org=%s", getattr(organization, "pk", None), exc_info=True)
        return False


def _owner_click_module():
    return _optional_module("content_factory.website_setup_owner_approval")


def _requires_owner_click(organization) -> bool:
    module = _owner_click_module()
    check = getattr(module, "requires_owner_click", None)
    if check is None:
        return False
    try:
        return bool(check(organization))
    except Exception:
        logger.warning("founder_journey_owner_click_check_failed org=%s", getattr(organization, "pk", None),
                       exc_info=True)
        return False


def terms_out_of_date(user) -> bool:
    """Rule 2: the member accepted community terms whose version is no longer current.

    Read from the member's ``CommunityMemberProfile`` the way
    ``community_chat.onboarding.onboarding_payload`` reads it. A member who
    never accepted any version (no profile, or an empty version) is not asked
    here: admission already handles that.
    """
    current = str(getattr(settings, "COMMUNITY_CHAT_MEMBERSHIP_POLICY_VERSION", "") or "")
    if not current or not getattr(user, "pk", None):
        return False
    try:
        from community_chat.models import CommunityMemberProfile
        accepted = (CommunityMemberProfile.objects.filter(user_id=user.pk, adult_confirmed_at__isnull=False)
                    .values_list("policy_version", flat=True).first())
    except Exception:
        logger.warning("founder_journey_terms_unavailable user=%s", getattr(user, "pk", None), exc_info=True)
        return False
    return bool(accepted) and accepted != current


# ---------------------------------------------------------------------------
# Run facts


def _result(run):
    return _mapping(getattr(run, "result", None))


def _request(run):
    return _mapping(getattr(run, "run_request", None))


def failure_class(run) -> str:
    """The run's registry failure class (``needs_user``, ``transient``, ...), or ''."""
    result = _result(run)
    failure = _mapping(result.get("failure"))
    return str(result.get("failure_class") or failure.get("failure_class") or "").strip()


def registry_action_id(run) -> str:
    """The registry action a stopped run names, or ''."""
    from .founder_journey_alerts import run_action_id
    return run_action_id(_result(run))


def _support_code(run):
    result = _result(run)
    failure = _mapping(result.get("failure"))
    for value in (result.get("failure_fingerprint"), failure.get("fingerprint"), result.get("failure_occurrence_id"),
                  failure.get("occurrence_id")):
        if isinstance(value, str) and value.strip():
            return value.strip()[:128]
    return None


def is_hidden_run(run) -> bool:
    """A superseded (switch-topic), discarded or deleted run is skipped as the latest run."""
    from .run_cancellation import hidden_by_draft_delete
    result = _result(run)
    if str(result.get("cancellation_reason") or "") in HIDDEN_CANCELLATIONS and run.status == "cancelled":
        return True
    return hidden_by_draft_delete(run.status, result)


def is_owner_cancel(run) -> bool:
    """An owner's own cancel, which is never a failure to explain."""
    if run.status != "cancelled":
        return False
    result = _result(run)
    if "cancellation_reason" in result:
        return result.get("cancellation_reason") in OWNER_CANCELLATIONS or result.get("cancelled_by") == "owner"
    return not (run.error or "")


def is_retrying(run) -> bool:
    """A failed or blocked run that Content Factory will run again (not terminal)."""
    from .run_state import active_retry_signal
    if run.status not in FAILED_STATUSES:
        return False
    result = _result(run)
    if _mapping(result.get("recovery")).get("state") == "pending":
        return True
    return active_retry_signal(result)


def latest_article_run(organization):
    """The newest article run that is not superseded, discarded or a publish child, or None."""
    from workflow_runs.models import ContentFactoryRun
    from .run_state import ARTICLE_WORKFLOWS
    from .vibe_marketing_views import _is_publish_child_run
    runs = (ContentFactoryRun.objects.filter(organization_id=organization.pk, workflow__in=ARTICLE_WORKFLOWS)
            .order_by("-created_at", "-id")[:LATEST_RUN_SCAN])
    for run in runs:
        if is_hidden_run(run) or _is_publish_child_run(run):
            continue
        return run
    return None


def delivery_mode(run) -> str:
    result, request = _result(run), _request(run)
    return str(result.get("delivery_mode") or request.get("resolved_delivery_mode")
               or request.get("delivery_mode") or "").strip()


def posted_record(config, run_id) -> dict:
    """IF-P1-13 ``founder_posted_articles[run_id]`` read as JSON (written by be-draft)."""
    strategy = getattr(config, "pillar_strategy", None)
    records = _mapping(strategy.get(cohorts.POSTED_ARTICLES_KEY)) if isinstance(strategy, dict) else {}
    return _mapping(records.get(str(run_id)))


def copied_or_posted(config, run_id) -> bool:
    record = posted_record(config, run_id)
    return bool(record.get("copiedAt") or record.get("url"))


def _run_started_at(run):
    started = None
    try:
        started = min((step.started_at for step in run.steps.all() if step.started_at), default=None)
    except Exception:
        started = None
    return started or run.created_at


def _median_seconds(run, now):
    from .run_queue_position import _cached_median_run_seconds
    queue_name = str(_result(run).get("queue_name") or "").strip()
    if not queue_name:
        return None
    try:
        return _cached_median_run_seconds(queue_name, now=now)
    except Exception:
        logger.warning("founder_journey_median_unavailable run=%s", run.run_id, exc_info=True)
        return None


def _rounded_eta(seconds):
    """Round up to whole minutes so the ETag stays stable within a minute."""
    if seconds is None:
        return None
    return minutes(seconds) * 60


def writing_facts(run, *, now) -> dict:
    """Queue position, ETA and slowness of an in-flight article run."""
    from .run_queue_position import queue_position_payload
    queue = queue_position_payload(run, now=now) if run.status == "queued" else {}
    p50 = _median_seconds(run, now)
    slow_after = min(2 * p50, SLOW_AFTER_CAP_SECONDS) if p50 else SLOW_AFTER_CAP_SECONDS
    elapsed = max(0.0, (now - _run_started_at(run)).total_seconds())
    retrying = is_retrying(run)
    if queue.get("queuePosition"):
        eta = queue.get("queueEtaSeconds")
    elif p50:
        eta = max(0.0, p50 - elapsed)
    else:
        eta = None
    return {"queuePosition": queue.get("queuePosition"), "etaSeconds": _rounded_eta(eta),
            "slow": retrying or elapsed > slow_after, "retrying": retrying}


# ---------------------------------------------------------------------------
# Money


def _refund_case(run) -> str:
    """'refunded', 'refund_pending' or 'none', read from the run's points ledger rows."""
    from roo.models import Ledger
    key = str(_request(run).get("client_request_id") or "").strip()
    if not key:
        return "none"
    charged = Ledger.objects.filter(idempotency_key=f"content_factory:charge:{key}", kind="SPEND").exists()
    if not charged:
        return "none"
    if Ledger.objects.filter(idempotency_key=f"content_factory:refund:{key}", kind="REFUND").exists():
        return "refunded"
    return "refund_pending"


def article_price_and_balance(organization, user) -> tuple[int, int]:
    """The article price for this founder and their spendable balance in points."""
    from .vibe_marketing_views import _roo_points_balance_for_user, get_content_factory_article_cost_points
    try:
        price = int(get_content_factory_article_cost_points(organization.domain, user=user) or 0)
    except Exception:
        logger.warning("founder_journey_price_unavailable org=%s", organization.pk, exc_info=True)
        price = 0
    try:
        balance = int(_roo_points_balance_for_user(user) or 0)
    except Exception:
        logger.warning("founder_journey_balance_unavailable org=%s", organization.pk, exc_info=True)
        balance = 0
    return price, balance


# ---------------------------------------------------------------------------
# Website facts


def _site_profile(config) -> dict:
    strategy = getattr(config, "pillar_strategy", None)
    state = _mapping(strategy.get(cohorts.FIRST_PROFILE_KEY)) if isinstance(strategy, dict) else {}
    return _mapping(state.get("siteProfile"))


def _https_or_none(value):
    text = str(value or "").strip()
    return text if text.startswith("https://") and len(text) <= 2048 else None


def _connection(organization):
    from .website_models import WebsiteConnection
    return WebsiteConnection.objects.filter(organization_id=organization.pk).order_by("-updated_at").first()


def _setup_run(organization, run_id=None):
    from workflow_runs.models import ContentFactoryRun
    runs = ContentFactoryRun.objects.filter(organization_id=organization.pk, workflow__in=SETUP_WORKFLOWS)
    if run_id:
        return runs.filter(run_id=run_id).first()
    return runs.order_by("-created_at", "-id").first()


def _website_journey_facts(*, organization, company, config, user, connection, latest_run_at):
    """The few website-journey facts the founder journey needs, cached per org for 15 s.

    Only called when a WebsiteConnection exists. Capabilities are read with
    ``allow_probe=False``; any error means no facts (the setup asks then come
    from the setup run alone).
    """
    key = (f"{WEBSITE_CACHE_PREFIX}{organization.pk}:{getattr(connection, 'configuration_version', 0)}:"
           f"{latest_run_at.isoformat() if latest_run_at else ''}")
    try:
        cached = cache.get(key)
    except Exception:
        cached = None
    if isinstance(cached, dict):
        return cached
    facts = {"operation": None, "setupPullRequest": None, "canGenerateArticle": False}
    try:
        from founder_tools.services import FounderCompanyContext
        from .vibe_marketing_views import _article_capabilities_for_context
        from .website_journey import journey_for_context
        context = FounderCompanyContext(profile=company.profile, company=company, organization=organization)
        capabilities = _article_capabilities_for_context(context, config, allow_probe=False)
        journey = journey_for_context(context, config, capabilities=capabilities, owner_view=True)
        operation = _mapping(journey.get("operation"))
        receipt = _mapping(operation.get("receipt"))
        user_action = _mapping(receipt.get("user_action"))
        pull_request = _mapping(journey.get("setupPullRequest"))
        facts = {
            "operation": {"state": operation.get("state"), "failureClass": receipt.get("failure_class"),
                          "actionId": user_action.get("id"), "url": _https_or_none(user_action.get("url"))}
            if operation else None,
            "setupPullRequest": {"runId": pull_request.get("runId"), "prUrl": _https_or_none(pull_request.get("prUrl"))}
            if pull_request else None,
            "canGenerateArticle": bool(_mapping(journey.get("capabilities")).get("canGenerateArticle")),
        }
    except Exception:
        logger.warning("founder_journey_website_facts_unavailable org=%s", organization.pk, exc_info=True)
    try:
        cache.set(key, facts, WEBSITE_CACHE_SECONDS)
    except Exception:
        pass
    return facts


def publish_automatically(*, organization, marker, connection, setup_run) -> str:
    """``unavailable``, ``available``, ``in_progress`` or ``on`` (IF-P1-03)."""
    if not cohorts.organization_has_website(organization):
        return "unavailable"
    if marker.get("publishAutomatically") and cohorts.has_connected_website(organization):
        return "on"
    if connection is None and setup_run is None:
        return "available"
    return "in_progress"


def _github_return(marker, now):
    value = _mapping(marker.get("githubReturn"))
    reason, at = value.get("reason"), parse_datetime(str(value.get("at") or ""))
    if not reason or at is None or timezone.is_naive(at) or now - at > GITHUB_RETURN_WINDOW or at > now + timedelta(minutes=1):
        return None
    return {"reason": str(reason), "at": str(value.get("at"))}


# ---------------------------------------------------------------------------
# Shape helpers


def _copy(case, **values):
    headline, body, label = COPY[case]
    fill = (lambda text: text.format(**values) if text else text)
    return fill(headline), fill(body), fill(label)


def _with_email_clause(case, body, notify):
    if notify and case in EMAIL_CLAUSE_CASES and body:
        return f"{body} {EMAIL_CLAUSE}"
    return body


def _action(ask, label, url=None):
    if ask not in FOUNDER_ASKS:
        raise ValueError(f"Unknown founder ask: {ask}")
    return {"id": ask, "label": label, "url": url}


def _next(kind, label, *, run_id=None, url=None):
    if kind not in NEXT_KINDS:
        raise ValueError(f"Unknown next kind: {kind}")
    return {"kind": kind, "label": label, "runId": run_id, "url": url}


def _decide(stage, state, case, *, action=None, next_kind=None, run_id=None, holding=None, url=None,
            notify=False, support_code=None, **values):
    headline, body, label = _copy(case, **values)
    body = _with_email_clause(case, body, notify)
    decision = dict(stage=stage, state=state, headline=headline, body=body, holdingReason=holding,
                         action=_action(action, label, url) if action else None,
                         next=_next(next_kind, label, run_id=run_id, url=url if next_kind == "view_article" else None)
                         if next_kind and label else None,
                         runId=run_id, supportCode=support_code)
    return decision


# ---------------------------------------------------------------------------
# The projection


def _audience_known(organization, config, facts) -> bool:
    from .content_first_article import audience_for
    return audience_for(organization, config, facts) is not None


def project_founder_journey(organization, *, user, company, now=None) -> dict:
    """The IF-P1-03 journey body for this company, with its ``etag``.

    A legacy organisation (``is_new_journey`` false) returns the legacy body
    before any read beyond the cohort marker.
    """
    company_id = str(getattr(company, "pk", "") or "")
    if organization is None or not cohorts.is_new_journey(organization):
        return legacy_payload(company_id)
    config = cohorts._config_for(organization) or cohorts._EmptyConfig
    now = now or timezone.now()
    from .activation import first_article_capability
    from .first_article_entitlement import entitled_request, entitlement_state
    marker = cohorts.read_marker(organization, config) or {}
    entitlement = entitlement_state(organization, user, config=config, now=now)
    first_article = first_article_capability(entitlement)
    notify = draft_ready_notifications(organization, user)
    has_website = cohorts.organization_has_website(organization)
    connection = _connection(organization)
    setup_run = _setup_run(organization)
    site = _site_profile(config)
    run = latest_article_run(organization)
    decision = None
    facts = {"etaSeconds": None, "queuePosition": None, "switchTopicAvailable": False}

    if terms_out_of_date(user):
        decision = _decide("first_article", "your_turn", "agree_terms", action="agree_terms")

    if decision is None and run is not None and failure_class(run) == "needs_user" \
            and run.status not in DELIVERED_STATUSES | ACTIVE_STATUSES:
        registry_action = registry_action_id(run)
        ask = founder_ask(registry_action)
        if ask in SETUP_ASKS:
            ask = None if ask == "approve_on_github" and not _pull_request_url(run) else ask
        if ask == "review_draft":
            decision = _decide("review", "your_turn", "review", action="review_draft", run_id=run.run_id)
        elif ask == "buy_points":
            price, balance = article_price_and_balance(organization, user)
            decision = _decide("writing", "your_turn", "buy_points", action="buy_points", run_id=run.run_id,
                               price=price, balance=balance, shortfall=max(1, price - balance))
        elif ask is not None:
            decision = _decide("writing", "your_turn", ask, action=ask, run_id=run.run_id,
                               url=_pull_request_url(run) if ask == "approve_on_github" else None)
        else:
            if registry_action in RETIRED_ACTIONS:
                logger.info("founder_journey_retired_action org=%s action=%s", organization.pk, registry_action)
            decision = _decide("writing", "working", "still_working", next_kind="view_progress", run_id=run.run_id,
                               holding="slow")

    if decision is None and run is not None and (run.status in ACTIVE_STATUSES or is_retrying(run)):
        writing = writing_facts(run, now=now)
        free_run = entitled_request(_request(run)) and not _request(run).get("original_billing_source_run_id")
        facts["etaSeconds"], facts["queuePosition"] = writing["etaSeconds"], writing["queuePosition"]
        facts["switchTopicAvailable"] = bool(
            writing["slow"] and free_run and first_article["attemptsUsed"] < first_article["attemptsAllowed"]
            and getattr(settings, "ARTICLES_FIRST_ARTICLE_CARD_ENABLED", False))
        if writing["retrying"]:
            case = "writing_retrying"
        elif writing["slow"]:
            case = "writing_slow"
        elif writing["queuePosition"]:
            case = "writing_queued" if writing["etaSeconds"] is not None else "writing_queued_no_eta"
        else:
            case = "writing_running" if writing["etaSeconds"] is not None else "writing_running_no_eta"
        decision = _decide("writing", "working", case, next_kind="view_progress", run_id=run.run_id,
                           holding="slow" if writing["slow"] else None, notify=notify,
                           ordinal=ordinal(writing["queuePosition"] or 1),
                           minutes=minutes(writing["etaSeconds"] or 0))

    if decision is None and run is not None and (
            (run.status in FAILED_STATUSES and not is_retrying(run))
            or (run.status == "cancelled" and not is_owner_cancel(run))):
        price, _balance = article_price_and_balance(organization, user)
        support = _support_code(run)
        if entitled_request(_request(run)) and first_article["status"] == "available":
            decision = _decide("first_article", None, "failed_free", next_kind="write_first_article",
                               support_code=support)
        else:
            case = {"refunded": "failed_refunded", "refund_pending": "failed_refund_pending"}.get(
                "none" if entitled_request(_request(run)) else _refund_case(run), "failed")
            decision = _decide("first_article", None, case, next_kind="write_article", support_code=support,
                               price=price)

    if decision is None and run is not None and run.status in DELIVERED_STATUSES:
        mode = delivery_mode(run)
        approved = str(getattr(run, "approval_state", "") or "") == "approved"
        if mode == "content_only":
            # A copied or posted draft is done (rule 9), after setup and publishing.
            if not copied_or_posted(config, run.run_id):
                decision = _decide("review", "your_turn", "review", action="review_draft", run_id=run.run_id)
        elif not approved:
            decision = _decide("review", "your_turn", "review", action="review_draft", run_id=run.run_id)

    if decision is None:
        decision = _setup_decision(organization=organization, company=company, config=config, user=user,
                                   marker=marker, connection=connection, setup_run=setup_run, run=run)

    if decision is None:
        decision = _publishing_or_done(organization=organization, config=config, marker=marker, run=run,
                                       connection=connection)

    if decision is None and not has_website and not _one_liner(organization):
        decision = _decide("website", None, "website", next_kind="add_website")

    if decision is None:
        decision = _first_article_decision(organization=organization, company=company, config=config, user=user,
                                           first_article=first_article)

    payload = {
        "version": VERSION, "companyId": company_id, "cohort": cohorts.NEW_JOURNEY,
        "stage": decision["stage"], "state": decision["state"], "headline": decision["headline"],
        "body": decision["body"], "holdingReason": decision["holdingReason"], "action": decision["action"],
        "next": decision["next"], "runId": decision["runId"], "etaSeconds": facts["etaSeconds"],
        "queuePosition": facts["queuePosition"], "draftReadyNotifications": notify,
        "switchTopicAvailable": facts["switchTopicAvailable"], "githubReturn": _github_return(marker, now),
        "website": {
            "hasWebsite": has_website,
            "domain": organization.domain if has_website else None,
            "platform": str(site.get("platform") or "") or None,
            "blogUrl": _https_or_none(site.get("blogUrl")),
            "publishAutomatically": publish_automatically(organization=organization, marker=marker,
                                                          connection=connection, setup_run=setup_run),
        },
        "firstArticle": first_article,
        "pollAfterMs": _poll_after(decision),
        "supportCode": decision["supportCode"],
    }
    action_id = (payload["action"] or {}).get("id")
    logger.info("founder_journey_projection org=%s stage=%s state=%s action=%s", organization.pk,
                payload["stage"], payload["state"], action_id or "")
    return _with_etag(payload)


def _poll_after(decision) -> int:
    if decision["stage"] == "publishing":
        return POLL_AFTER_MS["publishing"]
    return POLL_AFTER_MS.get(decision["state"], POLL_AFTER_MS[None])


def _pull_request_url(run):
    result = _result(run)
    for value in (result.get("pr_html_url"), result.get("pr_url"), _mapping(result.get("article_system_setup")).get("pr_url")):
        url = _https_or_none(value)
        if url and url.startswith("https://github.com/"):
            return url
    return None


def _one_liner(organization) -> str:
    from startup_updates.models import StartupProfile
    profile = StartupProfile.objects.filter(organization_id=organization.pk).only("short_description").first()
    return str(getattr(profile, "short_description", "") or "").strip()


def _setup_decision(*, organization, company, config, user, marker, connection, setup_run, run):
    """Rule 7: a website setup that asks the owner something."""
    if connection is None and setup_run is None:
        return None
    approval = _mapping(marker.get("setupApproval"))
    owner_click = _requires_owner_click(organization)
    facts = (_website_journey_facts(organization=organization, company=company, config=config, user=user,
                                    connection=connection, latest_run_at=_latest_run_updated_at(organization))
             if connection is not None else {"operation": None, "setupPullRequest": None})
    if (owner_click and setup_run is not None and setup_run.status in SETUP_PREVIEW_READY_STATUSES
            and approval.get("runId") != setup_run.run_id):
        return _decide("setup", "your_turn", "approve_setup", action="approve_setup", run_id=setup_run.run_id)
    if approval.get("merge") == "manual":
        pull_request = _mapping(facts.get("setupPullRequest"))
        approved_run = _setup_run(organization, approval.get("runId")) if approval.get("runId") else None
        url = pull_request.get("prUrl") if pull_request.get("runId") == approval.get("runId") else None
        url = url or (_pull_request_url(approved_run) if approved_run is not None
                      and approved_run.status not in {"completed"} | SETUP_STOPPED_STATUSES else None)
        if url:
            return _decide("setup", "your_turn", "approve_on_github", action="approve_on_github",
                           run_id=approval.get("runId"), url=url)
    if setup_run is not None and failure_class(setup_run) == "needs_user":
        ask = founder_ask(registry_action_id(setup_run))
        if ask in SETUP_ASKS:
            url = _pull_request_url(setup_run) if ask == "approve_on_github" else None
            if ask != "approve_on_github" or url:
                return _decide("setup", "your_turn", ask, action=ask, run_id=setup_run.run_id, url=url)
    operation = _mapping(facts.get("operation"))
    if operation.get("state") == "needs_user" and operation.get("failureClass") in (None, "", "needs_user"):
        ask = founder_ask(operation.get("actionId"))
        if ask in SETUP_ASKS:
            url = operation.get("url") or _mapping(facts.get("setupPullRequest")).get("prUrl")
            if ask != "approve_on_github" or url:
                return _decide("setup", "your_turn", ask, action=ask, url=url if ask == "approve_on_github" else None)
        elif operation.get("actionId") in RETIRED_ACTIONS:
            logger.info("founder_journey_retired_action org=%s action=%s", organization.pk, operation.get("actionId"))
    return None


def _latest_run_updated_at(organization):
    from workflow_runs.models import ContentFactoryRun
    return (ContentFactoryRun.objects.filter(organization_id=organization.pk).order_by("-updated_at")
            .values_list("updated_at", flat=True).first())


def _live_article(organization, run=None):
    from .models import ArticlePublishStatus, WrittenArticle
    articles = WrittenArticle.objects.filter(organization_id=organization.pk, publish_status=ArticlePublishStatus.LIVE,
                                             live_url__isnull=False).exclude(live_url="")
    if run is not None:
        articles = articles.filter(source_run_id=run.run_id)
    return articles.order_by("-live_verified_at", "-published_at").first()


def _publishing_or_done(*, organization, config, marker, run, connection):
    """Rules 8 and 9: publishing while the site updates, then done."""
    if run is not None and run.status in DELIVERED_STATUSES and str(getattr(run, "approval_state", "") or "") == "approved" \
            and delivery_mode(run) != "content_only":
        live = _live_article(organization, run)
        if live is None:
            return _decide("publishing", "working", "publishing", holding="website_update", run_id=run.run_id)
        return _decide("done", "done", "done_live", next_kind="view_article", run_id=run.run_id,
                       url=_https_or_none(live.live_url))
    approval = _mapping(marker.get("setupApproval"))
    if approval.get("merge") == "auto" and approval.get("runId") and not cohorts.has_connected_website(organization):
        approved_run = _setup_run(organization, approval.get("runId"))
        if approved_run is None or approved_run.status not in SETUP_STOPPED_STATUSES:
            return _decide("publishing", "working", "publishing", holding="website_update")
    if run is not None and run.status in DELIVERED_STATUSES:
        live = _live_article(organization, run)
        if live is not None and _https_or_none(live.live_url):
            return _decide("done", "done", "done_live", next_kind="view_article", run_id=run.run_id,
                           url=_https_or_none(live.live_url))
        if delivery_mode(run) == "content_only" and copied_or_posted(config, run.run_id):
            return _decide("done", "done", "done_copy_ready", next_kind="view_draft", run_id=run.run_id)
    if run is None:
        live = _live_article(organization)
        if live is not None and _https_or_none(live.live_url):
            return _decide("done", "done", "done_live", next_kind="view_article", url=_https_or_none(live.live_url))
    return None


def _first_article_decision(*, organization, company, config, user, first_article):
    """Rules 11 and 12: the first-article stage."""
    from .content_first_article import first_topic, profile_facts
    facts = profile_facts(company, organization, config)
    if not _audience_known(organization, config, facts):
        return _decide("first_article", "your_turn", "describe_customer", action="describe_customer")
    if first_article.get("limit") == "daily_cap":
        return _decide("first_article", None, "daily_cap", holding="busy_today")
    if first_article.get("free"):
        topic = first_topic(organization=organization, company=company, config=config, facts=facts)
        if topic:
            return _decide("first_article", None, "first_free_topic", next_kind="write_first_article",
                           topic=topic["title"])
        return _decide("first_article", None, "first_free", next_kind="write_first_article")
    price, balance = article_price_and_balance(organization, user)
    if balance >= price:
        return _decide("first_article", None, "first_paid", next_kind="write_article", price=price)
    return _decide("first_article", "your_turn", "buy_points", action="buy_points", price=price, balance=balance,
                   shortfall=max(1, price - balance))
