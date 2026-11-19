"""An article run the system stopped is restarted or announced once (plan step B24).

A connection or operation transition (disconnect, reconnect, reset, revoke,
purge, or a fenced callback for an operation that a transition closed) stops
an article run although the founder asked for nothing. Before this the run was
just cancelled: the drafts list hides cancelled runs, so the founder saw the
article vanish (Tala Thrive: six ``confirmed_topic`` runs, 3 to 17 Nov 2026).

Each such run now gets exactly one follow-up, recorded under
``result["system_stop_followup"]``, claimed under the run's lock before any
restart so two passes can never both restart one run:

- ``restarted``: the website is connected and consent is unchanged (same
  repository id and the same consenting owner, and the stop was not a consent
  withdrawal), so the run is restarted once against the current generation
  through the founder's own Restart path, which reuses the original payment
  and never charges again.
- ``notice``: anything else. The run's draft card stays in the founder's list
  with one notice and a Restart (or, when its points were refunded, Start
  again) action.

A founder's own cancel (``cancelled_by`` owner, an owner cancellation reason
or the legacy "Cancelled by user." stop) gives nothing.

``website_operation_terminal`` is decided per raise site
(``TERMINAL_RAISE_SITES``), never by its code alone: only the two sites where
a worker's late report met an operation that was already closed are a system
stop; the three that refuse a founder's own request change no run. A stop
recorded before raise sites were stamped is left alone (staff see it through
the connection invariant sweep) until the A1 trace confirms which site fired.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

logger = logging.getLogger(__name__)

FOLLOWUP_KEY = "system_stop_followup"
LOOKBACK = timedelta(days=30)
# Only a recent stop restarts on its own; an older one gets the notice instead.
RESTART_WINDOW = timedelta(hours=48)
SWEEP_LIMIT = 25

# Where ``website_operation_terminal`` is raised, and what each site means.
TERMINAL_RAISE_SITES = {
    # A worker report for an operation an earlier outcome or a transition
    # already closed: a stale-operation protocol fault, not the founder.
    "website_operations.validate_operation": "stale_operation",
    # The delivery receiver acks this as terminal (ack_terminal): stale too.
    "setup_delivery_receiver.check_current_scope": "stale_operation",
    # Refusals of the founder's own cancel or resume request: no run changed.
    "website_operations.cancel_operation_finished": "founder_request_refused",
    "website_operations.cancel_operation_worker_finished": "founder_request_refused",
    "website_operations.resume_completed": "founder_request_refused",
}
STALE_TERMINAL_SITES = frozenset(site for site, meaning in TERMINAL_RAISE_SITES.items() if meaning == "stale_operation")

# Fences a connection or operation transition raises at a run's callback.
TRANSITION_FENCE_CODES = frozenset({
    "website_connection_changed", "website_disconnected", "website_operation_cancelled",
    "website_operation_changed", "website_run_changed", "website_publishing_paused", "authority_revoked",
})
# Transitions that withdraw consent: a run they stopped is never restarted on its own.
CONSENT_WITHDRAWALS = frozenset({"disconnect", "revoke", "purge"})
TRANSITION_ACTIONS = ("pause", "disconnect", "reconnect", "reset", "revoke", "purge")
OWNER_REASONS = frozenset({"owner_cancelled", "draft_deleted", "article_discarded"})
LEGACY_OWNER_MESSAGE = "Cancelled by user."
SYSTEM_TRANSITION_ERROR = "Website connection changed."
STOPPED_STATUSES = ("cancelled", "failed", "blocked", "denied")

NOTICE_MESSAGE = "We stopped this article when your website connection changed. Restart it when you're ready; it won't be charged again."
REFUNDED_NOTICE_MESSAGE = "We stopped this article when your website connection changed and refunded its points. Start it again from your topics."


def raise_site(exc, site):
    """Stamp where an authority error was raised; returns ``exc`` for ``raise raise_site(...)``."""
    exc.raise_site = site
    return exc


def _mapping(value):
    return value if isinstance(value, dict) else {}


def founder_cancelled(run) -> bool:
    """Whether the founder stopped this run themselves."""
    result = _mapping(run.result)
    if result.get("cancelled_by") == "owner" or result.get("cancellation_reason") in OWNER_REASONS:
        return True
    return run.status == "cancelled" and str(run.error or "").strip() == LEGACY_OWNER_MESSAGE


def _transition_for(run):
    """The connection transition whose cancellation list names this run, if any."""
    from .website_models import WebsiteConnectionOperation
    saved = _mapping(run.run_request)
    connection_id = saved.get("website_connection_id")
    if not connection_id:
        return None
    from .website_cancellation_retirement import KEY as CANCEL_WORK_KEY
    rows = (WebsiteConnectionOperation.objects.filter(connection_id=connection_id, action__in=TRANSITION_ACTIONS)
        .order_by("-created_at").values_list("action", "payload", "receipt")[:20])
    for action, payload, receipt in rows:
        # Cancellation retirement moves an unconfirmed id out of the payload,
        # so the receipt's claimed list counts too.
        named = [*(_mapping(payload).get("cancel_run_ids") or []),
            *(_mapping(_mapping(receipt).get(CANCEL_WORK_KEY)).get("run_ids") or []),
            *(_mapping(receipt).get("unconfirmed_cancel_run_ids") or [])]
        if run.run_id in named:
            return action
    return None


def stop_cause(run):
    """``{"kind": ..., "transition": ..., "stop_code": ...}`` for a system stop, else None."""
    from .run_state import ARTICLE_WORKFLOWS
    if run.workflow not in ARTICLE_WORKFLOWS or run.status not in STOPPED_STATUSES or founder_cancelled(run):
        return None
    result = _mapping(run.result)
    if run.status == "cancelled":
        transition = result.get("cancellation_transition")
        legacy = str(run.error or "") == SYSTEM_TRANSITION_ERROR and (
            result.get("cancelled_by") == "system" or "cancellation_reason" not in result)
        if not transition:
            # A run cancelled before transitions were recorded (or an
            # authoritative run cancelled by the operation itself) is matched
            # to the transition whose cancellation list names it. One that
            # cannot be matched stays "unknown" and is never restarted on its
            # own: it may have been a consent withdrawal.
            transition = _transition_for(run) or ("unknown" if legacy else None)
        if transition:
            return {"kind": "transition", "transition": transition, "stop_code": "authority_revoked"}
        return None
    rejection = _mapping(result.get("callback_rejection"))
    code = str(rejection.get("code") or _mapping(result.get("failure")).get("callback_denial_code")
        or result.get("error_code") or "")
    if code in TRANSITION_FENCE_CODES:
        return {"kind": "fence", "transition": "pause" if code == "website_publishing_paused" else "operation", "stop_code": code}
    if code == "website_operation_terminal":
        site = str(rejection.get("raise_site") or "")
        if not site:
            # The worker reported the fence; the refusal that met its callback names the site.
            refusals = [item for item in result.get("authority_refusals") or [] if isinstance(item, dict)
                and item.get("code") == code and item.get("raise_site")]
            site = str(refusals[-1]["raise_site"]) if refusals else ""
        if site in STALE_TERMINAL_SITES:
            return {"kind": "fence", "transition": "operation", "stop_code": code, "raise_site": site}
    return None


def _owner_context(run, connection):
    """The founder context of the consenting owner, when consent is unchanged for this run."""
    from founder_tools.models import VibeRaisingCompany
    from founder_tools.services import get_founder_company_context
    from .vibe_marketing_views import founder_actor_id_for_user
    owner = connection.authorized_by
    if owner is None or not owner.is_active:
        return None
    saved = _mapping(run.run_request)
    actors = {str(value) for value in (run.slack_user_id, saved.get("requested_by_slack_user_id"), saved.get("slack_user_id")) if value}
    if founder_actor_id_for_user(owner) not in actors:
        return None
    company = VibeRaisingCompany.objects.filter(organization_id=connection.organization_id, profile__user_id=owner.pk).first()
    if company is None:
        return None
    try:
        return get_founder_company_context(owner, company_id=company.pk)
    except Exception:
        return None


def _consent_withdrawn_since(run, connection) -> bool:
    """Whether a disconnect, revoke or purge happened on this connection after the run's own generation."""
    saved = _mapping(run.run_request)
    generation = _mapping(run.result).get("cancellation_generation", saved.get("connection_generation"))
    if type(generation) is not int:
        return True  # Unknown: never assume consent survived.
    return connection.operations.filter(action__in=CONSENT_WITHDRAWALS, generation__gt=generation).exists()


def decide(run, cause, connection, *, now=None):
    """``"restart"`` when the connection is connected and consent unchanged, else ``"notice"``."""
    if (connection is None or connection.state != "connected" or cause["transition"] in CONSENT_WITHDRAWALS
            or cause["transition"] == "unknown"):
        return "notice"
    if run.updated_at and run.updated_at < (now or timezone.now()) - RESTART_WINDOW:
        # An old stop is announced, not silently restarted weeks later.
        return "notice"
    saved = _mapping(run.run_request)
    if saved.get("repository_id") not in (None, connection.repository_id):
        return "notice"
    if _consent_withdrawn_since(run, connection):
        return "notice"
    from .article_publication_evidence import refund_owns_charge
    if refund_owns_charge(run):
        return "notice"
    return "restart"


def _record(run, followup) -> bool:
    """Write the one follow-up under the run's lock; False when another pass already did."""
    from workflow_runs.models import ContentFactoryRun
    with transaction.atomic():
        current = ContentFactoryRun.objects.select_for_update().filter(pk=run.pk).first()
        if current is None or FOLLOWUP_KEY in _mapping(current.result):
            return False
        current.result = {**_mapping(current.result), FOLLOWUP_KEY: followup}
        from .website_run_authority import event_owned
        current.save(update_fields=["result"] if event_owned(current) else ["result", "updated_at"])
    run.result = current.result
    return True


def _replace(run, followup) -> None:
    """Record the outcome of the restart this pass claimed."""
    from workflow_runs.models import ContentFactoryRun
    with transaction.atomic():
        current = ContentFactoryRun.objects.select_for_update().filter(pk=run.pk).first()
        if current is None:
            return
        current.result = {**_mapping(current.result), FOLLOWUP_KEY: followup}
        from .website_run_authority import event_owned
        current.save(update_fields=["result"] if event_owned(current) else ["result", "updated_at"])
    run.result = current.result


def notice_for(run):
    """The founder-facing notice of a system-stopped run, or None (founder cancels never have one)."""
    followup = _mapping(_mapping(run.result).get(FOLLOWUP_KEY))
    if followup.get("decision") != "notice" or _mapping(run.result).get("restart_child_run_id"):
        return None  # Once the founder restarted it, the replacement carries the card.
    from .article_publication_evidence import refund_owns_charge
    refunded = refund_owns_charge(run)
    action = {"id": "start_again", "label": "Start again"} if refunded else {"id": "restart", "label": "Restart"}
    return {"kind": "system_stopped", "message": REFUNDED_NOTICE_MESSAGE if refunded else NOTICE_MESSAGE, "action": action}


def already_restarted(run) -> bool:
    """A replacement already took (or is taking) this run's payment, or its follow-up restarted it."""
    from .article_publication_evidence import restart_reused_charge
    followup = _mapping(_mapping(run.result).get(FOLLOWUP_KEY))
    return restart_reused_charge(run) or followup.get("decision") in {"restarted", "restarting"}


def restartable(run) -> bool:
    """Whether the founder's Restart may take this cancelled run: only a system stop, never their own cancel."""
    return run.status == "cancelled" and stop_cause(run) is not None and not already_restarted(run)


def follow_up(run, *, now=None) -> str | None:
    """Give one system-stopped run its one follow-up; returns the decision, or None."""
    from .models import OrganizationContentConfig
    now = now or timezone.now()
    if FOLLOWUP_KEY in _mapping(run.result):
        return None
    cause = stop_cause(run)
    if cause is None:
        return None
    if already_restarted(run):
        return None  # The founder already restarted it; a second replacement would reuse one payment twice.
    config = OrganizationContentConfig.objects.select_related("website_connection__authorized_by").filter(
        organization_id=run.organization_id).first()
    connection = getattr(config, "website_connection", None) if config else None
    if connection is not None and connection.state == "paused":
        return None  # Decided after resume (B20 keeps the run's consent through a pause).
    decision = decide(run, cause, connection, now=now)
    followup = {"decision": decision, "at": now.isoformat(), "cause": cause,
        "connection_generation": getattr(connection, "generation", None)}
    context = _owner_context(run, connection) if decision == "restart" else None
    if context is None:
        followup["decision"] = "notice"
        if not _record(run, followup):
            return None
    else:
        # Claim the run before restarting, so two passes can never both restart
        # it. A pass that dies mid-restart leaves the notice (with Restart), never
        # a second replacement.
        if not _record(run, {**followup, "decision": "notice", "restart_attempted": True}):
            return None
        restarted = _restart(run, context)
        if restarted is not None:
            followup.update(decision="restarted", restart_run_id=restarted)
            _replace(run, followup)
        else:
            followup["decision"] = "notice"
    logger.info("system_stopped_run_followup run=%s decision=%s transition=%s code=%s",
        run.run_id, followup["decision"], cause["transition"], cause["stop_code"])
    return followup["decision"]


def _restart(run, context):
    """Restart through the founder's own Restart path (no new charge); the new run id, or None."""
    from .vibe_marketing_views import _restart_article_run
    try:
        restarted, error = _restart_article_run(run=run, context=context)
    except Exception:
        logger.exception("system_stopped_run_restart_failed run=%s", run.run_id)
        return None
    if error is not None or restarted is None:
        return None
    return restarted.run_id


def run_system_stopped_run_sweep(now=None, limit=SWEEP_LIMIT) -> dict:
    """Follow up recently system-stopped article runs; one bounded pass per scheduler tick."""
    from workflow_runs.models import ContentFactoryRun
    from .run_state import ARTICLE_WORKFLOWS
    from django.db.models import Q
    now = now or timezone.now()
    codes = sorted(TRANSITION_FENCE_CODES | {"website_operation_terminal"})
    # Only rows that can be a system stop, so ordinary failures and the
    # founder's own cancels can never crowd real stops out of the window.
    candidates = (Q(status="cancelled") & ~Q(error=LEGACY_OWNER_MESSAGE)
        | Q(result__error_code__in=codes) | Q(result__callback_rejection__code__in=codes)
        | Q(result__failure__callback_denial_code__in=codes))
    rows = (ContentFactoryRun.objects.filter(workflow__in=ARTICLE_WORKFLOWS, status__in=STOPPED_STATUSES,
            updated_at__gte=now - LOOKBACK, organization__isnull=False).filter(candidates)
        .exclude(result__has_key=FOLLOWUP_KEY)
        # has_key first: a JSON key comparison on a row without the key is
        # NULL, and excluding NULL would drop every row that lacks it.
        .exclude(Q(result__has_key="cancelled_by") & Q(result__cancelled_by="owner"))
        .exclude(Q(result__has_key="cancellation_reason") & Q(result__cancellation_reason__in=sorted(OWNER_REASONS)))
        .order_by("-updated_at"))
    examined, decided = 0, {}
    for run in rows[:max(1, limit) * 4]:
        if sum(decided.values()) >= limit:
            break
        examined += 1
        try:
            decision = follow_up(run, now=now)
        except Exception:
            logger.exception("system_stopped_run_followup_failed run=%s", run.run_id)
            continue
        if decision:
            decided[decision] = decided.get(decision, 0) + 1
    return {"status": "completed", "examined": examined, "decided": decided}
