"""One always-on "founder stuck" alert for every cohort.

A founder can be stuck in three ways that the cohort-gated alerts miss:

* a website setup step stopped (``needs_attention``, or ``failed`` with no
  automatic retry) on the connection's current generation;
* a website step is still retrying after many failed attempts or a long time
  (La Vie's reconnect sat pending after 21 attempts); a deployment or policy wait
  is left out, because it has its own 24-hour deadline that ends in a stop;
* the founder's latest article or setup run stopped (failed, blocked, stopped or
  denied) more than six hours ago with no founder action and no scheduled
  self-heal retry.

Each condition queues one ``founder_stuck`` receipt through the shared
founder-journey alert store, keyed by the operation or run and the kind of stop,
so a founder gets one alert per condition until it changes. Delivery reuses the
ops Slack channel (``CONTENT_FACTORY_OPS_SLACK_CHANNEL_ID``) and its three-attempt
cap. The Slack text names the founder and the startup, the step, the failure code
and its registry description, how long it has been stuck, and the staff console
link. Names are read at send time and never stored in the receipt.

Every cohort is covered: the sweep does not use the founder-journey launch date
(``ARTICLES_FOUNDER_JOURNEY_ALERTS_SINCE``) and looks back ``LOOKBACK`` (90 days),
so legacy founders who have been stuck longest are alerted too.

Scheduler safety (the 10 Nov 2026 OOM, fixed by #1325): the sweep runs at most
once per ``SWEEP_INTERVAL_SECONDS``, every query has a row limit, filters on
indexed columns (``state``, ``workflow``/``status``) and reads only scalar
columns and single JSON keys, never a whole ``result`` or ``receipt`` document.
Each kind reads two pages a pass: the newest rows, so a fresh stop alerts at
once, and the next page of a cursor that walks the whole window oldest first,
so rows that are already alerted or Your turn can never hide an older stuck
founder. Receipts are read only for the organisations in this pass's candidates
plus those still holding a pending ``founder_stuck`` receipt (kept in the cache),
so a failed Slack post is retried even after its condition leaves the window.

``ARTICLES_FOUNDER_STUCK_ALERTS_ENABLED`` is a kill switch that defaults on.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
import logging
import re

from django.conf import settings
from django.core.cache import cache
from django.db.models import Exists, F, OuterRef, Q
from django.utils import timezone

from workflow_runs.models import ContentFactoryRun

from . import founder_journey_alerts as alerts

logger = logging.getLogger(__name__)

STUCK_REASON = "founder_stuck"
SWEEP_INTERVAL_SECONDS = 600
THROTTLE_KEY = "founder_stuck_alerts:last_sweep"
# Rows read per condition query, and new alerts queued or delivered per sweep.
QUERY_LIMIT = 50
MAX_ALERTS_PER_SWEEP = 25
# Stops older than this are history, not a founder waiting now. Long enough to
# cover legacy founders stuck since before the new journey launched (29 Sep 2026).
LOOKBACK = timedelta(days=90)
OPERATION_RETRY_ATTEMPTS = 10
# A setup that keeps progressing can legitimately be pending for a day (steps,
# then a deployment wait of up to 24 hours), so only twice that reads as stuck.
OPERATION_PENDING_AFTER = timedelta(hours=48)
RUN_STUCK_AFTER = timedelta(hours=6)
CURSOR_KEY = "founder_stuck_alerts:cursor:{kind}"
PENDING_ORGS_KEY = "founder_stuck_alerts:pending_organizations"
CURSOR_TIMEOUT_SECONDS = 7 * 24 * 3600
MAX_PENDING_ORGANIZATIONS = 500

# Website operations a founder waits on. Bookkeeping, cleanup and alert outbox
# operations are left out on purpose: they never block a founder.
FOUNDER_OPERATION_ACTIONS = (
    "prepare", "setup", "reconnect", "repo_scan", "config_write", "custom_contract", "custom-contract",
    "resume", "setup-pr-merge", "verify-merged-setup", "source-reverify", "deployment-verify",
    "ci-verify", "publish-pr", "live-status",
)
RETRYING_OPERATION_STATES = ("pending", "running")
# A wait with its own deadline (deployment, policy) is not a retry loop.
WAITING_RECEIPT_STATUSES = ("waiting_for_deployment", "waiting_for_step")
# These actions count every worker claim as an attempt, including each step and
# each deployment poll, so their attempts only count while a failure is recorded.
POLLING_OPERATION_ACTIONS = ("prepare", "setup", "resume")
OPERATION_FAILURE_KEYS = ("reason_code", "error_code", "last_error")
RUN_STOP_STATUSES = ("failed", "blocked", "stopped", "denied")
SETUP_WORKFLOW = "article_system_setup"
RUN_WORKFLOWS = (*alerts.ARTICLE_WORKFLOWS, SETUP_WORKFLOW)

STEP_LABELS = {
    "prepare": "Website setup",
    "setup": "Website setup",
    "reconnect": "Reconnecting GitHub",
    "repo_scan": "Reading the website",
    "config_write": "Saving the website settings",
    "custom_contract": "Adapting the articles page",
    "custom-contract": "Adapting the articles page",
    "resume": "Resuming website setup",
    "setup-pr-merge": "Merging the setup pull request",
    "verify-merged-setup": "Checking the merged setup",
    "source-reverify": "Re-checking the website code",
    "deployment-verify": "Checking the live website",
    "ci-verify": "Checking the website build",
    "publish-pr": "Publishing an article",
    "live-status": "Waiting for the live website to update",
    "article-live": "Waiting for a merged article to appear on the live website",
    SETUP_WORKFLOW: "Website setup run",
    "article_generation": "Writing an article",
    "article_revision": "Revising an article",
    "confirmed_topic": "Writing an article",
    "content_factory_article": "Writing an article",
    "direct_generate": "Writing an article",
}
KIND_DESCRIPTIONS = {
    "operation_stopped": "The website step stopped and will not retry by itself.",
    "operation_retrying": "The website step keeps retrying without finishing.",
    "run_stopped": "The latest run stopped and the founder has nothing to press.",
    "operation_retired_ask": "The website step waits on an ask the founder's journey does not show.",
    "operation_budget": "Automatic website checks reached their budget; nothing more is dispatched until staff look.",
    "article_not_live": "An article merged on GitHub but its page is not on the founder's live website; it is not counted as published.",
}
MAX_NAME_LENGTH = 80


@dataclass(frozen=True)
class StuckCondition:
    """One stuck founder condition; ``slot`` is its dedupe identity."""

    organization_id: int
    slot: str
    kind: str
    step: str
    code: str
    stuck_since: datetime
    details: dict = field(default_factory=dict)


def run_founder_stuck_alert_sweep(now=None) -> dict:
    """Queue new stuck-founder alerts and deliver pending ones; one bounded pass every 10 minutes."""
    if not getattr(settings, "ARTICLES_FOUNDER_STUCK_ALERTS_ENABLED", True):
        return {"status": "disabled"}
    now = now or timezone.now()
    if not cache.add(THROTTLE_KEY, now.isoformat(), SWEEP_INTERVAL_SECONDS):
        return {"status": "throttled"}
    conditions = find_stuck_conditions(now=now)
    organization_ids = {condition.organization_id for condition in conditions}
    queued = _queue(conditions, organization_ids=organization_ids)
    delivered = _deliver_pending(now=now, organization_ids=organization_ids)
    return {"status": "completed", "candidates": len(conditions), "queued": queued, **delivered}


def _deliver_pending(*, now, organization_ids) -> dict:
    """Deliver pending stuck receipts for this pass's organisations and every one still waiting from earlier passes.

    The waiting set lives in the cache and is capped. When it is missing (a cold
    cache), one pass reads every organisation's receipts to rebuild it; that is
    the same read the founder-journey sweep makes on every tick.
    """
    remembered = cache.get(PENDING_ORGS_KEY)
    if remembered is None:
        scope = None
    else:
        scope = set(organization_ids) | {pk for pk in remembered if isinstance(pk, int)}
    if scope is not None and not scope:
        return {"sent": 0, "unconfigured": 0, "undelivered": 0}
    delivered = alerts._deliver(now=now, limit=MAX_ALERTS_PER_SWEEP, organization_ids=scope, reasons={STUCK_REASON})
    waiting = sorted({target.organization_id for target, _ in alerts._pending_targets(scope, {STUCK_REASON})})
    cache.set(PENDING_ORGS_KEY, waiting[:MAX_PENDING_ORGANIZATIONS], None)
    return delivered


@dataclass(frozen=True)
class Scope:
    """How far detection looks. Alerts wait and page through 90 days; the staff console sees every stop now."""

    organization_ids: frozenset | None = None
    run_stuck_after: timedelta = RUN_STUCK_AFTER
    lookback: timedelta | None = LOOKBACK
    limit: int | None = QUERY_LIMIT


def find_stuck_conditions(*, now) -> list[StuckCondition]:
    """The stuck conditions in this pass's bounded pages: the newest rows plus the cursor's next page, per kind."""
    return _find(now=now, scope=Scope())


def stuck_conditions_by_organization(organization_ids, *, now) -> dict[int, list[StuckCondition]]:
    """The staff console's view: every current stop for these organisations, with no waiting period.

    Scoped to the given organisations, so each query returns at most a few rows
    per organisation (only the newest operation of each action and the newest run).
    """
    ids = frozenset(pk for pk in organization_ids if pk is not None)
    if not ids:
        return {}
    found = {}
    for condition in _find(now=now, scope=Scope(organization_ids=ids, run_stuck_after=timedelta(0), lookback=None, limit=None)):
        found.setdefault(condition.organization_id, []).append(condition)
    return found


def _find(*, now, scope) -> list[StuckCondition]:
    return [*_stuck_operations(now=now, scope=scope), *_stopped_runs(now=now, scope=scope)]


def _pages(queryset, scope, *, kind, pk_field) -> list[dict]:
    """The rows to read this pass: all of them for the console, two bounded pages for the alert.

    The head page (newest ``updated_at`` first) catches a fresh stop at once. The
    cursor page continues, oldest first, from where the last pass stopped, and
    starts again from the oldest row once it reaches the end. So every row in the
    window is read within a bounded number of passes, however many newer rows are
    already alerted or are Your turn.
    """
    if scope.limit is None:
        return list(queryset.order_by("-updated_at", pk_field))
    head = list(queryset.order_by("-updated_at", f"-{pk_field}")[:scope.limit])
    key = CURSOR_KEY.format(kind=kind)
    cursor = _cursor(cache.get(key))
    tail = queryset
    if cursor is not None:
        moment, last = cursor
        tail = tail.filter(Q(updated_at__gt=moment) | Q(updated_at=moment, **{f"{pk_field}__gt": last}))
    tail = list(tail.order_by("updated_at", pk_field)[:scope.limit])
    if len(tail) < scope.limit:
        cache.delete(key)
    else:
        cache.set(key, [tail[-1]["updated_at"].isoformat(), tail[-1][pk_field]], CURSOR_TIMEOUT_SECONDS)
    seen, rows = set(), []
    for row in (*head, *tail):
        if row[pk_field] not in seen:
            seen.add(row[pk_field])
            rows.append(row)
    return rows


def _cursor(value):
    if not isinstance(value, (list, tuple)) or len(value) != 2 or not isinstance(value[1], int):
        return None
    moment = alerts._timestamp(value[0])
    return (moment, value[1]) if moment else None


def stuck_alert_id(condition: StuckCondition) -> str:
    """The receipt id, which is also the Slack client_msg_id."""
    return alerts.founder_journey_alert_id(organization_id=condition.organization_id, alert_reason=STUCK_REASON,
                                           slot=condition.slot)


# --- Detection ---------------------------------------------------------------

OPERATION_FIELDS = ("id", "connection__organization_id", "action", "state", "attempts", "created_at", "updated_at",
                    "receipt__reason_code", "receipt__error_code", "receipt__failure__code", "receipt__code",
                    "receipt__last_error", "receipt__user_action__id")
# Prepare records its code as reason_code (sometimes error_code or failure.code);
# other operations use code or last_error. The first safe code wins.
OPERATION_CODE_FIELDS = ("receipt__reason_code", "receipt__error_code", "receipt__failure__code", "receipt__code",
                         "receipt__last_error")


def _current_operations(*, scope):
    """Founder-path operations on a live connection's current generation, newest of their action only."""
    from .website_models import WebsiteConnectionOperation
    newer = WebsiteConnectionOperation.objects.filter(connection_id=OuterRef("connection_id"), generation=OuterRef("generation"),
                                                      action=OuterRef("action"), created_at__gt=OuterRef("created_at"))
    rows = WebsiteConnectionOperation.objects.filter(action__in=FOUNDER_OPERATION_ACTIONS, generation=F("connection__generation"))
    if scope.organization_ids is not None:
        rows = rows.filter(connection__organization_id__in=scope.organization_ids)
    return rows.exclude(connection__state="disconnected").exclude(Exists(newer))


def _operation_code(row) -> str:
    for value in (row.get(key) for key in OPERATION_CODE_FIELDS):
        if isinstance(value, str) and alerts.SAFE_CODE.fullmatch(value.strip()):
            return value.strip()
    return ""


def _operation_condition(row, *, kind, stuck_since) -> StuckCondition:
    details = {"website_operation_id": str(row["id"]), "operation_state": row["state"], "attempts": row["attempts"]}
    action = row.get("receipt__user_action__id")
    if kind == "retired_ask" and isinstance(action, str) and alerts.SAFE_CODE.fullmatch(action):
        details["action"] = action
    return StuckCondition(
        organization_id=row["connection__organization_id"], slot=f"op-{row['id']}-{kind}", kind=f"operation_{kind}",
        step=row["action"], code=_operation_code(row), stuck_since=stuck_since, details=details)


def _retired_actions():
    """Owner actions the new journey retired; an operation resting on one waits unseen (plan A9)."""
    from .founder_journey import retired_actions
    return sorted(retired_actions())


def _stuck_operations(*, now, scope) -> list[StuckCondition]:
    """Stopped and long-retrying operations (one query for the console, two bounded pages for the alert)."""
    retry_unset = Q(receipt__retryable=False) | ~Q(receipt__has_key="retryable")
    stopped = Q(state="needs_attention") | (Q(state="failed") & retry_unset)
    if scope.lookback is not None:
        stopped &= Q(updated_at__gte=now - scope.lookback)
    retrying = (Q(state__in=RETRYING_OPERATION_STATES)
                & ~(Q(receipt__has_key="status") & Q(receipt__status__in=WAITING_RECEIPT_STATUSES))
                & ((Q(attempts__gte=OPERATION_RETRY_ATTEMPTS) & (~Q(action__in=POLLING_OPERATION_ACTIONS) | _failure_recorded()))
                   | Q(created_at__lte=now - OPERATION_PENDING_AFTER)))
    # Any cohort: an operation parked on a retired owner action is shown to no
    # founder on the new journey, so staff hear of it at once (Lilia, 9 Nov 2026).
    retired = Q(state="needs_user") & Q(receipt__user_action__id__in=_retired_actions())
    # A page choice parked under the founder's "I choose" consent is rendered
    # (plan A9 follow-up), so it is not a hidden ask.
    from .website_models import WebsiteConnectionOperation
    rendered = WebsiteConnectionOperation.objects.filter(state="needs_user", receipt__user_action__id="choose_articles_page",
                                                         receipt__goal__consent__answers__page_policy="pick")
    retired &= ~Q(id__in=rendered.values("id"))
    if scope.lookback is not None:
        retired &= Q(updated_at__gte=now - scope.lookback)
    rows = _pages(_current_operations(scope=scope).filter(stopped | retrying | retired).values(*OPERATION_FIELDS), scope,
                  kind="operations", pk_field="id")
    return [_operation_condition(row, kind="retrying", stuck_since=row["created_at"])
            if row["state"] in RETRYING_OPERATION_STATES
            else _operation_condition(row, kind="retired_ask", stuck_since=row["updated_at"]) if row["state"] == "needs_user"
            else _operation_condition(row, kind="stopped", stuck_since=row["updated_at"]) for row in rows]


def _failure_recorded() -> Q:
    """The receipt holds a failure code right now (prepare clears it when a step succeeds)."""
    recorded = Q()
    for key in OPERATION_FAILURE_KEYS:
        blank = Q(**{f"receipt__{key}": ""}) | Q(**{f"receipt__{key}": None})
        recorded |= Q(receipt__has_key=key) & ~blank
    return recorded


RUN_FIELDS = ("pk", "run_id", "organization_id", "workflow", "status", "updated_at",
              "result__error_code", "result__failure_code", "result__failure__code", "result__authority_denied__code",
              "result__failure__action__id", "result__failure__action_id", "result__action_id",
              "result__self_heal_v1__next_at", "result__self_heal_v1__capped", "result__self_heal_v1__stop__code")


def _stopped_runs(*, now, scope) -> list[StuckCondition]:
    newer = ContentFactoryRun.objects.filter(organization_id=OuterRef("organization_id"), workflow__in=RUN_WORKFLOWS,
                                             created_at__gt=OuterRef("created_at"))
    rows = ContentFactoryRun.objects.filter(workflow__in=RUN_WORKFLOWS, status__in=RUN_STOP_STATUSES,
                                            organization__isnull=False, updated_at__lte=now - scope.run_stuck_after)
    if scope.lookback is not None:
        rows = rows.filter(updated_at__gte=now - scope.lookback)
    if scope.organization_ids is not None:
        rows = rows.filter(organization_id__in=scope.organization_ids)
    rows = _pages(rows.filter(alerts.founder_run_q())
                  .exclude(Exists(newer)).values(*RUN_FIELDS), scope, kind="runs", pk_field="pk")
    conditions = []
    for row in rows:
        code = _run_code(row)
        if _founder_has_next_action(row, code) or _self_heal_scheduled(row, now=now) or _self_heal_stopped(row):
            continue
        conditions.append(StuckCondition(
            organization_id=row["organization_id"], slot=f"run-{row['pk']}-{row['status']}", kind="run_stopped",
            step=row["workflow"], code=code, stuck_since=row["updated_at"],
            details={"run_id": str(row["run_id"]), "run_status": row["status"]}))
    return conditions


def _run_code(row) -> str:
    for key in ("result__error_code", "result__failure_code", "result__failure__code", "result__authority_denied__code"):
        value = row.get(key)
        if isinstance(value, str) and alerts.SAFE_CODE.fullmatch(value.strip()):
            return value.strip()
    return ""


def _founder_has_next_action(row, code) -> bool:
    """A named founder action, or a registry stop the founder can fix, is Your turn, not stuck."""
    for key in ("result__failure__action__id", "result__failure__action_id", "result__action_id"):
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return True
    entry = _registry_entry(code)
    if not (entry and entry.get("failure_class") == "needs_user"):
        return False
    # The staff console's rule: a retired or unknown action id is no ask.
    from .registry_policy import canonical_action_id
    return canonical_action_id(entry.get("action_id")) is not None


def _self_heal_scheduled(row, *, now) -> bool:
    """Self-heal has a future retry booked and is not capped, so the run is not stuck yet."""
    moment = alerts._timestamp(row.get("result__self_heal_v1__next_at"))
    return bool(moment and moment > now and not row.get("result__self_heal_v1__capped"))


def _self_heal_stopped(row) -> bool:
    """Self-heal recorded its own capped stop and already raised ``self_heal_capped`` for it (B2)."""
    value = row.get("result__self_heal_v1__stop__code")
    return isinstance(value, str) and bool(value)


def _registry_entry(code):
    if not code:
        return None
    from .website_failure_contract import load_registry
    registry = load_registry()
    return registry["by_code_v2"].get(code) or registry["by_code"].get(code)


# --- Queueing ----------------------------------------------------------------

def _queue(conditions, *, organization_ids) -> int:
    """Queue each condition once; receipts are read only for these organisations."""
    if not conditions:
        return 0
    from organizations.models import Organization
    known = {organization_id: set(receipts) for organization_id, receipts in alerts._receipt_index(organization_ids)}
    fresh = [c for c in conditions if stuck_alert_id(c) not in known.get(c.organization_id, ())]
    fresh = fresh[:MAX_ALERTS_PER_SWEEP]
    organizations = Organization.objects.only("pk", "billing_user_id").in_bulk({c.organization_id for c in fresh})
    queued = 0
    for condition in fresh:
        organization = organizations.get(condition.organization_id)
        if organization is None:
            continue
        item = "website" if condition.kind.startswith("operation_") or condition.step == SETUP_WORKFLOW else "article"
        details = {**condition.details, "step": condition.step, "kind": condition.kind, "item": item,
                   "stuck_since_at": condition.stuck_since.isoformat()}
        if condition.code:
            details["code"] = condition.code
        alerts.queue_founder_journey_alert(organization=organization, alert_reason=STUCK_REASON, details=details,
                                           slot=condition.slot)
        queued += 1
    return queued


def queue_operation_stopped_alert(op, *, code, now=None) -> int:
    """Queue the stopped-operation alert for ``op`` now, instead of on the next sweep.

    It is the same receipt the sweep would queue for this ``needs_attention``
    operation (same slot), so staff get one alert, not two. Honours
    ``ARTICLES_FOUNDER_STUCK_ALERTS_ENABLED``. Returns how many were queued.
    """
    if not getattr(settings, "ARTICLES_FOUNDER_STUCK_ALERTS_ENABLED", True):
        return 0
    now = now or timezone.now()
    organization_id = op.connection.organization_id
    condition = _operation_condition({"id": op.pk, "connection__organization_id": organization_id, "action": op.action,
                                      "state": op.state, "attempts": op.attempts, "receipt__reason_code": code},
                                     kind="stopped", stuck_since=now)
    return _queue([condition], organization_ids={organization_id})


def queue_budget_alert(connection, op, *, scope, count, limit, now=None) -> int:
    """Queue one alert when automatic scans reach their budget (plan B3).

    The slot is the operation for an operation budget, and the website and UTC
    day for the daily budget, so a loop of pushes raises one alert, not one
    per push. Honours ``ARTICLES_FOUNDER_STUCK_ALERTS_ENABLED``.
    """
    if not getattr(settings, "ARTICLES_FOUNDER_STUCK_ALERTS_ENABLED", True):
        return 0
    now = now or timezone.now()
    organization_id = connection.organization_id
    slot = (f"op-{op.pk}-budget" if scope == "operation"
            else f"website-{connection.pk}-budget-{now.date().isoformat()}")
    condition = StuckCondition(organization_id=organization_id, slot=slot, kind="operation_budget",
                               step=getattr(op, "action", "") or "prepare", code="prepare_no_progress", stuck_since=now,
                               details={"website_operation_id": str(op.pk), "attempts": int(count),
                                        "budget_scope": str(scope), "budget_limit": int(limit)})
    return _queue([condition], organization_ids={organization_id})


def queue_article_not_live_alert(article, *, observation=None, since=None, now=None) -> int:
    """Queue one alert for a merged article whose page is still not live (plan C14).

    The slot is the article and its merge commit, so later re-checks of the
    same merge never alert again, and a new merge (a republish) alerts once
    more. Honours ``ARTICLES_FOUNDER_STUCK_ALERTS_ENABLED``.
    """
    if not getattr(settings, "ARTICLES_FOUNDER_STUCK_ALERTS_ENABLED", True):
        return 0
    now = now or timezone.now()
    merge = re.sub(r"[^a-f0-9]", "", str(article.on_main_commit_sha or article.merge_commit_sha or "").lower())[:12]
    observation = observation if isinstance(observation, dict) else {}
    reason = str(observation.get("reason") or "")
    check = ("content_mismatch" if observation.get("state") == "content_mismatch"
             else "not_found" if "HTTP 404" in reason else "unavailable")
    condition = StuckCondition(organization_id=article.organization_id,
                               slot=f"article-{article.pk}-not-live-{merge or 'unknown'}", kind="article_not_live",
                               step="article-live", code="live_route_unavailable", stuck_since=since or now,
                               details={"run_id": str(article.source_run_id or "")[:100],
                                        "live_check": check})
    queued = _queue([condition], organization_ids={article.organization_id})
    if queued:
        # Queued outside the sweep: remember the organisation so the next pass
        # delivers it even while the pending set is warm.
        remembered = cache.get(PENDING_ORGS_KEY)
        if isinstance(remembered, list) and article.organization_id not in remembered:
            cache.set(PENDING_ORGS_KEY, [*remembered, article.organization_id][-MAX_PENDING_ORGANIZATIONS:], None)
    return queued


# --- Plain descriptions ------------------------------------------------------

def describe(condition: StuckCondition, *, now) -> dict:
    """The staff console's stuck reason: step, code, its registry description, and since when."""
    entry = _registry_entry(condition.code)
    return {"kind": condition.kind, "step": condition.step,
            "stepLabel": STEP_LABELS.get(condition.step, condition.step),
            "code": condition.code or None,
            "description": (entry or {}).get("user_message") or KIND_DESCRIPTIONS.get(condition.kind, ""),
            "since": condition.stuck_since.isoformat(),
            "stuckMinutes": max(0, int((now - condition.stuck_since).total_seconds() // 60)),
            **{key: value for key, value in condition.details.items()
               if key in ("run_id", "website_operation_id", "attempts")}}


# --- Slack text --------------------------------------------------------------

def stuck_alert_text(receipt, context) -> str:
    """Plain staff text: who, which startup, which step, why, for how long, and where to look."""
    details = receipt.get("details") if isinstance(receipt.get("details"), dict) else {}
    organization_id = context.get("org_id")
    startup, founder = _names(organization_id)
    code = str(details.get("code") or "")
    entry = _registry_entry(code)
    description = (entry or {}).get("user_message") or KIND_DESCRIPTIONS.get(str(details.get("kind")), "")
    step = STEP_LABELS.get(str(details.get("step") or ""), str(details.get("step") or "Unknown step"))
    parts = [f"Founder stuck: {founder} at {startup}" + (f" ({context['domain']})." if context.get("domain") else ".")]
    parts.append(f"Step: {step}.")
    parts.append(f"Reason: {code + ': ' if code else ''}{description or 'No failure code was reported.'}")
    since = alerts._timestamp(details.get("stuck_since_at"))
    if since:
        parts.append(f"Stuck for {_duration(timezone.now() - since)}.")
    if details.get("action"):
        parts.append(f"Waiting on the retired ask {alerts._safe(details['action'])}.")
    if details.get("live_check"):
        parts.append(f"Live check: {alerts._safe(details['live_check'])}.")
    if details.get("attempts"):
        parts.append(f"Attempts {details['attempts']}.")
    if details.get("run_id"):
        parts.append(f"Run {alerts._safe(details['run_id'])}.")
    if details.get("website_operation_id"):
        parts.append(f"Website operation {alerts._safe(details['website_operation_id'])}.")
    link = staff_console_link(organization_id)
    if link:
        parts.append(f"Staff console: {link}")
    parts.append(f"Alert delivery attempt {receipt.get('attempts') or 0} of {alerts.MAX_DELIVERY_ATTEMPTS}.")
    return " ".join(parts)


def staff_console_link(organization_id) -> str:
    base = str(getattr(settings, "COMMUNITY_CHAT_FRONTEND_URL", "") or "").rstrip("/")
    return f"{base}/admin/startups/{organization_id}" if base and organization_id else ""


def _names(organization_id) -> tuple[str, str]:
    """The startup and founder names for staff, read at send time."""
    from founder_tools.models import VibeRaisingCompany
    from organizations.models import Organization
    organization = Organization.objects.select_related("billing_user").filter(pk=organization_id).first()
    company = (VibeRaisingCompany.objects.select_related("profile__user").filter(organization_id=organization_id)
               .order_by("created_at", "pk").first())
    startup = (company.name if company and company.name else getattr(organization, "name", "")) or "an unnamed startup"
    owner = company.profile.user if company else getattr(organization, "billing_user", None)
    founder = (getattr(owner, "full_name", "") or "") if owner else ""
    return _slack_plain(startup), _slack_plain(founder) or "A founder"


def _slack_plain(value) -> str:
    """One line, no Slack control characters, bounded length."""
    text = re.sub(r"\s+", " ", str(value or "")).strip()[:MAX_NAME_LENGTH]
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _duration(delta: timedelta) -> str:
    minutes = max(0, int(delta.total_seconds() // 60))
    days, rest = divmod(minutes, 24 * 60)
    hours, minutes = divmod(rest, 60)
    if days:
        return f"{days} day{'s' if days != 1 else ''}" + (f" {hours} hour{'s' if hours != 1 else ''}" if hours else "")
    if hours:
        return f"{hours} hour{'s' if hours != 1 else ''}"
    return f"{minutes} minute{'s' if minutes != 1 else ''}"
