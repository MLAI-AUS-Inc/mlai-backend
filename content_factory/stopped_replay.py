"""Replay website preparations a fixed code stopped, through the owner's Try again (A16).

A release that fixes a stop code names the code; ``replay_stopped --code <code>``
then carries the fix to founders already stuck on it. Each operation is
re-opened with ``start_prepare(..., retry=True)``, the same call the founder's
Try again button makes, under the founder who authorised the website. Nothing
is re-opened unless all of these hold:

- it is the newest ``prepare`` operation of its connection's current
  generation, and that connection is the startup's selected, ``connected``
  website with an authorising founder (no new consent is invented);
- it is stopped (``needs_attention``, ``needs_user`` or ``stopped``) on exactly
  that code;
- its setup dispatch cap (``website_prepare_setup_cap``) is not reached and the
  startup has an automatic pull request slot left today (the self-heal PR cap);
- no event-owned child run owns it, and it is not a finished first-article goal;
- this replay key (code and ``--since``) has not replayed it before.

The default is a dry-run report. A replay records ``replay_v1`` on the
operation's receipt with ``QuerySet.update``, so the stamp itself does not move
``updated_at``; the re-open does, as the owner's Try again always has.
"""
from __future__ import annotations

from datetime import datetime

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .founder_journey_alerts import SAFE_CODE
from .website_contract import WebsiteAuthorityError
from .website_goal import mapping

STOPPED_STATES = ("needs_attention", "needs_user", "stopped")
RECEIPT_KEY = "replay_v1"


class ReplayInputError(ValueError):
    """The code or the since boundary is not usable."""


def replay_key(stop_code, since) -> str:
    return f"{stop_code}@{since.isoformat() if since else 'all'}"


def _stopped_code(op) -> str:
    receipt = mapping(op.receipt)
    return str(receipt.get("reason_code") or mapping(receipt.get("failure")).get("code") or "")


def founder_context(op) -> tuple[str, dict]:
    """Why ``op`` cannot be re-opened under its founder's consent ("" when it can), and that founder's context.

    Shared by this replay and the staff console's operation actions (plan F10):
    the newest preparation of the selected, connected website's current
    generation, with an authorising founder. No new consent is invented.
    """
    from founder_tools.models import VibeRaisingCompany
    from .models import OrganizationContentConfig
    connection = op.connection
    if op.action != "prepare":
        return "not_a_preparation", {}
    if connection.state != "connected" or op.generation != connection.generation:
        return "not_current_connected_generation", {}
    latest = connection.operations.filter(generation=connection.generation, action="prepare").order_by("-created_at", "-pk").first()
    if latest is None or latest.pk != op.pk:
        return "superseded_operation", {}
    config = OrganizationContentConfig.objects.filter(organization_id=connection.organization_id).first()
    if config is None or config.website_connection_id != connection.pk:
        return "not_selected_website", {}
    if connection.authorized_by_id is None:
        return "no_authorising_founder", {}
    company = VibeRaisingCompany.objects.filter(organization_id=connection.organization_id,
                                                profile__user_id=connection.authorized_by_id).first()
    if company is None:
        return "no_founder_company", {}
    # Keep the goal the founder chose: a first-article goal re-opened as "ready"
    # would be superseded (cancelled and refunded) instead of retried.
    target = mapping(mapping(op.receipt).get("goal")).get("target") or "ready"
    return "", {"config": config, "company": company, "user": connection.authorized_by, "target": target}


def reopen_guard(op, *, now, setup_cap_checked=True) -> str:
    """The caps and ownership that hold a re-open ("" when none does).

    ``setup_cap_checked=False`` is only for a staff restart that resets the
    setup cap under its own cap (plan F10); every other guard still holds.
    """
    from . import website_prepare_setup_cap as setup_cap
    from .failure_policy_engine import pull_request_cap_reached
    from .website_goal_runner import terminal_first_article_goal
    from .website_run_authority import owned_goal_child
    from .website_prepare_sticky_stop import budget_hold_active
    if setup_cap_checked:
        if setup_cap.reached(op) or setup_cap.HELD_KEY in mapping(op.receipt):
            return "setup_cap"
        if budget_hold_active(op.receipt, now=now):
            # Staff hold a scan-budget stop; a replay would only buy the next scan.
            return "scan_budget"
    if pull_request_cap_reached(op.connection.organization, now=now):
        return "pull_request_cap"
    if owned_goal_child(op, include_completed=False) is not None:
        return "execution_owned"
    if terminal_first_article_goal(op):
        return "finished_first_article_goal"
    return ""


def _skip_reason(op, *, stop_code, key, now) -> tuple[str, dict]:
    """Why ``op`` must not be replayed ("" when it may), and the founder context it would use."""
    if op.action != "prepare" or op.state not in STOPPED_STATES or _stopped_code(op) != stop_code:
        return "not_stopped_on_code", {}
    reason, founder = founder_context(op)
    if reason:
        return reason, {}
    if key in (mapping(mapping(op.receipt).get(RECEIPT_KEY)).get("keys") or []):
        return "already_replayed", {}
    reason = reopen_guard(op, now=now)
    return (reason, {}) if reason else ("", founder)


def _stamp(op_pk, *, stop_code, key, previous_state, now):
    from .website_models import WebsiteConnectionOperation
    with transaction.atomic():
        current = WebsiteConnectionOperation.objects.select_for_update().filter(pk=op_pk).first()
        if current is None:
            return
        receipt = dict(mapping(current.receipt))
        old = mapping(receipt.get(RECEIPT_KEY))
        receipt[RECEIPT_KEY] = {"keys": [*(old.get("keys") or []), key][-20:], "stopped_on": stop_code,
                                "from_state": previous_state, "at": now.isoformat()}
        WebsiteConnectionOperation.objects.filter(pk=current.pk).update(receipt=receipt)


def replay_stopped(*, stop_code, since: datetime | None = None, apply=False, organization_id=None, limit=50, now=None) -> dict:
    """Report (default) or replay prepare operations stopped on ``stop_code``."""
    from .website_models import WebsiteConnectionOperation
    if not isinstance(stop_code, str) or not SAFE_CODE.fullmatch(stop_code):
        raise ReplayInputError("--code must be one registered-code-shaped identifier.")
    if since is not None and timezone.is_naive(since):
        raise ReplayInputError("--since must carry a time zone.")
    if type(limit) is not int or not 1 <= limit <= 500:
        raise ReplayInputError("--limit must be from 1 to 500.")
    now = now or timezone.now()
    key = replay_key(stop_code, since)
    rows = (WebsiteConnectionOperation.objects.filter(action="prepare", state__in=STOPPED_STATES)
            .filter(Q(receipt__reason_code=stop_code) | Q(receipt__failure__code=stop_code))
            .select_related("connection__organization", "connection__authorized_by").order_by("updated_at", "pk"))
    if since is not None:
        rows = rows.filter(updated_at__gte=since)
    if organization_id is not None:
        rows = rows.filter(connection__organization_id=organization_id)
    report = {"mode": "apply" if apply else "dry_run", "stop_code": stop_code, "replay_key": key,
              "checked": 0, "replayed": 0, "eligible": 0, "skipped": {}, "rows": []}
    for op in rows[:limit]:
        report["checked"] += 1
        row = {"operation_id": str(op.pk), "organization_id": op.connection.organization_id,
               "state": op.state, "generation": op.generation}
        outcome, new_state = replay_operation(op, stop_code=stop_code, key=key, apply=apply, now=now)
        reason = outcome.split(":", 1)[1] if outcome.startswith("skipped:") else ""
        if reason and reason != "not_reopened":
            report["skipped"][reason] = report["skipped"].get(reason, 0) + 1
            report["rows"].append({**row, "outcome": outcome})
            continue
        report["eligible"] += 1
        if outcome == "replayed":
            report["replayed"] += 1
            report["rows"].append({**row, "outcome": "replayed", "new_state": new_state})
            continue
        if outcome != "would_replay":
            # Refused the way the owner's own Try again would be, or not re-opened.
            label = reason or outcome
            report["skipped"][label] = report["skipped"].get(label, 0) + 1
        report["rows"].append({**row, "outcome": outcome})
    return report


def replay_operation(op, *, stop_code, key, apply, now) -> tuple[str, str | None]:
    """Replay one stopped preparation under ``key``; returns its outcome and new state.

    The outcome is ``skipped:<reason>``, ``refused:<code>``, ``would_replay``
    (a dry run) or ``replayed``. Used by the command and by the staff console's
    "replay on the current build" (plan F10).
    """
    from .website_prepare import start_prepare
    reason, founder = _skip_reason(op, stop_code=stop_code, key=key, now=now)
    if reason:
        return f"skipped:{reason}", None
    if not apply:
        return "would_replay", None
    previous = op.state
    try:
        reopened = start_prepare(founder["config"], company_id=founder["company"].pk, user=founder["user"], retry=True,
                                 target=founder["target"])
    except WebsiteAuthorityError as exc:
        # The owner's own Try again would be refused the same way; nothing changed.
        return f"refused:{exc.code}", None
    reopened.refresh_from_db()
    if reopened.pk == op.pk and reopened.state in STOPPED_STATES and _stopped_code(reopened) == stop_code:
        return "skipped:not_reopened", None
    _stamp(reopened.pk, stop_code=stop_code, key=key, previous_state=previous, now=now)
    return "replayed", reopened.state
