"""Org-agnostic founder-journey ops alerts.

A founder who gets stuck should never be stuck silently. This sweep tells ops,
once, when a founder's run stops or blocks, when a startup has no draft a day
after it was created, or when a founder has been left on "Your turn" for three
days. Unlike the goal-scoped website operational alerts, it needs neither goal
enrolment nor a website connection, so it covers every new founder.

Detection writes a ``pending`` receipt; delivery posts it to the ops Slack
channel with the alert id as ``client_msg_id`` and at most three attempts.
Every receipt, run-scoped ones included, lives in
``OrganizationContentConfig.pillar_strategy['ops_alerts']`` keyed by alert id
(kept by ``editorial_catalog.merge_strategy``, hidden by ``public_strategy``).
That index is the only thing dedupe and delivery read. Run-scoped receipts are
also mirrored into ``ContentFactoryRun.result['ops_alerts'][reason]`` for the
run's readers, but a status poll may replace a run's result wholesale, so the
mirror is never trusted alone. Receipts are written with ``QuerySet.update`` so
recording an alert never looks like founder activity (``updated_at`` is
untouched).

Without a valid channel every alert still writes one structured
``founder_journey_alert`` warning and its receipt becomes ``unconfigured``.
Alert text carries ids, the workflow, the error code and a staff admin link,
never a founder's email, name or article text.
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime, time, timedelta, timezone as dt_timezone
from types import SimpleNamespace
from uuid import NAMESPACE_URL, uuid5

from django.conf import settings
from django.db import transaction
from django.db.models import Exists, Max, OuterRef, Q
from django.urls import NoReverseMatch, reverse
from django.utils import timezone

from workflow_runs.models import ContentFactoryRun, ContentFactoryRunStep, ContentFactoryStepStatus

logger = logging.getLogger(__name__)

RECEIPTS_KEY = "ops_alerts"
# Reasons the sweep detects itself; the rest are queued by their owners (BE1, BE3).
DETECTED_REASONS = ("run_terminal", "run_blocked", "startup_autofill_blocked", "your_turn_72h", "no_draft_24h")
ALERT_REASONS = frozenset({
    "run_terminal",
    "run_blocked",
    "startup_autofill_blocked",
    "no_draft_24h",
    "your_turn_72h",
    "domain_claim_disputed",
    "first_article_attempts_exhausted",
    "free_cap_spike",
})
REASON_LABELS = {
    "run_terminal": "A founder run stopped",
    "run_blocked": "A founder run is blocked",
    "startup_autofill_blocked": "Startup autofill is blocked",
    "no_draft_24h": "A startup has no delivered draft 24 hours after it was created",
    "your_turn_72h": "A founder has been waiting on their turn for over 72 hours",
    "domain_claim_disputed": "A founder tried to claim a domain another startup holds",
    "first_article_attempts_exhausted": "A founder used every free first-article attempt",
    "free_cap_spike": "Free first-article reservations spiked",
}
TERMINAL_STATUSES = frozenset({"failed", "cancelled", "denied", "stopped"})
YOUR_TURN_STATUSES = frozenset({"awaiting_approval", "approval_required", "awaiting_confirmation", "awaiting_delivery_mode"})
# IF-20 owner actions. Every other cancellation_reason, 'unspecified' included, alerts.
OWNER_CANCELLATION_REASONS = ("owner_cancelled", "draft_deleted", "article_discarded")
SCHEDULER_WORKFLOWS = ("island_refresh",)
SCHEDULER_REQUEST_SOURCE = "mlai_backend_scheduler"
# Until owner cancels record cancellation_reason (BE6), this is the shape
# _cancel_local_article_run leaves: empty error, steps 'Cancelled by user.'.
LEGACY_OWNER_CANCEL_MESSAGE = "Cancelled by user."
ARTICLE_WORKFLOWS = ("article_generation", "article_revision", "confirmed_topic", "content_factory_article", "direct_generate")
DELIVERED_DRAFT_STATUSES = ("completed", "awaiting_approval")
NO_DRAFT_AFTER = timedelta(hours=24)
YOUR_TURN_AFTER = timedelta(hours=72)
MAX_DELIVERY_ATTEMPTS = 3
RETRY_AFTER = timedelta(minutes=5)
SLACK_CHANNEL_PATTERN = re.compile(r"[CG][A-Z0-9]{8,}")
SAFE_TOKEN = re.compile(r"[A-Za-z0-9_.:\-]{1,100}")
SAFE_CODE = re.compile(r"[A-Za-z][A-Za-z0-9_.\-]{0,79}")
DETAIL_KEY = re.compile(r"[a-z][a-z0-9_]{0,40}")
# Substrings that mark a detail as personal or article content. Such details are
# dropped before they are stored, so they can never reach Slack.
PRIVATE_DETAIL_MARKERS = ("email", "name", "title", "text", "content", "body", "article", "markdown", "prompt", "phone")


class FounderJourneyAlertConfigError(ValueError):
    """ARTICLES_FOUNDER_JOURNEY_ALERTS_SINCE is not an ISO date."""


def founder_journey_alert_id(*, organization_id, alert_reason, run_id=None, company_id=None, claimant_user_id=None) -> str:
    """Return the stable alert id for one (org, slot, reason); it is also the Slack client_msg_id."""
    if run_id:
        slot = str(run_id)
    elif company_id:
        slot = str(company_id)
    elif alert_reason == "domain_claim_disputed" and claimant_user_id:
        slot = f"claimant-user-{claimant_user_id}"
    else:
        slot = "-"
    return str(uuid5(NAMESPACE_URL, f"mlai:founder-journey:{organization_id}:{slot}:{alert_reason}"))


def queue_founder_journey_alert(*, organization, alert_reason: str, company_id=None, run=None, details: dict | None = None) -> str:
    """Record one pending founder-journey alert idempotently and return its alert id.

    The sweep delivers it. The receipt is stored in the org's content config
    (created when missing); with ``run`` it is also mirrored onto the run. Call
    this outside any transaction that is about to roll back, or the receipt
    rolls back with it.
    For ``domain_claim_disputed`` pass ``details={"claimant_user_id": user.pk}``
    so each claimant gets their own alert.
    """
    if alert_reason not in ALERT_REASONS:
        raise ValueError(f"Unknown founder-journey alert_reason: {alert_reason!r}")
    if organization is None or getattr(organization, "pk", None) is None:
        raise ValueError("A founder-journey alert needs a saved organization.")
    safe_details = _safe_details(details)
    alert_id = founder_journey_alert_id(
        organization_id=organization.pk, alert_reason=alert_reason,
        run_id=getattr(run, "run_id", None), company_id=company_id,
        claimant_user_id=safe_details.get("claimant_user_id"),
    )
    receipt = {
        "alert_id": alert_id, "alert_reason": alert_reason, "state": "pending", "attempts": 0, "sent_at": None,
        "queued_at": timezone.now().isoformat(),
        "company_id": str(company_id) if company_id else None,
        "run_id": str(run.run_id) if run is not None else None,
        "run_pk": run.pk if run is not None else None,
        "details": safe_details,
    }
    if run is not None:
        mirrored = _receipts_in(ContentFactoryRun.objects.filter(pk=run.pk).values_list("result", flat=True).first())
        mirrored = mirrored.get(alert_reason)
        if isinstance(mirrored, dict) and mirrored.get("alert_id") == alert_id:
            # A receipt only the run still holds (its index entry was lost) is adopted, never re-sent.
            receipt = {**receipt, **mirrored}
    stored = _queue_org_receipt(organization, alert_id, receipt)
    if run is not None:
        _mirror_run_receipt(run, alert_reason, stored)
    return alert_id


def run_founder_journey_alert_sweep(now=None, limit=None) -> dict:
    """Queue newly stuck founders and deliver pending alerts; one scheduler tick."""
    if not getattr(settings, "ARTICLES_FOUNDER_JOURNEY_ALERTS_ENABLED", False):
        return {"status": "disabled"}
    now = now or timezone.now()
    limit = max(1, int(limit if limit is not None else getattr(settings, "ARTICLES_FOUNDER_JOURNEY_ALERTS_MAX_PER_TICK", 20)))
    try:
        since = alerts_since()
    except FounderJourneyAlertConfigError as exc:
        return {"status": "failed", "error": str(exc)}
    queued = _detect(now=now, since=since, limit=limit)
    delivered = _deliver(now=now, limit=limit)
    return {"status": "completed", "queued": sum(queued.values()), "queued_by_reason": queued, **delivered}


def alerts_since() -> datetime:
    """The earliest run/company created_at the sweep considers (UTC midnight)."""
    raw = str(getattr(settings, "ARTICLES_FOUNDER_JOURNEY_ALERTS_SINCE", "2026-09-29") or "").strip()
    try:
        day = date.fromisoformat(raw)
    except ValueError as exc:
        raise FounderJourneyAlertConfigError("ARTICLES_FOUNDER_JOURNEY_ALERTS_SINCE must be an ISO date (YYYY-MM-DD).") from exc
    return datetime.combine(day, time.min, tzinfo=dt_timezone.utc)


def organization_has_delivered_draft(organization_id) -> bool:
    """A written article, or an article run that finished or is ready for review."""
    return bool(organizations_with_delivered_drafts([organization_id]))


def organizations_with_delivered_drafts(organization_ids, *, using="default") -> set:
    """The subset of ``organization_ids`` with a delivered draft (shared with founder_funnel_report)."""
    from .models import WrittenArticle
    ids = {pk for pk in organization_ids if pk is not None}
    if not ids:
        return set()
    written = WrittenArticle.objects.using(using).filter(organization_id__in=ids).values_list("organization_id", flat=True)
    drafted = (ContentFactoryRun.objects.using(using)
               .filter(organization_id__in=ids, workflow__in=ARTICLE_WORKFLOWS, status__in=DELIVERED_DRAFT_STATUSES)
               .values_list("organization_id", flat=True))
    return set(written) | set(drafted)


# --- Detection ---------------------------------------------------------------

def run_alert_reasons(run, *, now) -> list[str]:
    """Which run-scoped alerts this run qualifies for now (receipts not considered)."""
    if run.organization_id is None or is_scheduler_run(run):
        return []
    reasons = []
    status = str(run.status or "")
    if status in TERMINAL_STATUSES and not is_owner_cancel(run) and not is_first_article_switch(run):
        reasons.append("run_terminal")
    if status == "blocked":
        reasons.append("startup_autofill_blocked" if run.workflow == "startup_autofill" else "run_blocked")
    waiting = status in YOUR_TURN_STATUSES or (status == "blocked" and bool(run_action_id(run.result)))
    if waiting and run.updated_at and run.updated_at <= now - YOUR_TURN_AFTER:
        reasons.append("your_turn_72h")
    return reasons


FIRST_ARTICLE_REQUEST_PREFIX = "first-article:"


def is_first_article_switch(run) -> bool:
    """A free first article the owner replaced with a new topic (P1.7), which is not an incident.

    Only a run started by the first-article card (request id
    ``first-article:...``) and cancelled as ``superseded`` qualifies, and only
    while ``ARTICLES_FIRST_ARTICLE_CARD_ENABLED`` is on. Other superseded stops
    (website reconciliation, retirement) still alert as before.
    """
    if run.status != "cancelled" or not getattr(settings, "ARTICLES_FIRST_ARTICLE_CARD_ENABLED", False):
        return False
    result = run.result if isinstance(run.result, dict) else {}
    request = run.run_request if isinstance(run.run_request, dict) else {}
    return (result.get("cancellation_reason") == "superseded"
            and str(request.get("client_request_id") or "").startswith(FIRST_ARTICLE_REQUEST_PREFIX))


def is_scheduler_run(run) -> bool:
    """Island refresh or ``mlai_backend_scheduler`` runs, which are never alerted (IF-15)."""
    request = run.run_request if isinstance(run.run_request, dict) else {}
    return run.workflow in SCHEDULER_WORKFLOWS or request.get("request_source") == SCHEDULER_REQUEST_SOURCE


def is_owner_cancel(run) -> bool:
    """An owner's own cancel (IF-20 reason, or the legacy pre-BE6 shapes) is not an incident.

    Before BE6, ``_cancel_local_article_run`` leaves an empty error and rewrites
    unfinished steps to 'Cancelled by user.'. Discarding a finished draft has no
    unfinished step to rewrite, so a cancelled run whose steps all completed and
    whose result says ``cancelled`` is treated as an owner cancel too: a system
    stop always interrupts a step that has not completed.
    """
    if run.status != "cancelled":
        return False
    result = run.result if isinstance(run.result, dict) else {}
    if "cancellation_reason" in result:
        return result.get("cancellation_reason") in OWNER_CANCELLATION_REASONS
    if run.error or "":
        return False
    if run.steps.filter(message=LEGACY_OWNER_CANCEL_MESSAGE).exists():
        return True
    steps = run.steps.all()
    return (result.get("cancelled") is True and steps.exists()
            and not steps.exclude(status=ContentFactoryStepStatus.COMPLETED).exists())


def run_action_id(result) -> str:
    """The founder action a blocked run is waiting on, if it names one."""
    result = result if isinstance(result, dict) else {}
    failure = result.get("failure") if isinstance(result.get("failure"), dict) else {}
    action = failure.get("action") if isinstance(failure.get("action"), dict) else {}
    for value in (action.get("id"), failure.get("action_id"), result.get("action_id")):
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _without_json_key(path, key):
    """NULL-safe 'path lacks key': a negated nested has_key is NULL on Postgres when the parent is absent."""
    parent, _, child = path.rpartition("__")
    absent = ~Q(**{f"{path}__has_key": key})
    return absent if not parent else ~Q(**{f"{parent}__has_key": child}) | absent


def _detect(*, now, since, limit) -> dict:
    budget = [limit]
    counts = dict.fromkeys(DETECTED_REASONS, 0)
    # Every org's alert ids, read once per tick; kept current as this tick queues.
    known = {org_id: set(receipts) for org_id, receipts in _receipt_index()}

    def seen(organization_id, alert_id):
        return alert_id in known.get(organization_id, ())

    def queue(reason, **kwargs):
        alert_id = queue_founder_journey_alert(alert_reason=reason, **kwargs)
        known.setdefault(kwargs["organization"].pk, set()).add(alert_id)
        counts[reason] += 1
        budget[0] -= 1

    base = (ContentFactoryRun.objects.filter(created_at__gte=since, organization__isnull=False)
            .exclude(workflow__in=SCHEDULER_WORKFLOWS)
            .filter(~Q(run_request__has_key="request_source") | ~Q(run_request__request_source=SCHEDULER_REQUEST_SOURCE)))
    steps = ContentFactoryRunStep.objects.filter(run=OuterRef("pk"))
    legacy_owner_cancel = (Q(status="cancelled", error="") & ~Q(result__has_key="cancellation_reason") & (
        Q(Exists(steps.filter(message=LEGACY_OWNER_CANCEL_MESSAGE)))
        | (Q(result__cancelled=True) & Q(Exists(steps))
           & ~Q(Exists(steps.exclude(status=ContentFactoryStepStatus.COMPLETED))))))
    candidates = {
        "run_terminal": base.filter(status__in=TERMINAL_STATUSES).exclude(legacy_owner_cancel).filter(
            ~Q(result__has_key="cancellation_reason") | ~Q(result__cancellation_reason__in=OWNER_CANCELLATION_REASONS)),
        "run_blocked": base.filter(status="blocked").exclude(workflow="startup_autofill"),
        "startup_autofill_blocked": base.filter(status="blocked", workflow="startup_autofill"),
        "your_turn_72h": base.filter(updated_at__lte=now - YOUR_TURN_AFTER).filter(
            Q(status__in=YOUR_TURN_STATUSES) | (Q(status="blocked") & (
                Q(result__failure__action__has_key="id") | Q(result__failure__has_key="action_id")
                | Q(result__has_key="action_id")))),
    }
    for reason, queryset in candidates.items():
        # Ids first: results can be large, and receipts are written while we walk.
        # The mirror prefilter is only a shortcut; the org index decides.
        rows = list(queryset.filter(_without_json_key(f"result__{RECEIPTS_KEY}", reason))
                    .order_by("created_at", "pk").values_list("pk", "organization_id", "run_id"))
        for pk, organization_id, run_id in rows:
            if budget[0] <= 0:
                return counts
            if seen(organization_id, founder_journey_alert_id(
                    organization_id=organization_id, alert_reason=reason, run_id=run_id)):
                continue
            run = ContentFactoryRun.objects.select_related("organization").filter(pk=pk).first()
            if run and reason in run_alert_reasons(run, now=now) and reason not in _receipts_in(run.result):
                queue(reason, organization=run.organization, run=run)
    if budget[0] > 0:
        _detect_companies_without_draft(now=now, since=since, budget=budget, queue=queue, seen=seen)
    if budget[0] > 0:
        _detect_waiting_website_operations(now=now, since=since, budget=budget, queue=queue, seen=seen)
    return counts


def _detect_companies_without_draft(*, now, since, budget, queue, seen):
    from founder_tools.models import VibeRaisingCompany
    from organizations.models import Organization
    companies = (VibeRaisingCompany.objects.filter(created_at__gte=since, created_at__lte=now - NO_DRAFT_AFTER,
                                                   organization__isnull=False)
                 .exclude(profile__user__is_staff=True).order_by("created_at", "pk")
                 .values_list("pk", "organization_id"))
    unalerted = [(company_id, organization_id) for company_id, organization_id in companies
                 if not seen(organization_id, founder_journey_alert_id(
                     organization_id=organization_id, alert_reason="no_draft_24h", company_id=company_id))]
    for company_id, organization_id in unalerted:
        if budget[0] <= 0:
            return
        organization = Organization.objects.filter(pk=organization_id).first()
        if organization is None or organization_has_delivered_draft(organization_id):
            continue
        queue("no_draft_24h", organization=organization, company_id=company_id)


def _detect_waiting_website_operations(*, now, since, budget, queue, seen):
    from .website_models import WebsiteConnectionOperation
    operations = (WebsiteConnectionOperation.objects.filter(state="needs_user", created_at__gte=since,
                                                             updated_at__lte=now - YOUR_TURN_AFTER)
                  .select_related("connection__organization").order_by("created_at", "pk"))
    for operation in list(operations):
        if budget[0] <= 0:
            return
        organization = operation.connection.organization
        if seen(organization.pk, founder_journey_alert_id(organization_id=organization.pk, alert_reason="your_turn_72h")):
            continue
        queue("your_turn_72h", organization=organization, details={"website_operation": operation.action})


# --- Receipts ----------------------------------------------------------------

def _receipts_in(result) -> dict:
    receipts = (result if isinstance(result, dict) else {}).get(RECEIPTS_KEY)
    return receipts if isinstance(receipts, dict) else {}


def _receipt_index():
    """(organization_id, receipts) for every org holding receipts; reads only the receipt JSON."""
    from .models import OrganizationContentConfig
    rows = (OrganizationContentConfig.objects.filter(pillar_strategy__has_key=RECEIPTS_KEY)
            .order_by("pk").values_list("organization_id", f"pillar_strategy__{RECEIPTS_KEY}"))
    return [(organization_id, receipts if isinstance(receipts, dict) else {}) for organization_id, receipts in rows]


def _queue_org_receipt(organization, alert_id, receipt) -> dict:
    """Store ``receipt`` under ``alert_id`` unless one is there; return the stored receipt."""
    from .models import OrganizationContentConfig
    with transaction.atomic():
        config, _ = OrganizationContentConfig.objects.select_for_update().get_or_create(organization=organization)
        strategy = dict(config.pillar_strategy) if isinstance(config.pillar_strategy, dict) else {}
        receipts = dict(_receipts_in(strategy))
        if isinstance(receipts.get(alert_id), dict):
            return receipts[alert_id]
        receipts[alert_id] = receipt
        OrganizationContentConfig.objects.filter(pk=config.pk).update(pillar_strategy={**strategy, RECEIPTS_KEY: receipts})
        return receipt


def _mirror_run_receipt(run, alert_reason, receipt):
    """Copy an index receipt onto its run (its own transaction, after the config lock is released)."""
    with transaction.atomic():
        locked = ContentFactoryRun.objects.select_for_update().filter(pk=run.pk).values_list("result", flat=True).first()
        result = dict(locked) if isinstance(locked, dict) else {}
        receipts = dict(_receipts_in(result))
        if receipts.get(alert_reason) != receipt:
            receipts[alert_reason] = receipt
            ContentFactoryRun.objects.filter(pk=run.pk).update(result={**result, RECEIPTS_KEY: receipts})
    # A caller that saves its in-memory run afterwards keeps the receipt too.
    if hasattr(run, "result"):
        current = run.result if isinstance(run.result, dict) else {}
        run.result = {**current, RECEIPTS_KEY: {**_receipts_in(current), alert_reason: receipt}}


def _safe_details(details) -> dict:
    """Keep only scalar, non-personal details; anything resembling text or identity is dropped."""
    safe = {}
    for key, value in (details if isinstance(details, dict) else {}).items():
        key = str(key)
        if not DETAIL_KEY.fullmatch(key) or any(marker in key for marker in PRIVATE_DETAIL_MARKERS):
            continue
        if isinstance(value, bool) or isinstance(value, int):
            safe[key] = value
        elif isinstance(value, str) and SAFE_TOKEN.fullmatch(value) and "@" not in value:
            safe[key] = value
    return safe


# --- Delivery ----------------------------------------------------------------

class _Target:
    """One pending index receipt and how to lock, read and write it."""

    def __init__(self, *, organization_id, key):
        self.organization_id, self.key = organization_id, key

    def lock(self):
        from .models import OrganizationContentConfig
        row = OrganizationContentConfig.objects.select_for_update().filter(organization_id=self.organization_id).first()
        container = row.pillar_strategy if row is not None else None
        container = dict(container) if isinstance(container, dict) else {}
        receipt = _receipts_in(container).get(self.key)
        return row, container, dict(receipt) if isinstance(receipt, dict) else None

    def write(self, row, container, receipt):
        from .models import OrganizationContentConfig
        receipts = {**_receipts_in(container), self.key: receipt}
        OrganizationContentConfig.objects.filter(pk=row.pk).update(pillar_strategy={**container, RECEIPTS_KEY: receipts})


def _pending_targets() -> list:
    """Every pending index receipt, oldest queued first."""
    targets = [(_Target(organization_id=organization_id, key=key), receipt)
               for organization_id, receipts in _receipt_index()
               for key, receipt in receipts.items()
               if isinstance(receipt, dict) and receipt.get("state") == "pending"]
    return sorted(targets, key=lambda item: (str(item[1].get("queued_at") or ""), item[0].organization_id, item[0].key))


def _deliver(*, now, limit) -> dict:
    channel = str(getattr(settings, "CONTENT_FACTORY_OPS_SLACK_CHANNEL_ID", "") or "").strip()
    configured = bool(SLACK_CHANNEL_PATTERN.fullmatch(channel))
    outcome = {"sent": 0, "unconfigured": 0, "undelivered": 0}
    processed = 0
    for target, seen in _pending_targets():
        if processed >= limit:
            break
        lease = _timestamp(seen.get("next_attempt_at"))
        if lease and lease > now:
            continue
        settled = None
        with transaction.atomic():
            row, container, receipt = target.lock()
            if (row is None or receipt is None or receipt.get("state") != "pending"
                    or (lease := _timestamp(receipt.get("next_attempt_at"))) and lease > now):
                continue
            processed += 1
            context = _alert_context(row, receipt)
            if not configured:
                receipt.update(state="unconfigured")
                outcome["unconfigured"] += 1
                settled = receipt
            elif int(receipt.get("attempts") or 0) >= MAX_DELIVERY_ATTEMPTS:
                # An interrupted final attempt never earns a fourth send.
                receipt.update(state="failed")
                outcome["undelivered"] += 1
                settled = receipt
            else:
                attempts = int(receipt.get("attempts") or 0) + 1
                receipt.update(attempts=attempts, next_attempt_at=(now + RETRY_AFTER).isoformat())
            target.write(row, container, receipt)
        if settled is not None:
            _log_alert(logging.WARNING, settled, context, channel_configured=configured)
            _mirror_settled(settled)
            continue
        # Slack is called outside the row lock; the receipt above is the lease.
        sent, message_id = _post(channel, alert_text(receipt, context), receipt["alert_id"])
        with transaction.atomic():
            row, container, current = target.lock()
            if row is None or current is None or current.get("attempts") != attempts or current.get("state") != "pending":
                continue
            if sent:
                current.update(state="sent", sent_at=now.isoformat(), message_id=message_id, channel_id=channel)
                current.pop("next_attempt_at", None)
                outcome["sent"] += 1
            else:
                current.update(state="failed" if attempts >= MAX_DELIVERY_ATTEMPTS else "pending")
                outcome["undelivered"] += 1
            target.write(row, container, current)
        if current["state"] != "pending":
            _log_alert(logging.INFO if sent else logging.WARNING, current, context, channel_configured=True)
            _mirror_settled(current)
    return outcome


def _mirror_settled(receipt):
    """Copy a settled run-scoped receipt onto its run, if the run still exists (after the config lock)."""
    reason = receipt.get("alert_reason")
    if reason not in ALERT_REASONS:
        return
    if receipt.get("run_pk") is not None:
        run_pk = ContentFactoryRun.objects.filter(pk=receipt["run_pk"]).values_list("pk", flat=True).first()
    elif receipt.get("run_id"):
        run_pk = ContentFactoryRun.objects.filter(run_id=receipt["run_id"]).values_list("pk", flat=True).first()
    else:
        return
    if run_pk is not None:
        _mirror_run_receipt(SimpleNamespace(pk=run_pk), reason, receipt)


def _post(channel, text, alert_id):
    from integrations.services.slack import SlackService
    try:
        response = SlackService.get_client().chat_postMessage(channel=channel, text=text, client_msg_id=alert_id,
                                                              unfurl_links=False, unfurl_media=False)
        message_id = str(response.get("ts") or "")
        if not response.get("ok") or not message_id:
            raise ValueError("Ops alert delivery was not acknowledged.")
        return True, message_id
    except Exception as exc:
        logger.warning("founder_journey_alert_delivery_retry alert_id=%s error_type=%s", alert_id, type(exc).__name__)
        return False, ""


def _alert_context(row, receipt) -> dict:
    """Ids and codes only: never a founder's email, name or article text."""
    from organizations.models import Organization
    org_id = getattr(row, "organization_id", None)
    domain = Organization.objects.filter(pk=org_id).values_list("domain", flat=True).first() if org_id else ""
    context = {
        "org_id": org_id, "domain": _safe(domain), "company_id": _safe(receipt.get("company_id")),
        "run_id": _safe(receipt.get("run_id")), "workflow": "", "run_status": "", "stop_code": "",
        "step_attempts": None, "staff_link": "",
    }
    run = None
    if receipt.get("run_pk") is not None:
        run = ContentFactoryRun.objects.filter(pk=receipt["run_pk"]).first()
    elif receipt.get("run_id"):
        run = ContentFactoryRun.objects.filter(run_id=receipt["run_id"]).first()
    if run is not None:
        context.update(run_id=_safe(run.run_id), workflow=_safe(run.workflow), run_status=_safe(run.status),
                       stop_code=run_stop_code(run.result),
                       step_attempts=run.steps.aggregate(value=Max("attempts"))["value"],
                       staff_link=_admin_link("admin:workflow_runs_contentfactoryrun_change", run.pk))
    elif context["company_id"]:
        context["staff_link"] = _admin_link("admin:founder_tools_viberaisingcompany_change", context["company_id"])
    elif context["org_id"]:
        context["staff_link"] = _admin_link("admin:organizations_organization_change", context["org_id"])
    return context


def run_stop_code(result) -> str:
    """The run's stop code, if it reported one in a safe identifier shape."""
    result = result if isinstance(result, dict) else {}
    failure = result.get("failure") if isinstance(result.get("failure"), dict) else {}
    denied = result.get("authority_denied") if isinstance(result.get("authority_denied"), dict) else {}
    for value in (result.get("error_code"), result.get("failure_code"), failure.get("code"), denied.get("code")):
        if isinstance(value, str) and SAFE_CODE.fullmatch(value.strip()):
            return value.strip()
    return ""


def alert_text(receipt, context) -> str:
    """The Slack message. The error code lives only inside this string (IF-23)."""
    reason = receipt.get("alert_reason") or ""
    parts = [f"Founder journey alert: {REASON_LABELS.get(reason, reason)} ({reason})."]
    parts.append(f"Org {context['org_id']}" + (f" ({context['domain']})." if context["domain"] else "."))
    if context["company_id"]:
        parts.append(f"Company {context['company_id']}.")
    if context["run_id"]:
        parts.append(f"Run {context['run_id']}. Workflow {context['workflow'] or 'unknown'}. Status {context['run_status'] or 'unknown'}.")
        parts.append(f"Code {context['stop_code'] or 'none reported'}.")
        if context["step_attempts"] is not None:
            parts.append(f"Step attempts {context['step_attempts']}.")
    details = receipt.get("details") if isinstance(receipt.get("details"), dict) else {}
    if details:
        parts.append("Details " + ", ".join(f"{key}={_safe(value)}" for key, value in sorted(details.items())) + ".")
    parts.append(f"Alert delivery attempt {receipt.get('attempts') or 0} of {MAX_DELIVERY_ATTEMPTS}.")
    if context["staff_link"]:
        parts.append(f"Staff link: {context['staff_link']}")
    return " ".join(parts)


def _log_alert(level, receipt, context, *, channel_configured):
    logger.log(
        level,
        "founder_journey_alert alert_id=%s alert_reason=%s state=%s org_id=%s company_id=%s run_id=%s workflow=%s "
        "code=%s attempts=%s channel_configured=%s staff_link=%s",
        receipt.get("alert_id"), receipt.get("alert_reason"), receipt.get("state"), context["org_id"],
        context["company_id"] or "-", context["run_id"] or "-", context["workflow"] or "-",
        context["stop_code"] or "-", receipt.get("attempts") or 0, channel_configured, context["staff_link"] or "-",
        extra={
            "alert_id": receipt.get("alert_id"), "alert_reason": receipt.get("alert_reason"),
            "alert_state": receipt.get("state"), "org_id": context["org_id"],
            "company_id": context["company_id"] or None, "run_id": context["run_id"] or None,
            "workflow": context["workflow"] or None, "attempts": receipt.get("attempts") or 0,
            "channel_configured": channel_configured,
        },
    )


def _admin_link(name, pk) -> str:
    try:
        path = reverse(name, args=[pk])
    except NoReverseMatch:
        return ""
    return f"{str(getattr(settings, 'DEFAULT_BACKEND_URL', '') or '').rstrip('/')}{path}"


def _safe(value) -> str:
    text = str(value or "").strip()
    return text if SAFE_TOKEN.fullmatch(text) else re.sub(r"[^A-Za-z0-9_.:\-]", "_", text)[:100]


def _timestamp(value):
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if timezone.is_aware(parsed) else timezone.make_aware(parsed, dt_timezone.utc)
