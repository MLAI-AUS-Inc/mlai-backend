"""One always-on "founder stuck" alert for every cohort.

A founder can be stuck in three ways that the cohort-gated alerts miss:

* a website setup step stopped (``needs_attention``, or ``failed`` with no
  automatic retry) on the connection's current generation;
* a website step is still retrying after many attempts or a long time (La Vie's
  reconnect sat pending after 21 attempts);
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

Scheduler safety (the 10 Nov 2026 OOM, fixed by #1325): the sweep runs at most
once per ``SWEEP_INTERVAL_SECONDS``, every query has a row limit, filters on
indexed columns (``state``, ``workflow``/``status``) and reads only scalar
columns and single JSON keys, never a whole ``result`` or ``receipt`` document.
Receipts are read only for the organisations in this sweep's candidates.

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
# Stops older than this are history, not a founder waiting now.
LOOKBACK = timedelta(days=14)
OPERATION_RETRY_ATTEMPTS = 10
OPERATION_PENDING_AFTER = timedelta(hours=24)
RUN_STUCK_AFTER = timedelta(hours=6)

# Website operations a founder waits on. Bookkeeping, cleanup and alert outbox
# operations are left out on purpose: they never block a founder.
FOUNDER_OPERATION_ACTIONS = (
    "prepare", "setup", "reconnect", "repo_scan", "config_write", "custom_contract", "custom-contract",
    "resume", "setup-pr-merge", "verify-merged-setup", "source-reverify", "deployment-verify",
    "ci-verify", "publish-pr",
)
RETRYING_OPERATION_STATES = ("pending", "running")
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
    try:
        since = alerts.alerts_since()
    except alerts.FounderJourneyAlertConfigError as exc:
        return {"status": "failed", "error": str(exc)}
    conditions = find_stuck_conditions(now=now, since=since)
    organization_ids = {condition.organization_id for condition in conditions}
    queued = _queue(conditions, organization_ids=organization_ids)
    delivered = alerts._deliver(now=now, limit=MAX_ALERTS_PER_SWEEP, organization_ids=organization_ids,
                                reasons={STUCK_REASON}) if organization_ids else {"sent": 0, "unconfigured": 0, "undelivered": 0}
    return {"status": "completed", "candidates": len(conditions), "queued": queued, **delivered}


@dataclass(frozen=True)
class Scope:
    """How far detection looks. Alerts wait and look back 14 days; the staff console sees every stop now."""

    since: datetime | None = None
    organization_ids: frozenset | None = None
    run_stuck_after: timedelta = RUN_STUCK_AFTER
    lookback: timedelta | None = LOOKBACK
    limit: int | None = QUERY_LIMIT


def find_stuck_conditions(*, now, since) -> list[StuckCondition]:
    """Every stuck condition the bounded alert queries find, newest first within each kind."""
    return _find(now=now, scope=Scope(since=since))


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


def _bounded(queryset, scope):
    return queryset[:scope.limit] if scope.limit is not None else queryset


def stuck_alert_id(condition: StuckCondition) -> str:
    """The receipt id, which is also the Slack client_msg_id."""
    return alerts.founder_journey_alert_id(organization_id=condition.organization_id, alert_reason=STUCK_REASON,
                                           slot=condition.slot)


# --- Detection ---------------------------------------------------------------

OPERATION_FIELDS = ("id", "connection__organization_id", "action", "state", "attempts", "created_at", "updated_at",
                    "receipt__code", "receipt__last_error")


def _current_operations(*, scope):
    """Founder-path operations on a live connection's current generation, newest of their action only."""
    from .website_models import WebsiteConnectionOperation
    newer = WebsiteConnectionOperation.objects.filter(connection_id=OuterRef("connection_id"), generation=OuterRef("generation"),
                                                      action=OuterRef("action"), created_at__gt=OuterRef("created_at"))
    rows = WebsiteConnectionOperation.objects.filter(action__in=FOUNDER_OPERATION_ACTIONS, generation=F("connection__generation"))
    if scope.since is not None:
        rows = rows.filter(created_at__gte=scope.since)
    if scope.organization_ids is not None:
        rows = rows.filter(connection__organization_id__in=scope.organization_ids)
    return rows.exclude(connection__state="disconnected").exclude(Exists(newer))


def _operation_code(row) -> str:
    for value in (row.get("receipt__code"), row.get("receipt__last_error")):
        if isinstance(value, str) and alerts.SAFE_CODE.fullmatch(value.strip()):
            return value.strip()
    return ""


def _operation_condition(row, *, kind, stuck_since) -> StuckCondition:
    return StuckCondition(
        organization_id=row["connection__organization_id"], slot=f"op-{row['id']}-{kind}", kind=f"operation_{kind}",
        step=row["action"], code=_operation_code(row), stuck_since=stuck_since,
        details={"website_operation_id": str(row["id"]), "operation_state": row["state"], "attempts": row["attempts"]})


def _stuck_operations(*, now, scope) -> list[StuckCondition]:
    """Stopped and long-retrying operations in one bounded query (one round trip for the console)."""
    retry_unset = Q(receipt__retryable=False) | ~Q(receipt__has_key="retryable")
    stopped = Q(state="needs_attention") | (Q(state="failed") & retry_unset)
    if scope.lookback is not None:
        stopped &= Q(updated_at__gte=now - scope.lookback)
    retrying = Q(state__in=RETRYING_OPERATION_STATES) & (
        Q(attempts__gte=OPERATION_RETRY_ATTEMPTS) | Q(created_at__lte=now - OPERATION_PENDING_AFTER))
    rows = _bounded(_current_operations(scope=scope).filter(stopped | retrying)
                    .order_by("-updated_at", "id").values(*OPERATION_FIELDS), scope)
    return [_operation_condition(row, kind="retrying", stuck_since=row["created_at"])
            if row["state"] in RETRYING_OPERATION_STATES
            else _operation_condition(row, kind="stopped", stuck_since=row["updated_at"]) for row in rows]


RUN_FIELDS = ("pk", "run_id", "organization_id", "workflow", "status", "updated_at",
              "result__error_code", "result__failure_code", "result__failure__code", "result__authority_denied__code",
              "result__failure__action__id", "result__failure__action_id", "result__action_id",
              "result__self_heal_v1__next_at", "result__self_heal_v1__capped")


def _stopped_runs(*, now, scope) -> list[StuckCondition]:
    newer = ContentFactoryRun.objects.filter(organization_id=OuterRef("organization_id"), workflow__in=RUN_WORKFLOWS,
                                             created_at__gt=OuterRef("created_at"))
    rows = ContentFactoryRun.objects.filter(workflow__in=RUN_WORKFLOWS, status__in=RUN_STOP_STATUSES,
                                            organization__isnull=False, updated_at__lte=now - scope.run_stuck_after)
    if scope.since is not None:
        rows = rows.filter(created_at__gte=scope.since)
    if scope.lookback is not None:
        rows = rows.filter(updated_at__gte=now - scope.lookback)
    if scope.organization_ids is not None:
        rows = rows.filter(organization_id__in=scope.organization_ids)
    rows = _bounded(rows.filter(~Q(run_request__has_key="request_source")
                                | ~Q(run_request__request_source=alerts.SCHEDULER_REQUEST_SOURCE))
                    .exclude(Exists(newer)).order_by("-updated_at", "pk").values(*RUN_FIELDS), scope)
    conditions = []
    for row in rows:
        code = _run_code(row)
        if _founder_has_next_action(row, code) or _self_heal_scheduled(row, now=now):
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
    return bool(entry and entry.get("failure_class") == "needs_user" and entry.get("action_id"))


def _self_heal_scheduled(row, *, now) -> bool:
    """Self-heal has a future retry booked and is not capped, so the run is not stuck yet."""
    moment = alerts._timestamp(row.get("result__self_heal_v1__next_at"))
    return bool(moment and moment > now and not row.get("result__self_heal_v1__capped"))


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
        details = {**condition.details, "step": condition.step, "kind": condition.kind,
                   "stuck_since_at": condition.stuck_since.isoformat()}
        if condition.code:
            details["code"] = condition.code
        alerts.queue_founder_journey_alert(organization=organization, alert_reason=STUCK_REASON, details=details,
                                           slot=condition.slot)
        queued += 1
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
