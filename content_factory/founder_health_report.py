"""The daily founder-health report and its SLO lines (plan F9, decisions D12 and D8).

One read-only report for ops Slack and the staff console:

- funnel conversion by stage, in the approved onboarding order (E10), from
  ``founder_funnel.journey_funnel``;
- time to first article (signup to first draft, p50 and p90), from the same;
- setup success and the one-click article outcomes, from
  ``one_click_metrics.summarize_one_click``;
- publish success, generic stops shown, top codes by founders affected;
- the count and age of ``staff_fixing`` items by stack (B25);
- the stuck-over-24-hours list by startup name;
- duplicate organisations (E5), placeholder and hosting-builder domains;
- the D12 SLOs as pass/fail lines.

Marked test organisations (plan F1, ``content_factory.founder_test_orgs``) are
left out of every line.

Scheduler safety (the 10 Nov 2026 OOM): every query has a row limit and reads
scalar columns or single JSON keys, never a whole ``result``, ``receipt`` or
strategy document. The one exception is the funnel: ``journey_funnel`` reads
the cohort's run documents, so it runs only after a scalar count shows at most
``FUNNEL_RUN_LIMIT`` such runs, and is skipped (with a line saying so) above
that. A section that hit its limit says so.

Delivery: ``manage.py founder_health_report`` prints it (read-only, always
allowed), ``GET community-chat/admin/startups/health/`` returns it to the staff
console, and :func:`run_founder_health_report_sweep` posts it to the ops Slack
channel once a Melbourne day, only while ``ARTICLES_FOUNDER_HEALTH_REPORT_ENABLED``
is on (default off).
"""
from __future__ import annotations

from collections import defaultdict
from datetime import time, timedelta
import logging
import math
import re
from zoneinfo import ZoneInfo

from django.conf import settings
from django.core.cache import cache
from django.db import DEFAULT_DB_ALIAS
from django.utils import timezone

logger = logging.getLogger(__name__)

REPORT_VERSION = 1
LOCAL = ZoneInfo("Australia/Melbourne")
# Rows read per section. A section that reaches its limit is reported as truncated.
ROW_LIMIT = 2000
LIST_LIMIT = 20
TOP_CODES = 10
# The funnel's signup cohort and the activity windows.
FUNNEL_WINDOW = timedelta(days=90)
# Article and research runs the funnel may read whole (journey_funnel reads each run's result).
FUNNEL_RUN_LIMIT = 500
ACTIVITY_WINDOW = timedelta(days=7)
GENERIC_WINDOW = timedelta(hours=24)
SETUP_COHORT_WINDOW = timedelta(days=30)
STUCK_AFTER = timedelta(hours=24)
STUCK_LOOKBACK = timedelta(days=90)
# D12 targets.
SETUP_READY_WITHIN = timedelta(hours=24)
SETUP_READY_TARGET = 0.90
FIRST_DRAFT_WITHIN = timedelta(hours=2)
FIRST_DRAFT_TARGET = 0.95
STOP_STATUSES = ("failed", "blocked", "stopped", "denied", "precondition_failed", "preview_failed")
RUN_CODE_FIELDS = ("result__error_code", "result__failure_code", "result__failure__code",
                   "result__authority_denied__code")
OPERATION_CODE_FIELDS = ("receipt__reason_code", "receipt__error_code", "receipt__failure__code", "receipt__code")
SAFE_CODE = re.compile(r"[A-Za-z][A-Za-z0-9_.\-]{0,79}")
SLACK_CHANNEL = re.compile(r"[CGD][A-Z0-9]{8,99}")
DAILY_KEY = "founder_health_report:posted:{day}"
DAILY_TTL = 3 * 86400
DAILY_ATTEMPTS_KEY = "founder_health_report:attempts:{day}"
MAX_DAILY_ATTEMPTS = 3


def enabled() -> bool:
    """Whether the scheduler posts the report (``ARTICLES_FOUNDER_HEALTH_REPORT_ENABLED``, default off)."""
    return bool(getattr(settings, "ARTICLES_FOUNDER_HEALTH_REPORT_ENABLED", False))


def _code(row, fields):
    for key in fields:
        value = row.get(key)
        if isinstance(value, str) and SAFE_CODE.fullmatch(value.strip()):
            return value.strip()
    return None


def _percentile(values, fraction):
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * fraction) - 1)] if ordered else None


def _share(numerator, denominator):
    return round(numerator / denominator, 4) if denominator else None


def _bounded(queryset, limit):
    rows = list(queryset[:limit + 1])
    return rows[:limit], len(rows) > limit


def _hours(seconds):
    return None if seconds is None else round(seconds / 3600, 1)


# ---------------------------------------------------------------------------
# Sections


def _funnel_runs(*, since, until, using, test_ids):
    """Scalar counts of what ``journey_funnel`` would load whole for this cohort.

    The same selection journey_funnel makes: the startups founded by non-staff
    accounts that signed up in [since, until), and every article or research
    run of those startups, whenever it was created (journey_funnel reads them
    all, so a created_at window would not bound it). Two COUNT queries.
    """
    from django.contrib.auth import get_user_model
    from founder_tools.models import VibeRaisingCompany
    from workflow_runs.models import ContentFactoryRun
    from .founder_funnel import RESEARCH_WORKFLOWS
    from .run_state import ARTICLE_WORKFLOWS
    users = get_user_model().objects.using(using).filter(date_joined__gte=since, date_joined__lt=until,
                                                         is_staff=False).values("pk")
    organizations = (VibeRaisingCompany.objects.using(using)
                     .filter(profile__user_id__in=users, organization__isnull=False)
                     .exclude(organization_id__in=sorted(test_ids)).values("organization_id"))
    runs = (ContentFactoryRun.objects.using(using)
            .filter(organization_id__in=organizations, workflow__in=tuple(ARTICLE_WORKFLOWS) + RESEARCH_WORKFLOWS)
            .count())
    return runs, organizations.order_by().values("organization_id").distinct().count()


def _skipped_funnel(*, since, runs):
    return {"since": since.isoformat(), "organizations": 0, "stages": [], "notConnected": 0, "skipped": True,
            "runs": runs, "timeToFirstArticle": {"p50Seconds": None, "p90Seconds": None, "sampleSize": 0,
                                                 "unknownTimingUsers": 0}}


def _funnel(*, now, using, test_ids=frozenset(), run_limit=FUNNEL_RUN_LIMIT):
    from .founder_funnel import APPROVED_STAGES, journey_funnel
    since = now - FUNNEL_WINDOW
    # journey_funnel reads each cohort run's result document. The scheduler ran
    # out of memory on 10 Nov loading 2,047 of those, so the funnel is skipped
    # (and says so) rather than read when the cohort has more than run_limit runs.
    # It also reads each cohort startup's whole strategy document, so the number
    # of startups is bounded the same way.
    runs, organizations = _funnel_runs(since=since, until=now, using=using, test_ids=test_ids)
    if runs > run_limit or organizations > run_limit:
        logger.warning("founder_health_report_funnel_skipped runs=%s organizations=%s limit=%s",
                       runs, organizations, run_limit)
        return _skipped_funnel(since=since, runs=runs)
    try:
        report = journey_funnel(since=since, until=now, using=using)
    except ValueError:
        # More sign-ups than journey_funnel's cohort ceiling: skip, never fail the report.
        logger.warning("founder_health_report_funnel_skipped reason=cohort_too_large")
        return _skipped_funnel(since=since, runs=runs)
    approved = report["approved"]
    stopped = approved.get("stuckAt") or {}
    stages, previous = [], None
    for stage in APPROVED_STAGES:
        reached = approved["stages"][stage]["organizations"]
        # Stage facts are derived from current rows (E10), so a later stage can be
        # known where an earlier one is not; "stopped here" is where each startup is now.
        entry = {"stage": stage, "organizations": reached, "stoppedHere": int(stopped.get(stage) or 0)}
        if stage == "exited_not_on_github":
            stages.append({**entry, "first": False, "conversion": None, "sideExit": True})
            continue
        stages.append({**entry, "first": previous is None,
                       "conversion": _share(reached, previous) if previous is not None else None, "sideExit": False})
        previous = reached
    timing = report["timeToFirstDraft"]
    return {"since": report["since"], "organizations": approved["organizations"], "stages": stages,
            "notConnected": approved["notConnected"]["organizations"], "timeToFirstArticle": timing,
            "skipped": False, "runs": runs}


def _article_rows(*, now, using, limit, test_ids):
    """One-click article rows in summarize_one_click's shape, from scalar keys only."""
    from workflow_runs.models import ContentFactoryRun
    from .founder_journey_alerts import founder_run_q
    fields = ("pk", "run_id", "organization_id", "workflow", "status", "created_at", "updated_at",
              "run_request__roo_points_ledger_id", "run_request__original_billing_source_run_id",
              "run_request__restart_source_run_id", "run_request__source_run_id",
              "result__user_action_count", "result__content_acceptance_calls",
              "result__reconciliation_recovery", "result__reconciliation__outcome",
              "result__draft_ready_v1__version", "result__draft_ready_v1__at")
    query = (ContentFactoryRun.objects.using(using).filter(
        founder_run_q(), created_at__gte=now - ACTIVITY_WINDOW, organization__isnull=False,
        workflow__in=["article_generation", "direct_generate", "confirmed_topic", "article_revision"])
        .exclude(organization_id__in=sorted(test_ids))
        .order_by("-created_at").values(*fields))
    rows, truncated = _bounded(query, limit)
    shaped = []
    for row in rows:
        request = {key: row[f"run_request__{key}"] for key in (
            "roo_points_ledger_id", "original_billing_source_run_id", "restart_source_run_id", "source_run_id")
            if row.get(f"run_request__{key}") is not None}
        result = {"user_action_count": row["result__user_action_count"],
                  "reconciliation_recovery": row["result__reconciliation_recovery"],
                  "reconciliation": {"outcome": row["result__reconciliation__outcome"]}}
        if row.get("result__content_acceptance_calls") is not None:
            result["content_acceptance_calls"] = row["result__content_acceptance_calls"]
        ready_at = None
        if type(row.get("result__draft_ready_v1__version")) is int and row["result__draft_ready_v1__version"] == 1:
            from .founder_funnel import timestamp
            ready_at = timestamp(row.get("result__draft_ready_v1__at"))
        shaped.append({"pk": row["pk"], "draft_ready_at": ready_at, "run_id": row["run_id"], "organization_id": row["organization_id"], "workflow": row["workflow"],
                       "status": row["status"], "created_at": row["created_at"], "updated_at": row["updated_at"],
                       "run_request": request, "result": result})
    return shaped, truncated


def _prepare_rows(*, now, using, limit, test_ids):
    from .website_models import WebsiteConnectionOperation
    query = (WebsiteConnectionOperation.objects.using(using).filter(action="prepare", created_at__gte=now - ACTIVITY_WINDOW)
             .exclude(connection__organization_id__in=sorted(test_ids))
             .order_by("-created_at").values("state", "receipt__status", "payload__user_clicks", "created_at", "updated_at"))
    rows, truncated = _bounded(query, limit)
    return [{"state": row["state"], "receipt": {"status": row["receipt__status"]},
             "payload": {"user_clicks": row["payload__user_clicks"]}} for row in rows], truncated


def _one_click(*, now, using, limit, test_ids):
    from .one_click_metrics import REVIEW_STATUSES, summarize_one_click
    articles, articles_truncated = _article_rows(now=now, using=using, limit=limit, test_ids=test_ids)
    prepares, prepares_truncated = _prepare_rows(now=now, using=using, limit=limit, test_ids=test_ids)
    summary = summarize_one_click(article_rows=articles, prepare_rows=prepares)
    stopped = sum(row["state"] in {"needs_attention", "failed"} for row in prepares)
    # D12: first draft within 2 hours of the request, over requests that reached
    # review. The draft clock is the writer's draft_ready_v1 receipt, else the
    # completed packaging step; a run's mutable updated_at is never used, and a
    # request with neither clock is reported as unknown, not as a pass.
    reviewed = [row for row in articles if row["status"] in REVIEW_STATUSES and row["created_at"]]
    missing = [row["pk"] for row in reviewed if row["draft_ready_at"] is None]
    packaged = {}
    if missing:
        from workflow_runs.models import ContentFactoryRunStep
        packaged = dict(ContentFactoryRunStep.objects.using(using).filter(
            run_id__in=missing, step_key="package_content_delivery", status="completed",
            completed_at__isnull=False).values_list("run_id", "completed_at")[:len(missing)])
    timed = [(row["draft_ready_at"] or packaged.get(row["pk"])) - row["created_at"] for row in reviewed
             if (row["draft_ready_at"] or packaged.get(row["pk"])) is not None]
    within = sum(delta <= FIRST_DRAFT_WITHIN for delta in timed)
    return {"summary": summary, "prepareStopped": stopped, "truncated": articles_truncated or prepares_truncated,
            "draftDeliveryEvidence": _draft_delivery_evidence(
                articles, packaged, now=now, truncated=articles_truncated),
            "firstDraftWithin2h": {"within": within, "reviewed": len(timed), "unknown": len(reviewed) - len(timed),
                                   "share": _share(within, len(timed))}}


def _draft_delivery_evidence(articles, packaged, *, now, truncated):
    """Count requests with a complete observation window, including undelivered drafts."""
    from .one_click_metrics import REVIEW_STATUSES, article_request_groups

    counts = {"eligible": 0, "within": 0, "late": 0, "undelivered": 0, "unknown": 0}
    for group in article_request_groups(articles):
        starts = [row["created_at"] for row in group
                  if row["created_at"] and row["workflow"] != "article_revision"]
        if not starts:
            continue
        started = min(starts)
        if now - started < FIRST_DRAFT_WITHIN:
            continue
        counts["eligible"] += 1
        ready = [row["draft_ready_at"] or packaged.get(row["pk"]) for row in group]
        dates = [at for at in ready if at and started <= at <= now]
        if dates:
            bucket = "within" if min(dates) - started <= FIRST_DRAFT_WITHIN else "late"
        else:
            bucket = "unknown" if any(row["status"] in REVIEW_STATUSES for row in group) else "undelivered"
        counts[bucket] += 1
    share = _share(counts["within"], counts["eligible"])
    status = "inconclusive" if truncated or not counts["eligible"] or counts["unknown"] else (
        "passing" if share >= FIRST_DRAFT_TARGET else "below_target")
    return {**counts, "share": share, "status": status}


def _setup_ready(*, now, using, limit, test_ids):
    """D12: connected founders whose setup was Ready within 24 hours of connecting.

    The cohort is connections made between 30 days and 24 hours ago, so each
    has had its full 24 hours. Ready is the first verified articles target.
    """
    from django.db.models import Min
    from .website_models import WebsiteConnection
    query = (WebsiteConnection.objects.using(using).exclude(github_repo="")
             .filter(created_at__gte=now - SETUP_COHORT_WINDOW, created_at__lte=now - SETUP_READY_WITHIN)
             .exclude(organization_id__in=sorted(test_ids))
             .annotate(first_verified=Min("targets__verified_at"))
             .order_by("-created_at").values_list("organization_id", "created_at", "first_verified"))
    rows, truncated = _bounded(query, limit)
    first = {}
    for organization_id, created_at, verified in rows:
        # One founder, one connection: the first one made in the window.
        if organization_id not in first or created_at < first[organization_id][0]:
            first[organization_id] = (created_at, verified)
    ready = sum(verified is not None and verified - created <= SETUP_READY_WITHIN for created, verified in first.values())
    return {"ready": ready, "connected": len(first), "share": _share(ready, len(first)), "truncated": truncated}


def _publish(*, now, using, limit, test_ids):
    from django.db.models import Q
    from .models import ArticlePublishStatus, WrittenArticle
    since = now - ACTIVITY_WINDOW
    # Articles written, merged or seen live in the window (WrittenArticle has no update clock).
    query = (WrittenArticle.objects.using(using).filter(
        Q(created_at__gte=since) | Q(pr_merged_at__gte=since) | Q(live_verified_at__gte=since))
             .exclude(publish_status=ArticlePublishStatus.WRITTEN)
             .exclude(organization_id__in=sorted(test_ids))
             .order_by("-created_at", "pk").values_list("publish_status", flat=True))
    rows, truncated = _bounded(query, limit)
    published = sum(value in {ArticlePublishStatus.MERGED, ArticlePublishStatus.LIVE} for value in rows)
    live = sum(value == ArticlePublishStatus.LIVE for value in rows)
    return {"published": published, "live": live, "attempted": len(rows), "share": _share(published, len(rows)),
            "truncated": truncated}


def _stops(*, now, using, limit, test_ids):
    """Founder-run and setup stops in the window: generic stops and the top codes by founders affected."""
    from workflow_runs.models import ContentFactoryRun
    from .founder_journey_alerts import founder_run_q
    from .no_generic_stop import GENERIC_CODES
    from .website_models import WebsiteConnectionOperation
    runs, runs_truncated = _bounded(
        ContentFactoryRun.objects.using(using).filter(founder_run_q(), status__in=STOP_STATUSES,
                                                      organization__isnull=False,
                                                      updated_at__gte=now - ACTIVITY_WINDOW)
        .exclude(organization_id__in=sorted(test_ids))
        .order_by("-updated_at").values("organization_id", "updated_at", *RUN_CODE_FIELDS), limit)
    operations, operations_truncated = _bounded(
        WebsiteConnectionOperation.objects.using(using).filter(action="prepare", state__in=("needs_attention", "failed"),
                                                               updated_at__gte=now - ACTIVITY_WINDOW)
        .exclude(connection__organization_id__in=sorted(test_ids))
        .order_by("-updated_at").values("connection__organization_id", "updated_at", *OPERATION_CODE_FIELDS), limit)
    founders = defaultdict(set)
    generic_orgs, generic_count = set(), 0
    rows = ([(row["organization_id"], row["updated_at"], _code(row, RUN_CODE_FIELDS)) for row in runs]
            + [(row["connection__organization_id"], row["updated_at"], _code(row, OPERATION_CODE_FIELDS))
               for row in operations])
    for organization_id, updated_at, code in rows:
        code = code or "TERMINAL_CODE_MISSING"
        founders[code].add(organization_id)
        if code in GENERIC_CODES and updated_at >= now - GENERIC_WINDOW:
            generic_count += 1
            generic_orgs.add(organization_id)
    top = sorted(((code, len(orgs)) for code, orgs in founders.items()), key=lambda item: (-item[1], item[0]))
    return {"generic": {"stops": generic_count, "founders": len(generic_orgs)},
            "topCodes": [{"stopCode": code, "founders": count} for code, count in top[:TOP_CODES]],
            "truncated": runs_truncated or operations_truncated}


def _staff_fixing(*, now, limit, test_ids):
    from .website_staff_fixing import report
    return report(now=now, limit=limit, excluded_organization_ids=test_ids)


def _stuck(*, now, using, limit, test_ids):
    """Founders stuck over 24 hours, by startup name, and whether staff own each one.

    Reads the stuck alert's newest page only (it never moves the alert sweep's
    cursor). A stuck founder is owned when staff acknowledged its stuck alert or
    named an owner for its staff fix.
    """
    from organizations.models import Organization
    from . import founder_journey_alerts as alerts
    from .founder_stuck_alerts import STUCK_REASON, Scope, _find, stuck_alert_id
    from .website_models import WebsiteConnectionOperation
    from .website_staff_fixing import KEY, STATUS
    full = []
    scope = Scope(run_stuck_after=STUCK_AFTER, lookback=STUCK_LOOKBACK, limit=limit, cursor=False,
                  excluded_organization_ids=test_ids, full_pages=full)
    conditions = [condition for condition in _find(now=now, scope=scope)
                  if condition.stuck_since <= now - STUCK_AFTER]
    # A full page means older stuck founders may be past it: the section says it
    # was truncated, so a zero count is never read as an all-clear.
    page_full = bool(full)
    if not conditions:
        return {"founders": [], "withoutOwner": 0, "total": 0, "truncated": page_full}
    organization_ids = sorted({condition.organization_id for condition in conditions})
    receipts = dict(alerts._receipt_index(organization_ids))
    staff_owned = set(WebsiteConnectionOperation.objects.using(using).filter(
        action="prepare", receipt__status=STATUS, connection__organization_id__in=organization_ids,
        **{f"receipt__{KEY}__owner__user_id__isnull": False}).values_list("connection__organization_id", flat=True))
    names = dict(Organization.objects.using(using).filter(pk__in=organization_ids).values_list("pk", "name"))
    founders = {}
    for condition in sorted(conditions, key=lambda item: (item.stuck_since, item.organization_id)):
        receipt = receipts.get(condition.organization_id, {}).get(stuck_alert_id(condition))
        acknowledged = isinstance(receipt, dict) and receipt.get("alert_reason") == STUCK_REASON and bool(
            receipt.get("acknowledged_at"))
        owned = acknowledged or condition.organization_id in staff_owned
        entry = founders.get(condition.organization_id)
        if entry is None:
            founders[condition.organization_id] = {
                "organizationId": condition.organization_id, "name": names.get(condition.organization_id) or "",
                "step": condition.step, "stopCode": condition.code or "none",
                "hours": round((now - condition.stuck_since).total_seconds() / 3600, 1), "owned": owned}
        elif owned:
            entry["owned"] = True
    rows = list(founders.values())
    return {"founders": rows[:LIST_LIMIT], "withoutOwner": sum(not row["owned"] for row in rows), "total": len(rows),
            "truncated": page_full or len(rows) > LIST_LIMIT}


def _startups(*, using, limit, test_ids):
    """Startups with an organisation, newest first, bounded; scalar fields only."""
    from founder_tools.models import VibeRaisingCompany
    query = (VibeRaisingCompany.objects.using(using).filter(organization__isnull=False, closed_at__isnull=True)
             .exclude(organization_id__in=sorted(test_ids))
             .order_by("-created_at", "pk")
             .values("pk", "organization_id", "profile_id", "name", "domain", "created_at",
                     "organization__name", "organization__domain"))
    return _bounded(query, limit)


def _duplicates(startups):
    """E5: two startups in one profile with the same name or site, and staff-only look-alikes across accounts."""
    from founder_tools import website_hosts
    from founder_tools.website_preflight import name_key
    buckets = defaultdict(list)
    for row in startups:
        domain = str(row["domain"] or "")
        site = website_hosts.site_key(domain) if domain and not domain.endswith(".invalid") else ""
        if site:
            buckets[("site", site)].append(row)
        name = name_key(row["name"] or "")
        if len(name) >= 3:
            buckets[("name", name)].append(row)
    own, across = [], []
    for (match, key), members in sorted(buckets.items()):
        organizations = sorted({member["organization_id"] for member in members})
        if len(organizations) < 2:
            continue
        group = {"match": match, "key": key, "organizations": organizations,
                 "names": sorted({member["organization__name"] or member["name"] or "" for member in members})}
        if len({member["profile_id"] for member in members}) < 2:
            own.append(group)
        else:
            across.append(group)
    return {"ownProfile": own[:LIST_LIMIT], "acrossAccounts": across[:LIST_LIMIT],
            "total": len(own) + len(across)}


def _domains(startups):
    """Placeholder (``.invalid``) and hosting-builder or deployment-address domains, per organisation."""
    from .founder_reengagement import classify_host
    placeholder, hosted, seen = [], [], set()
    for row in startups:
        organization_id = row["organization_id"]
        if organization_id in seen:
            continue
        seen.add(organization_id)
        domain = str(row["organization__domain"] or "").strip().lower()
        name = row["organization__name"] or row["name"] or ""
        if not domain or domain.endswith(".invalid"):
            placeholder.append({"organizationId": organization_id, "name": name})
            continue
        host = classify_host(domain)
        if host.deployment_address:
            hosted.append({"organizationId": organization_id, "name": name, "domain": domain,
                           "hostClass": host.host_class, "platform": host.platform or "hosting"})
    return {"placeholder": placeholder[:LIST_LIMIT], "placeholderTotal": len(placeholder),
            "hosted": hosted[:LIST_LIMIT], "hostedTotal": len(hosted)}


# ---------------------------------------------------------------------------
# SLOs (D12)


def _slo(identifier, label, *, passed, value):
    return {"id": identifier, "label": label, "pass": passed, "value": value}


def evaluate_slos(report) -> list[dict]:
    """The D12 SLOs as pass/fail lines. A rate with no sample is reported, and passes (nothing missed it)."""
    setup = report["setupReady"]
    draft = report["oneClick"]["firstDraftWithin2h"]
    stuck = report["stuck"]
    generic = report["stops"]["generic"]
    fixing = report["staffFixing"]

    def rate(share, target, sample):
        return share is None or share >= target, (f"{share:.0%} of {sample}" if share is not None else "no sample")

    setup_pass, setup_value = rate(setup["share"], SETUP_READY_TARGET, setup["connected"])
    draft_pass, draft_value = rate(draft["share"], FIRST_DRAFT_TARGET, draft["reviewed"])
    return [
        _slo("setup_ready_24h", "90% of connected founders have setup Ready within 24 h",
             passed=setup_pass, value=setup_value),
        _slo("first_draft_2h", "95% of first drafts within 2 h of the request", passed=draft_pass, value=draft_value),
        _slo("stuck_owned", "Zero founders stuck over 24 h without a named staff owner",
             passed=stuck["withoutOwner"] == 0, value=str(stuck["withoutOwner"])),
        _slo("no_generic_stops", "Zero generic stops shown (last 24 h)",
             passed=generic["stops"] == 0, value=str(generic["stops"])),
        _slo("staff_fixing_owned", "Every staff fix has a named owner within one working day",
             passed=fixing["ownerOverdue"] == 0, value=f"{fixing['ownerOverdue']} overdue"),
        _slo("staff_fixing_day_7", "Every staff fix ends by day 7",
             passed=fixing["overLifetime"] == 0, value=f"{fixing['overLifetime']} past day 7"),
    ]


# ---------------------------------------------------------------------------
# The report


def build_report(*, now=None, using=DEFAULT_DB_ALIAS, row_limit=ROW_LIMIT) -> dict:
    """The founder-health report as data. Read-only; the caller owns any read-only transaction."""
    from .founder_test_orgs import test_organization_ids
    now = now or timezone.now()
    test_ids = test_organization_ids(using=using)
    startups, startups_truncated = _startups(using=using, limit=row_limit, test_ids=test_ids)
    sections = {"now": now, "using": using, "limit": row_limit, "test_ids": test_ids}
    report = {
        "version": REPORT_VERSION,
        "asOf": now.isoformat(),
        "day": timezone.localtime(now, LOCAL).date().isoformat(),
        "excludedTestOrganizations": len(test_ids),
        "funnel": _funnel(now=now, using=using, test_ids=test_ids, run_limit=min(row_limit, FUNNEL_RUN_LIMIT)),
        "oneClick": _one_click(**sections),
        "setupReady": _setup_ready(**sections),
        "publish": _publish(**sections),
        "stops": _stops(**sections),
        "staffFixing": _staff_fixing(now=now, limit=row_limit, test_ids=test_ids),
        "stuck": _stuck(**{**sections, "limit": min(row_limit, 200)}),
        "duplicates": _duplicates(startups),
        "domains": _domains(startups),
        "startupsTruncated": startups_truncated,
        "rowLimit": row_limit,
    }
    report["slos"] = evaluate_slos(report)
    report["lines"] = render_lines(report)
    return report


def _pct(value):
    return "n/a" if value is None else f"{value:.0%}"


def _duration(seconds):
    if seconds is None:
        return "unknown"
    return f"{round(seconds / 60)} min" if seconds < 3600 else f"{round(seconds / 3600, 1)} h"


def render_lines(report) -> list[str]:
    """The report as plain lines: the Slack text, the command output and the console's view."""
    lines = [f"Founder health {report['day']} (test organisations excluded: {report['excludedTestOrganizations']})"]
    slos = report.get("slos") or evaluate_slos(report)
    lines.append("SLOs")
    for slo in slos:
        lines.append(f"  {'PASS' if slo['pass'] else 'FAIL'} {slo['label']}: {slo['value']}")
    funnel = report["funnel"]
    if funnel.get("skipped"):
        lines.append(f"Funnel skipped: {funnel['runs']} runs for sign-ups since {funnel['since'][:10]} is over the "
                     "row limit; run manage.py founder_funnel_report for it")
    else:
        lines.append(f"Funnel (startups from sign-ups since {funnel['since'][:10]}): {funnel['organizations']} "
                     f"startups, {funnel['notConnected']} not connected")
    for stage in funnel["stages"]:
        conversion = "side exit" if stage["sideExit"] else (
            "start" if stage["first"] else f"{_pct(stage['conversion'])} of previous")
        lines.append(f"  {stage['stage']}: {stage['organizations']} ({conversion}), "
                     f"{stage.get('stoppedHere', 0)} stopped here")
    timing = funnel["timeToFirstArticle"]
    if not funnel.get("skipped"):
        lines.append(f"Time to first article (sign-up to first draft): p50 {_duration(timing['p50Seconds'])}, "
                     f"p90 {_duration(timing['p90Seconds'])}; {timing['sampleSize']} timed, "
                     f"{timing['unknownTimingUsers']} unknown")
    one_click = report["oneClick"]
    summary = one_click["summary"]
    setup = report["setupReady"]
    lines.append(f"Setup success (7 days): {summary['setupsReady']} of {summary['prepareOperations']} preparations "
                 f"Ready, {one_click['prepareStopped']} stopped; Ready within 24 h: {setup['ready']} of "
                 f"{setup['connected']} connected")
    lines.append(f"Articles (7 days): {summary['articlesReachingReview']} of {summary['articleRuns']} requests reached "
                 f"review; first draft within 2 h: {one_click['firstDraftWithin2h']['within']} of "
                 f"{one_click['firstDraftWithin2h']['reviewed']} timed, {one_click['firstDraftWithin2h']['unknown']} unknown")
    if evidence := one_click.get("draftDeliveryEvidence"):
        lines.append(f"Draft delivery qualification: {evidence['status'].replace('_', ' ')}. "
                     f"{evidence['within']} of {evidence['eligible']} within 2 h, {evidence['late']} late, "
                     f"{evidence['undelivered']} without a draft, {evidence['unknown']} with unknown timing.")
    publish = report["publish"]
    lines.append(f"Publish success (7 days): {publish['published']} of {publish['attempted']} merged or live, "
                 f"{publish['live']} live")
    stops = report["stops"]
    lines.append(f"Generic stops shown (24 h): {stops['generic']['stops']} for {stops['generic']['founders']} founders")
    lines.append("Top codes by founders affected (7 days): " + (", ".join(
        f"{row['stopCode']} {row['founders']}" for row in stops["topCodes"]) or "none"))
    fixing = report["staffFixing"]
    lines.append(f"Staff fixing: {fixing['total']} open, {fixing['owned']} owned, {fixing['ownerOverdue']} owner overdue, "
                 f"{fixing['overLifetime']} past day 7")
    for stack, entry in fixing["stacks"].items():
        lines.append(f"  {stack}: {entry['count']}, oldest {entry['oldest_hours']} h")
    stuck = report["stuck"]
    lines.append(f"Stuck over 24 h: {stuck['total']} founders, {stuck['withoutOwner']} without an owner")
    for row in stuck["founders"]:
        lines.append(f"  {row['name']} (org {row['organizationId']}): {row['step']} {row['stopCode']}, "
                     f"{row['hours']} h, {'owned' if row['owned'] else 'no owner'}")
    duplicates = report["duplicates"]
    lines.append(f"Duplicate organisations: {len(duplicates['ownProfile'])} in one profile, "
                 f"{len(duplicates['acrossAccounts'])} across accounts (staff only)")
    for group in (*duplicates["ownProfile"], *duplicates["acrossAccounts"]):
        lines.append(f"  {group['match']} {group['key']}: orgs {', '.join(str(pk) for pk in group['organizations'])}")
    domains = report["domains"]
    lines.append(f"Placeholder domains: {domains['placeholderTotal']}" + (": " + ", ".join(
        f"{row['name']} (org {row['organizationId']})" for row in domains["placeholder"]) if domains["placeholder"] else ""))
    lines.append(f"Hosting-builder domains: {domains['hostedTotal']}" + (": " + ", ".join(
        f"{row['name']} (org {row['organizationId']}, {row['platform']})" for row in domains["hosted"])
        if domains["hosted"] else ""))
    truncated = [name for name, value in (
        ("articles or setups", one_click["truncated"]), ("setup cohort", setup["truncated"]),
        ("publishing", publish["truncated"]), ("stops", stops["truncated"]), ("staff fixing", fixing["truncated"]),
        ("stuck", stuck["truncated"]), ("startups", report["startupsTruncated"])) if value]
    if truncated:
        lines.append(f"Row limit {report['rowLimit']} reached for: {', '.join(truncated)} (first rows only)")
    return lines


def render_text(report) -> str:
    return "\n".join(report.get("lines") or render_lines(report))


# ---------------------------------------------------------------------------
# Delivery


def _post(channel, text, *, client=None):
    """Post one message; returns the Slack ts or "" on failure. Never raises."""
    try:
        if client is None:
            from integrations.services.slack import SlackService
            client = SlackService.get_client()
        response = client.chat_postMessage(channel=channel, text=text, unfurl_links=False, unfurl_media=False)
        ts = str(response.get("ts") or "")
        return ts if response.get("ok") and ts else ""
    except Exception as exc:  # noqa: BLE001 - a failed post is reported, never raised into the scheduler
        logger.warning("founder_health_report_post_failed error_type=%s", type(exc).__name__)
        return ""


def run_founder_health_report_sweep(now=None, limit=None, *, client=None) -> dict:
    """The scheduler entry (a self-heal sweep): post the report once a Melbourne day while the flag is on.

    Off, it does nothing. On, it posts after 08:00 Melbourne, once per day: the
    day key is taken before the post, so a crash never double-posts. A post
    Slack refuses frees the day for a later tick, three tries a day at most (a
    missed day is visible as a missing message, never a page). Without a valid
    ops channel it logs the report and posts nothing.
    """
    if not enabled():
        return {"status": "disabled"}
    now = now or timezone.now()
    local = timezone.localtime(now, LOCAL)
    if local.time() < time(8, 0):
        return {"status": "not_yet"}
    key = DAILY_KEY.format(day=local.date().isoformat())
    if not cache.add(key, now.isoformat(), DAILY_TTL):
        return {"status": "already_posted"}

    def free_the_day():
        # A failed build or post frees the day for a later tick, at most MAX_DAILY_ATTEMPTS times.
        attempts_key = DAILY_ATTEMPTS_KEY.format(day=local.date().isoformat())
        cache.add(attempts_key, 0, DAILY_TTL)
        try:
            attempts = cache.incr(attempts_key)
        except ValueError:
            attempts = MAX_DAILY_ATTEMPTS
        if attempts < MAX_DAILY_ATTEMPTS:
            cache.delete(key)

    try:
        report = build_report(now=now, row_limit=min(int(limit or ROW_LIMIT), ROW_LIMIT))
    except Exception as exc:  # noqa: BLE001 - a failed read is reported, never raised into the scheduler
        logger.warning("founder_health_report_build_failed error_type=%s", type(exc).__name__, exc_info=True)
        free_the_day()
        return {"status": "error"}
    text = render_text(report)
    channel = str(getattr(settings, "CONTENT_FACTORY_OPS_SLACK_CHANNEL_ID", "") or "").strip()
    failing = [slo["id"] for slo in report["slos"] if not slo["pass"]]
    if not SLACK_CHANNEL.fullmatch(channel):
        logger.warning("founder_health_report_unconfigured failing_slos=%s", ",".join(failing) or "none")
        return {"status": "unconfigured", "failingSlos": failing}
    ts = _post(channel, text, client=client)
    if not ts:
        free_the_day()
        return {"status": "undelivered", "failingSlos": failing}
    logger.info("founder_health_report_posted failing_slos=%s", ",".join(failing) or "none")
    return {"status": "posted", "failingSlos": failing}
