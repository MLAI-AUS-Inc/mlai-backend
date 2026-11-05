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

P2 (IF-P2-19, IF-P2-23) adds the reasons in ``P2_REASON_LABELS``, a staff
acknowledgement (``acknowledge_founder_journey_alert``), the acknowledgement due
time ``details["ack_due_at"]`` (one Melbourne business day after queueing) and a
seam through which be-staff's ``staff_alert_rules.enrich_alert_details`` adds ids
to a receipt's details. Details still hold ids only; the staff console resolves
names, emails and links at read time.
"""
from __future__ import annotations

from functools import lru_cache
import importlib
import json
import logging
import re
from datetime import date, datetime, time, timedelta, timezone as dt_timezone
from types import SimpleNamespace
from uuid import NAMESPACE_URL, uuid5
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db import connection, transaction
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
# IF-P2-23: the P2 reasons, each queued by the package the contract names.
P2_REASON_LABELS = {
    "hosting_retired": "Move retired blog URLs to the startup website; see the site retirement receipt",
    "article_safety_review_required": "An article needs staff review of safety guidance",
    "article_quality_review_required": "An article's automatic quality repair needs staff review",
    "our_team_item": "A startup has an item no automatic path can finish",
    "unregistered_failure_code": "A run reported a failure code the registry does not know",
    "codeless_terminal": "A run stopped without a failure code",
    "setup_attempts_spike": "A startup had more than 6 setup attempts today",
    "queue_age": "A queued run has waited more than 10 minutes",
    "provider_error_rate": "Provider errors passed 20% of active runs",
    "throttle_spike": "Founders hit more than 5 rate limits in an hour",
    "refresh_failure_spike": "Sign-in refresh failures passed 3 times the weekly baseline",
    "github_callback_failures": "GitHub callback failures spiked",
    "free_budget_exhausted": "A free article ran out of model budget",
    "claim_findings_delivered": "A draft was delivered with claims to check",
    "repair_loop_exhausted": "A preview repair loop ran out of attempts",
    "pull_request_cap": "A startup reached 3 MLAI pull requests today",
    "ladder_exhausted": "Every blog route failed for a startup's website",
    "self_heal_capped": "Self-healing stopped retrying an item",
    "source_change_repeated": "A run's website source changed a third time",
    "deploy_not_detected": "A merged article was not deployed within 30 minutes",
    "merge_rejected_twice": "GitHub rejected an MLAI merge twice",
    "join_request_unanswered": "A team join request has waited 72 hours",
}
P2_ALERT_REASONS = frozenset(P2_REASON_LABELS)
ALERT_REASONS = ALERT_REASONS | P2_ALERT_REASONS
REASON_LABELS = {**REASON_LABELS, **P2_REASON_LABELS}
ACK_TIMEZONE = ZoneInfo("Australia/Melbourne")
ENRICH_MODULE = "content_factory.staff_alert_rules"
ENRICH_FUNCTION = "enrich_alert_details"
MAX_LAST_TRANSITIONS = 3
TRANSITION_FIELDS = ("at", "from", "to")
TERMINAL_STATUSES = frozenset({"failed", "cancelled", "denied", "stopped"})
YOUR_TURN_STATUSES = frozenset({"awaiting_approval", "approval_required", "awaiting_confirmation", "awaiting_delivery_mode"})
# IF-20 owner actions. Every other cancellation_reason, 'unspecified' included, alerts.
OWNER_CANCELLATION_REASONS = ("owner_cancelled", "draft_deleted", "article_discarded")
SCHEDULER_WORKFLOWS = ("island_refresh",)
SCHEDULER_REQUEST_SOURCE = "mlai_backend_scheduler"
# Daily research automations stamp this request-id prefix. Their topic batches
# wait on the founder by design, and the automation pauses itself after three
# unanswered days, so an unpicked batch is not a stuck founder (your_turn_72h).
AUTOMATION_RESEARCH_REQUEST_PREFIX = "automation-research:"
# Until owner cancels record cancellation_reason (BE6), this is the shape
# _cancel_local_article_run leaves: empty error, steps 'Cancelled by user.'.
LEGACY_OWNER_CANCEL_MESSAGE = "Cancelled by user."
ARTICLE_WORKFLOWS = ("article_generation", "article_revision", "confirmed_topic", "content_factory_article", "direct_generate")
DELIVERED_DRAFT_STATUSES = ("completed", "awaiting_approval")
NO_DRAFT_AFTER = timedelta(hours=24)
YOUR_TURN_AFTER = timedelta(hours=72)
MAX_DELIVERY_ATTEMPTS = 3
RETRY_AFTER = timedelta(minutes=5)
SLACK_CHANNEL_PATTERN = re.compile(r"[CGD][A-Z0-9]{8,}")
SAFE_TOKEN = re.compile(r"[A-Za-z0-9_.:\-]{1,100}")
SAFE_CODE = re.compile(r"[A-Za-z][A-Za-z0-9_.\-]{0,79}")
DETAIL_KEY = re.compile(r"[a-z][a-z0-9_]{0,40}")
# Substrings that mark a detail as personal or article content. Such details are
# dropped before they are stored, so they can never reach Slack.
PRIVATE_DETAIL_MARKERS = ("email", "name", "title", "text", "content", "body", "article", "markdown", "prompt", "phone")


class FounderJourneyAlertConfigError(ValueError):
    """ARTICLES_FOUNDER_JOURNEY_ALERTS_SINCE is not an ISO date."""


def founder_journey_alert_id(*, organization_id, alert_reason, run_id=None, company_id=None, claimant_user_id=None, slot=None) -> str:
    """Return the stable alert id for one (org, slot, reason); it is also the Slack client_msg_id."""
    if slot is not None:
        if alert_reason not in P2_ALERT_REASONS or not isinstance(slot, str) or not SAFE_TOKEN.fullmatch(slot):
            raise ValueError("An alert slot must be a safe token for a P2 reason.")
        slot = f"p2-slot-{slot}"
    elif run_id:
        slot = str(run_id)
    elif company_id:
        slot = str(company_id)
    elif alert_reason == "domain_claim_disputed" and claimant_user_id:
        slot = f"claimant-user-{claimant_user_id}"
    else:
        slot = "-"
    return str(uuid5(NAMESPACE_URL, f"mlai:founder-journey:{organization_id}:{slot}:{alert_reason}"))


def queue_founder_journey_alert(*, organization, alert_reason: str, company_id=None, run=None, details: dict | None = None, slot=None) -> str:
    """Record one pending founder-journey alert idempotently and return its alert id.

    The sweep delivers it. The receipt is stored in the org's content config
    (created when missing); with ``run`` it is also mirrored onto the run. Call
    this outside any transaction that is about to roll back, or the receipt
    rolls back with it.
    For ``domain_claim_disputed`` pass ``details={"claimant_user_id": user.pk}``
    so each claimant gets their own alert. P2 owners may pass a stable
    ``slot`` (for example ``day-2026-11-02`` in Melbourne time) for recurring
    org-level alerts; leave it absent to retain the original lifetime dedupe.
    """
    if alert_reason not in ALERT_REASONS:
        raise ValueError(f"Unknown founder-journey alert_reason: {alert_reason!r}")
    if organization is None or getattr(organization, "pk", None) is None:
        raise ValueError("A founder-journey alert needs a saved organization.")
    safe_details = _safe_details(details)
    alert_id = founder_journey_alert_id(
        organization_id=organization.pk, alert_reason=alert_reason,
        run_id=getattr(run, "run_id", None), company_id=company_id,
        claimant_user_id=safe_details.get("claimant_user_id"), slot=slot,
    )
    queued_at = timezone.now()
    receipt = {
        "alert_id": alert_id, "alert_reason": alert_reason, "state": "pending", "attempts": 0, "sent_at": None,
        "queued_at": queued_at.isoformat(),
        "company_id": str(company_id) if company_id else None,
        "run_id": str(run.run_id) if run is not None else None,
        "run_pk": run.pk if run is not None else None,
        "details": safe_details,
    }
    receipt["details"] = _p2_details(receipt, organization=organization, run=run, queued_at=queued_at)
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


def queue_founder_journey_alerts_batch(items, *, now=None) -> dict:
    """Reserve the same incident receipts in bounded SQL, preserving callback writes.

    Only reservation is batched. Delivery still reads the authoritative config
    index and uses its existing claims, redaction and provider identities.
    Config locks are released before acquiring run locks, as in the single-item
    path, and run results are re-read under those locks before any mirror write.
    """
    from .models import OrganizationContentConfig
    queued_at = now or timezone.now()
    prepared, sources = [], {}
    for item in items:
        organization, reason = item["organization"], item["alert_reason"]
        run = item.get("run")
        if reason not in ALERT_REASONS or organization is None or getattr(organization, "pk", None) is None:
            raise ValueError("An incident batch needs saved organizations and registered alert reasons.")
        if run is not None and run.organization_id != organization.pk:
            raise ValueError("An incident run must belong to its organization.")
        details = _safe_details(item.get("details"))
        company = item.get("company_id")
        identifier = founder_journey_alert_id(organization_id=organization.pk, alert_reason=reason,
            run_id=getattr(run, "run_id", None), company_id=company,
            claimant_user_id=details.get("claimant_user_id"), slot=item.get("slot"))
        receipt = {"alert_id": identifier, "alert_reason": reason, "state": "pending", "attempts": 0,
            "sent_at": None, "queued_at": queued_at.isoformat(), "company_id": str(company) if company else None,
            "run_id": str(run.run_id) if run is not None else None, "run_pk": run.pk if run is not None else None,
            "details": details}
        receipt["details"] = _p2_details(receipt, organization=organization, run=run, queued_at=queued_at)
        prepared.append((organization.pk, run, reason, identifier, receipt))
        if run is not None:
            sources[run.pk] = run
    if not prepared:
        return {"ids": [], "created": 0}
    # A settled mirror whose config entry was lost is adopted, never re-sent.
    mirrors = dict(ContentFactoryRun.objects.filter(pk__in=sources).values_list("pk", "result")) if sources else {}
    organizations = {item[0] for item in prepared}
    stored, created = [], 0
    with transaction.atomic():
        rows = {row.organization_id: row for row in OrganizationContentConfig.objects.select_for_update()
                .filter(organization_id__in=organizations).order_by("pk")}
        missing = organizations - rows.keys()
        if missing:
            OrganizationContentConfig.objects.bulk_create(
                [OrganizationContentConfig(organization_id=identifier) for identifier in sorted(missing)], ignore_conflicts=True)
            rows.update({row.organization_id: row for row in OrganizationContentConfig.objects.select_for_update()
                         .filter(organization_id__in=missing).order_by("pk")})
        changed = {}
        for organization_id, run, reason, identifier, candidate in prepared:
            row = rows[organization_id]
            strategy = dict(row.pillar_strategy) if isinstance(row.pillar_strategy, dict) else {}
            receipts = dict(_receipts_in(strategy))
            receipt = receipts.get(identifier)
            if not isinstance(receipt, dict):
                mirrored = _receipts_in(mirrors.get(run.pk)).get(reason) if run is not None else None
                receipt = {**candidate, **mirrored} if isinstance(mirrored, dict) and mirrored.get("alert_id") == identifier else candidate
                receipts[identifier] = receipt
                row.pillar_strategy = {**strategy, RECEIPTS_KEY: receipts}
                changed[row.pk] = row
                created += 1
            stored.append((run, reason, identifier, receipt))
        if changed:
            OrganizationContentConfig.objects.bulk_update(list(changed.values()), ["pillar_strategy"], batch_size=200)
    # Keep a concurrent CF result/status update. Only result.ops_alerts changes.
    if sources:
        with transaction.atomic():
            current = {row.pk: row for row in ContentFactoryRun.objects.select_for_update()
                       .filter(pk__in=sources).order_by("pk")}
            changed = {}
            for run, reason, _identifier, receipt in stored:
                if run is None or run.pk not in current:
                    continue
                row = current[run.pk]
                result = dict(row.result) if isinstance(row.result, dict) else {}
                receipts = dict(_receipts_in(result))
                if receipts.get(reason) != receipt:
                    receipts[reason] = receipt
                    row.result = {**result, RECEIPTS_KEY: receipts}
                    changed[row.pk] = row
                original = run.result if isinstance(run.result, dict) else {}
                run.result = {**original, RECEIPTS_KEY: {**_receipts_in(original), reason: receipt}}
            if changed:
                ContentFactoryRun.objects.bulk_update(list(changed.values()), ["result"], batch_size=200)
    return {"ids": [item[2] for item in stored], "created": created}


# --- P2: acknowledgement, due time and enrichment (IF-P2-19) -----------------

@lru_cache(maxsize=None)
def _optional(module: str, name: str):
    """The provider function ``module.name``, or None while that package is missing (IF-P2-24)."""
    try:
        return getattr(importlib.import_module(module), name)
    except (ImportError, AttributeError):
        return None


def ack_due_at(queued_at: datetime) -> datetime:
    """One business day after ``queued_at`` in ``Australia/Melbourne`` (Monday to Friday).

    The same wall-clock time on the next day, moved past Saturday and Sunday,
    so Friday 16:00 is due Monday 16:00. Returned in UTC.
    """
    if timezone.is_naive(queued_at):
        queued_at = timezone.make_aware(queued_at, dt_timezone.utc)
    local = queued_at.astimezone(ACK_TIMEZONE)
    day = local.date() + timedelta(days=1)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    due = datetime.combine(day, local.timetz().replace(tzinfo=None), tzinfo=ACK_TIMEZONE)
    return due.astimezone(dt_timezone.utc)


def _ack_due_applies(alert_reason) -> bool:
    """New P2 reasons always carry a due time; existing ones only once staff can act on it."""
    return (alert_reason in P2_ALERT_REASONS
            or bool(getattr(settings, "MY_STARTUP_STAFF_CONSOLE_ENABLED", False))
            or bool(getattr(settings, "ARTICLES_STAFF_ALERT_RULES_ENABLED", False)))


def _enriched(receipt, *, organization, run) -> dict:
    """be-staff's extra ids for a receipt, through the IF-P2-24 seam; {} when missing, off or broken."""
    if not getattr(settings, "ARTICLES_STAFF_ALERT_RULES_ENABLED", False):
        return {}
    enrich = _optional(ENRICH_MODULE, ENRICH_FUNCTION)
    if enrich is None:
        return {}
    try:
        extra = enrich(dict(receipt), organization=organization, run=run)
    except Exception:  # noqa: BLE001 - enrichment must never stop an alert being recorded
        logger.warning("founder_journey_alert_enrich_failed alert_reason=%s org_id=%s",
                       receipt.get("alert_reason"), getattr(organization, "pk", None), exc_info=True)
        return {}
    return extra if isinstance(extra, dict) else {}


def _p2_details(receipt, *, organization, run, queued_at) -> dict:
    """The receipt's details with the enrichment and ``ack_due_at`` added, ids only."""
    details = dict(receipt.get("details") or {})
    extra = _enriched(receipt, organization=organization, run=run)
    if extra:
        details = _safe_details({**details, **extra})
    if _ack_due_applies(receipt.get("alert_reason")):
        details["ack_due_at"] = ack_due_at(queued_at).isoformat()
    return details


def acknowledge_founder_journey_alert(organization, alert_id, *, by_user) -> dict | None:
    """Record a staff acknowledgement on one alert once, and return the stored receipt.

    Sets ``acknowledged_at`` and ``acknowledged_by_user_id`` under the config row
    lock with a queryset update, so ``updated_at`` never moves. A second call
    changes nothing and returns the receipt as it is. Returns None when the org
    has no such alert. A run-scoped receipt is mirrored onto its run afterwards.
    """
    from .models import OrganizationContentConfig
    if organization is None or getattr(organization, "pk", None) is None or not alert_id:
        return None
    user_id = getattr(by_user, "pk", None)
    with transaction.atomic():
        row = OrganizationContentConfig.objects.select_for_update().filter(organization=organization).first()
        if row is None:
            return None
        strategy = dict(row.pillar_strategy) if isinstance(row.pillar_strategy, dict) else {}
        receipts = dict(_receipts_in(strategy))
        receipt = receipts.get(str(alert_id))
        if not isinstance(receipt, dict):
            return None
        if receipt.get("acknowledged_at"):
            return dict(receipt)
        receipt = {**receipt, "acknowledged_at": timezone.now().isoformat(), "acknowledged_by_user_id": user_id}
        receipts[str(alert_id)] = receipt
        OrganizationContentConfig.objects.filter(pk=row.pk).update(pillar_strategy={**strategy, RECEIPTS_KEY: receipts})
    _mirror_settled(receipt)
    return dict(receipt)


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
    from .run_reconciliation import authoritative_run
    from .run_verified_delivery import has_verified_delivery
    drafted = ContentFactoryRun.objects.using(using).filter(organization_id__in=ids,
        workflow__in=ARTICLE_WORKFLOWS).only("organization_id", "run_id", "workflow", "domain", "github_repo", "status", "result")
    return set(written) | {run.organization_id for run in drafted
        if has_verified_delivery(run) if authoritative_run(run)} | {run.organization_id
        for run in drafted if not authoritative_run(run) and run.status in DELIVERED_DRAFT_STATUSES}


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
    if waiting and run.updated_at and not is_automation_research(run) and run.updated_at <= now - YOUR_TURN_AFTER:
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


def is_automation_research(run) -> bool:
    """A topic batch from a research automation, which waits on the founder by design."""
    request = run.run_request if isinstance(run.run_request, dict) else {}
    return str(request.get("client_request_id") or "").startswith(AUTOMATION_RESEARCH_REQUEST_PREFIX)


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
                | Q(result__has_key="action_id")))).filter(
            ~Q(run_request__has_key="client_request_id")
            | ~Q(run_request__client_request_id__startswith=AUTOMATION_RESEARCH_REQUEST_PREFIX)),
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


def _postgres_receipt_update(target, owner_id, key, receipt):
    """Atomically merge one ops receipt into the current row without its other JSON.

    UPDATE takes the same row lock and rechecks the current JSON after a
    concurrent writer commits. The config reservation still commits before
    the separate Run mirror, preserving the existing cross-table lock order.
    Only ops_alerts changes; execution fields and activity clocks are untouched.
    """
    from .models import OrganizationContentConfig
    if target == 'config':
        model, field, owner = OrganizationContentConfig, 'pillar_strategy', 'organization_id'
        predicate = "jsonb_typeof({field} -> %s::text -> %s::text) IS DISTINCT FROM 'object'"
        condition = [RECEIPTS_KEY, key]
    elif target == 'run':
        model, field, owner = ContentFactoryRun, 'result', 'id'
        predicate = "({field} -> %s::text -> %s::text) IS DISTINCT FROM %s::jsonb"
        condition = [RECEIPTS_KEY, key, json.dumps(receipt)]
    else:
        raise ValueError('Only incident config and Run metadata can be updated.')
    quote = connection.ops.quote_name
    column = quote(field)
    # Normalize malformed legacy containers just as the portable Python path
    # does, while retaining every unrelated key in a valid current object.
    document = f"CASE WHEN jsonb_typeof({column}) = 'object' THEN {column} ELSE '{{}}'::jsonb END"
    receipts = f"CASE WHEN jsonb_typeof({column} -> %s::text) = 'object' THEN {column} -> %s::text ELSE '{{}}'::jsonb END"
    sql = (f"UPDATE {quote(model._meta.db_table)} SET {column} = ({document}) || "
           f"jsonb_build_object(%s::text, ({receipts}) || jsonb_build_object(%s::text, %s::jsonb)) "
           f"WHERE {quote(owner)} = %s AND " + predicate.format(field=column) +
           f" RETURNING {column} -> %s::text -> %s::text")
    parameters = [RECEIPTS_KEY, RECEIPTS_KEY, RECEIPTS_KEY, key, json.dumps(receipt), owner_id,
                  *condition, RECEIPTS_KEY, key]
    with connection.cursor() as cursor:
        cursor.execute(sql, parameters)
        row = cursor.fetchone()
    if row is None:
        return None
    return json.loads(row[0]) if isinstance(row[0], str) else row[0]


def _queue_org_receipt(organization, alert_id, receipt) -> dict:
    """Store ``receipt`` under ``alert_id`` unless one is there; return the stored receipt."""
    from .models import OrganizationContentConfig
    if connection.vendor == 'postgresql':
        inserted = _postgres_receipt_update('config', organization.pk, alert_id, receipt)
        if inserted is not None:
            return inserted
        # Existing receipts retain their state and identity. The old locked
        # path also creates a missing config with the same uniqueness fence.
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
    if connection.vendor == 'postgresql':
        _postgres_receipt_update('run', run.pk, alert_reason, receipt)
    else:
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
    """Keep only scalar, non-personal details; anything resembling text or identity is dropped.

    Two P2 shapes are also kept: a ``*_at`` timestamp (stored as UTC ISO) and
    ``last_transitions``, at most 3 ``{"at", "from", "to"}`` entries of safe tokens.
    """
    safe = {}
    for key, value in (details if isinstance(details, dict) else {}).items():
        key = str(key)
        if not DETAIL_KEY.fullmatch(key) or any(marker in key for marker in PRIVATE_DETAIL_MARKERS):
            continue
        if isinstance(value, bool) or isinstance(value, int):
            safe[key] = value
        elif isinstance(value, str) and SAFE_TOKEN.fullmatch(value) and "@" not in value:
            safe[key] = value
        elif key.endswith("_at") and (moment := _timestamp(value)) is not None:
            safe[key] = moment.astimezone(dt_timezone.utc).isoformat()
        elif key == "last_transitions" and isinstance(value, list):
            safe[key] = _safe_transitions(value)
    return safe


def _safe_transitions(items) -> list[dict]:
    transitions = []
    for item in items[-MAX_LAST_TRANSITIONS:]:
        if not isinstance(item, dict):
            continue
        entry = {}
        for field in TRANSITION_FIELDS:
            value = item.get(field)
            if field == "at":
                moment = _timestamp(value)
                entry[field] = moment.astimezone(dt_timezone.utc).isoformat() if moment else None
            else:
                entry[field] = value if isinstance(value, str) and SAFE_TOKEN.fullmatch(value) and "@" not in value else None
        transitions.append(entry)
    return transitions


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
    # The P2 due time and transitions are for the staff console, not the Slack line.
    details = {key: value for key, value in details.items() if key not in ("ack_due_at", "last_transitions")}
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
