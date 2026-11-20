"""Operation-scoped staff actions for a stuck website preparation (plan 7.6, F10).

The staff console's run actions (``staff_console.perform_action``) cannot touch
a preparation that stopped before any run, or whose run is long gone. These
four can, behind the existing ``MY_STARTUP_STAFF_CONSOLE_ENABLED`` gate:

- ``restart_prepare``: the founder's own Try again (``start_prepare(retry=True)``)
  under the founder who authorised the website. With ``resetAttemptCap`` it
  also clears the setup dispatch cap and the scan budget hold, and that reset
  has its own cap (:data:`CAP_RESET_LIMIT` per preparation).
- ``rescan``: "Check website again" from the server: a fresh scan of the
  selected source on the same consent generation (a stopped or finished
  preparation). The scan budget still holds.
- ``reset_setup``: Try again, then start the setup over from a fresh scan
  (the saved scan, setup run and completed steps are dropped) so a new setup
  candidate is built. The setup dispatch cap still holds.
- ``replay_current_build``: the A16 replay of this one preparation, keyed to
  the deployed build (``APP_RELEASE``), so it re-opens a stop once per build.

Inside existing consent:

- every action re-opens the founder's own preparation through the founder's
  path, under the founder who authorised the website; nothing changes the
  connection generation or invents consent;
- a staff action never approves or merges a setup pull request. Those
  requests (:data:`CONSENT_ACTIONS`) are refused and audited; the founder's
  setup approval stays the consent point;
- a preparation waiting on a founder ask (a review, a merge, an approval, a
  page choice) is the founder's turn and is refused.

Choosing the articles page for the founder, regranting a free article and
archiving a duplicate startup touch consent, billing and ownership. They are
not offered here (:data:`NOT_OFFERED`).

Each action is idempotent (the caller's ``idempotencyKey`` replays the first
answer) and audited on the operation's receipt (``staff_actions_v1``: who,
when, why, and the state before and after). No schema change.
"""
from __future__ import annotations

from copy import deepcopy
import logging
from uuid import uuid4

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .website_goal import mapping

logger = logging.getLogger(__name__)

OPERATION_ACTIONS = ("restart_prepare", "rescan", "reset_setup", "replay_current_build")
# Requests that would stand in for the founder's setup consent. Always refused.
CONSENT_ACTIONS = frozenset({"approve_setup", "approve_setup_pr", "merge_setup_pr", "merge_pr", "approve_on_github",
                             "review_setup", "approve_pr"})
# Consent, billing and ownership changes the console does not offer (plan 7.6).
NOT_OFFERED = frozenset({"choose_articles_page", "regrant_free_article", "archive_duplicate"})
# A preparation resting on one of these asks is the founder's turn.
FOUNDER_TURN_ASKS = frozenset({"review_draft", "describe_customer", "agree_terms", "authorize_github", "approve_setup",
                               "approve_on_github", "buy_points", "approve_join_request", "choose_articles_page",
                               "deploy_site", "waiting_github_owner_approval", "choose_repository",
                               "send_pr_to_reviewer", "merge_pr", "review_setup", "review_github_permissions"})
STOPPED_STATES = ("needs_attention", "needs_user", "stopped")
RESCAN_STATES = (*STOPPED_STATES, "completed")
# How many times staff may reset one preparation's setup cap. The setup cap
# (ARTICLES_PREPARE_SETUP_DISPATCH_LIMIT) bounds setup runs per attempt; this
# bounds how often staff can buy it another round.
CAP_RESET_LIMIT = 1
AUDIT_KEY = "staff_actions_v1"
CAP_RESET_KEY = "staff_cap_resets_v1"
AUDIT_LIMIT = 20
NOTE_LIMIT = 500
SETUP_PROGRESS_KEYS = ("scan_run_id", "setup_run_id", "completed_steps")


class OperationActionRefused(Exception):
    """A guard holds the action; ``reason`` is a stable code."""

    def __init__(self, reason, detail=""):
        super().__init__(reason)
        self.reason, self.detail = reason, detail or reason


def build_key(stop_code) -> str:
    """The replay key for ``stop_code`` on the deployed build: one replay per stop and build."""
    release = str(getattr(settings, "APP_RELEASE", "") or "unknown")[:40]
    return f"{stop_code}@build:{release}"


def _code(op) -> str:
    from .stopped_replay import _stopped_code
    return _stopped_code(op)


def _ask(op) -> str:
    action = mapping(op.receipt).get("user_action")
    return str(action.get("id") or "") if isinstance(action, dict) else ""


def cap_resets(op) -> list:
    values = mapping(op.receipt).get(CAP_RESET_KEY)
    return [value for value in values if isinstance(value, dict)] if isinstance(values, list) else []


def snapshot(op) -> dict:
    """The audited state of ``op``: ids, codes and counters only."""
    from . import website_prepare_setup_cap as setup_cap
    receipt, payload = mapping(op.receipt), mapping(op.payload)
    return {"state": op.state, "status": receipt.get("status"), "stopCode": _code(op) or None,
            "currentStep": receipt.get("current_step"), "ask": _ask(op) or None,
            "attempt": payload.get("attempt"), "setupDispatches": len(setup_cap.dispatched(op)),
            "setupCapHeld": isinstance(receipt.get(setup_cap.HELD_KEY), dict)}


def _refusal(op, action, *, now, reset_cap=False) -> tuple[str, dict]:
    """Why staff may not take ``action`` on ``op`` now ("" when they may), and the founder context."""
    from .stopped_replay import RECEIPT_KEY as REPLAY_KEY, founder_context, reopen_guard
    from .website_prepare_sticky_stop import budget_hold_active, scan_budget_reached
    if action not in OPERATION_ACTIONS:
        return "unknown_action", {}
    states = RESCAN_STATES if action == "rescan" else STOPPED_STATES
    if op.state not in states:
        return "not_stopped", {}
    if op.state == "needs_user" and _ask(op) in FOUNDER_TURN_ASKS:
        # The founder's turn: a review, merge, approval or choice is theirs.
        return "founder_turn", {}
    reason, founder = founder_context(op)
    if reason:
        return reason, {}
    if action == "replay_current_build":
        code = _code(op)
        if not code:
            return "no_stop_code", {}
        if build_key(code) in (mapping(mapping(op.receipt).get(REPLAY_KEY)).get("keys") or []):
            return "already_replayed", {}
    if action == "rescan":
        if budget_hold_active(op.receipt, now=now) or scan_budget_reached(op, now=now):
            return "scan_budget", {}
        reason = reopen_guard(op, now=now, setup_cap_checked=False)
        return (reason, {}) if reason else ("", founder)
    if action == "restart_prepare" and reset_cap:
        if len(cap_resets(op)) >= CAP_RESET_LIMIT:
            return "cap_reset_limit", {}
        reason = reopen_guard(op, now=now, setup_cap_checked=False)
        return (reason, {}) if reason else ("", founder)
    reason = reopen_guard(op, now=now)
    return (reason, {}) if reason else ("", founder)


def offered(op, *, now=None) -> dict:
    """The actions staff may take on ``op`` now, and why the others are held.

    The console shows only what this lists; a client never infers an action.
    """
    from . import website_prepare_setup_cap as setup_cap
    now = now or timezone.now()
    available, held = [], {}
    for action in OPERATION_ACTIONS:
        reason, _ = _refusal(op, action, now=now)
        if reason:
            held[action] = reason
        else:
            available.append(action)
    capped = setup_cap.reached(op) or isinstance(mapping(op.receipt).get(setup_cap.HELD_KEY), dict)
    from .website_prepare_sticky_stop import budget_hold_active
    cap_reset = None
    if capped or budget_hold_active(op.receipt, now=now):
        reason, _ = _refusal(op, "restart_prepare", now=now, reset_cap=True)
        cap_reset = {"available": not reason, "reason": reason or None, "used": len(cap_resets(op)),
                     "limit": CAP_RESET_LIMIT}
    return {"actions": available, "held": held, "capReset": cap_reset}


def operation_summary(op, *, now=None) -> dict:
    """The staff console's view of one preparation: state, offered actions and the audit trail."""
    trail = mapping(op.receipt).get(AUDIT_KEY)
    return {"id": str(op.pk), "generation": op.generation, **snapshot(op), **offered(op, now=now),
            "staffActions": [entry for entry in trail if isinstance(entry, dict)][-AUDIT_LIMIT:]
            if isinstance(trail, list) else []}


# --- Executing -------------------------------------------------------------------

def _restart(op, founder, *, reset_cap, now, by_user):
    from . import website_prepare_setup_cap as setup_cap
    from .website_models import WebsiteConnectionOperation
    from .website_prepare import start_prepare
    from .website_prepare_sticky_stop import BUDGET_HELD_KEY, reset_scan_budget
    with transaction.atomic():
        if reset_cap:
            current = WebsiteConnectionOperation.objects.select_for_update().get(pk=op.pk)
            receipt = dict(mapping(current.receipt))
            if len(cap_resets(current)) >= CAP_RESET_LIMIT:
                raise OperationActionRefused("cap_reset_limit")
            reset = {"at": now.isoformat(), "by_user_id": by_user.pk,
                     "setup_dispatch_run_ids": setup_cap.dispatched(current)[-10:],
                     "setup_cap_held": isinstance(receipt.get(setup_cap.HELD_KEY), dict),
                     "scan_budget_held": isinstance(receipt.get(BUDGET_HELD_KEY), dict)}
            for key in (setup_cap.RECEIPT_KEY, setup_cap.HELD_KEY):
                receipt.pop(key, None)
            current.receipt = receipt
            reset_scan_budget(current)
            current.receipt[CAP_RESET_KEY] = [*cap_resets(op), reset]
            WebsiteConnectionOperation.objects.filter(pk=current.pk).update(receipt=current.receipt)
        # The founder's own Try again, under the founder who authorised the website.
        reopened = start_prepare(founder["config"], company_id=founder["company"].pk, user=founder["user"],
                                 retry=True, target=founder["target"])
        reopened.refresh_from_db()
        if reopened.pk != op.pk or reopened.state in STOPPED_STATES:
            # Nothing re-opened: keep the cap and the earlier state.
            raise OperationActionRefused("not_reopened")
    return reopened


def _rescan(op, founder, *, now):
    from .website_models import WebsiteConnectionOperation
    from .website_prepare import _adopt_source_checks, _cancel_prepare_child, _source_recheck_values
    with transaction.atomic():
        current = WebsiteConnectionOperation.objects.select_for_update().select_related("connection").get(pk=op.pk)
        if current.state != op.state or current.generation != op.generation:
            raise OperationActionRefused("operation_changed")
        if current.state in STOPPED_STATES:
            _cancel_prepare_child(current, founder["config"])
        values = _source_recheck_values(current, message="Checking your website again.")
        WebsiteConnectionOperation.objects.filter(pk=current.pk).update(updated_at=timezone.now(), **values)
        _adopt_source_checks(current.connection, current)
    return WebsiteConnectionOperation.objects.get(pk=op.pk)


def _reset_setup(op, founder, *, now, by_user):
    from .website_models import WebsiteConnectionOperation
    with transaction.atomic():
        reopened = _restart(op, founder, reset_cap=False, now=now, by_user=by_user)
        current = WebsiteConnectionOperation.objects.select_for_update().get(pk=reopened.pk)
        receipt = {key: value for key, value in mapping(current.receipt).items() if key not in SETUP_PROGRESS_KEYS}
        receipt["current_step"] = "scan"
        payload = {**mapping(current.payload), "source_reverify": True}
        WebsiteConnectionOperation.objects.filter(pk=current.pk).update(receipt=receipt, payload=payload)
    return WebsiteConnectionOperation.objects.get(pk=op.pk)


def _replay(op, *, now):
    from .stopped_replay import replay_operation
    from .website_models import WebsiteConnectionOperation
    code = _code(op)
    outcome, _ = replay_operation(op, stop_code=code, key=build_key(code), apply=True, now=now)
    if outcome != "replayed":
        raise OperationActionRefused(outcome.split(":", 1)[-1])
    return WebsiteConnectionOperation.objects.get(pk=op.pk)


def _audit(op_pk, entry):
    """Append one audit row to the operation's receipt without moving ``updated_at``."""
    from .website_models import WebsiteConnectionOperation
    with transaction.atomic():
        current = WebsiteConnectionOperation.objects.select_for_update().filter(pk=op_pk).first()
        if current is None:
            return
        receipt = dict(mapping(current.receipt))
        trail = receipt.get(AUDIT_KEY)
        trail = [row for row in trail if isinstance(row, dict)] if isinstance(trail, list) else []
        receipt[AUDIT_KEY] = [*trail[-(AUDIT_LIMIT - 1):], entry]
        WebsiteConnectionOperation.objects.filter(pk=current.pk).update(receipt=receipt)


def _execute(op, action, *, data, by_user, now, key):
    before = snapshot(op)
    reset_cap = action == "restart_prepare" and data.get("resetAttemptCap") is True
    entry = {"action": action, "at": now.isoformat(), "by_user_id": by_user.pk, "idempotency_key": key,
             "note": str(data.get("note") or "")[:NOTE_LIMIT], "before": before,
             **({"reset_attempt_cap": True} if reset_cap else {})}
    if action in CONSENT_ACTIONS:
        response = {"status": "refused", "reason": "founder_consent_required",
                    "detail": "A staff action never approves or merges a setup pull request. The founder's "
                              "approval is the consent point."}
        _audit(op.pk, {**entry, "outcome": "refused", "reason": response["reason"], "after": before})
        return response
    reason, founder = _refusal(op, action, now=now, reset_cap=reset_cap)
    after = op
    if not reason:
        from .website_contract import WebsiteAuthorityError
        try:
            if action == "restart_prepare":
                after = _restart(op, founder, reset_cap=reset_cap, now=now, by_user=by_user)
            elif action == "rescan":
                after = _rescan(op, founder, now=now)
            elif action == "reset_setup":
                after = _reset_setup(op, founder, now=now, by_user=by_user)
            else:
                after = _replay(op, now=now)
        except OperationActionRefused as exc:
            reason = exc.reason
        except WebsiteAuthorityError as exc:
            # The founder's own Try again would be refused the same way; nothing changed.
            reason = f"refused:{exc.code}"
    if reason:
        op.refresh_from_db()
        response = {"status": "refused", "reason": reason, "detail": "Action refused by its safety policy."}
        _audit(op.pk, {**entry, "outcome": "refused", "reason": reason, "after": snapshot(op)})
        return response
    after.refresh_from_db()
    result = snapshot(after)
    _audit(op.pk, {**entry, "outcome": "done", "after": result})
    logger.info("staff_operation_action action=%s operation=%s by_user=%s from=%s to=%s", action, op.pk, by_user.pk,
                before["state"], result["state"])
    return {"status": "done", "detail": "Done.", "receipt": {"operationId": str(op.pk), "before": before,
                                                             "after": result}}


def perform_operation_action(organization_id, data, *, by_user):
    """Claim a scoped idempotency key, then take one audited operation action.

    Returns ``(status, body, replayed)`` like ``staff_console.perform_action``.
    A note (why) is required. A second call with the same key and body replays
    the first answer and changes nothing.
    """
    from founder_tools import idempotency as idem
    from django.core.cache import cache
    from django.core.exceptions import ValidationError as DjangoValidationError
    from .staff_console import require_staff
    from .website_models import WebsiteConnectionOperation
    require_staff(by_user)
    action = data.get("action")
    if action in NOT_OFFERED:
        raise ValueError("This action is not offered by the staff console.")
    if (action not in OPERATION_ACTIONS and action not in CONSENT_ACTIONS) or not isinstance(data.get("operationId"), str):
        raise ValueError("Choose an operation action and operationId.")
    if not str(data.get("note") or "").strip():
        raise ValueError("Say why in a note.")
    if "resetAttemptCap" in data and (action != "restart_prepare" or not isinstance(data["resetAttemptCap"], bool)):
        raise ValueError("resetAttemptCap applies to restart_prepare only.")
    scope = ("staff_operation_action", organization_id, by_user.pk)
    key = data.get("idempotencyKey")
    digest = idem.request_sha256(data)
    state, previous = idem.begin(scope, key, digest)
    if state == idem.REPLAY:
        return previous["status"], previous["body"], True
    if state != idem.NEW:
        return 409, {"status": "refused", "detail": idem.CONFLICT_DETAIL, "reason": idem.CONFLICT_REASON}, False
    try:
        try:
            op = (WebsiteConnectionOperation.objects.select_related("connection__organization", "connection__authorized_by")
                  .get(pk=data["operationId"], connection__organization_id=organization_id))
        except (DjangoValidationError, ValueError) as exc:
            raise WebsiteConnectionOperation.DoesNotExist("Unknown operation.") from exc
        lock, token = f"staff-action:operation:{op.pk}", uuid4().hex
        if not cache.add(lock, token, 300):
            idem.abandon(scope, key, digest)
            return 409, {"status": "refused", "detail": idem.CONFLICT_DETAIL, "reason": idem.CONFLICT_REASON}, False
        try:
            response = _execute(op, action, data=deepcopy(data), by_user=by_user, now=timezone.now(), key=key)
        finally:
            if cache.get(lock) == token:
                cache.delete(lock)
        idem.finish(scope, key, digest, status=200, body=response)
        return 200, response, False
    except Exception:
        idem.abandon(scope, key, digest)
        raise
