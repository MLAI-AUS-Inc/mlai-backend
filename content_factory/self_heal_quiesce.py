"""Quiesce self-heal history that should never have been swept (A16).

Orgs 175 to 193 (Nov 2026): 155 old nightly ``island_refresh`` failures were
swept by self-heal, recoded to ``SELF_HEAL_DEADLINE_REACHED`` or
``SELF_HEAL_ATTEMPTS_EXHAUSTED`` and alerted. A2 stops that for new sweeps; this
cleans up what is already stored:

- Scheduler runs (``is_scheduler_run``, old unstamped ones included) are stamped
  ``self_heal_v1.last_action = "none"`` so no sweep picks them up again.
- On those runs the engine's recode is undone: ``error_code`` gets back the code
  the engine read before it stopped (``self_heal_v1.code``, else the newest
  history entry) and the engine's stop is recorded beside it under
  ``self_heal_v1.stop``. A run whose original code cannot be recovered keeps its
  recode and is reported.
- Founder runs created before the history horizon (default: the founder-journey
  alert start date) that self-heal never touched are stamped the same way, so a
  newly enabled sweep never starts on old history.
- Pending founder-journey receipts for scheduler runs are counted, not changed:
  delivery is bounded per tick, and A2 already stops new ones.

Writes take the run row lock and use ``QuerySet.update``, so ``updated_at`` (the
worker's stall clock) never moves. Running it twice changes nothing the second
time.
"""
from __future__ import annotations

from datetime import datetime

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from workflow_runs.models import ContentFactoryRun
from .founder_journey_alerts import RECEIPTS_KEY, SAFE_CODE, alerts_since, founder_run_q, is_scheduler_run
from .website_goal import mapping

CANDIDATE_STATUSES = ("failed", "blocked", "precondition_failed", "preview_failed")
RECODES = {"SELF_HEAL_DEADLINE_REACHED": "wall", "SELF_HEAL_ATTEMPTS_EXHAUSTED": "attempts"}
NOT_ORIGINAL = frozenset({*RECODES, "pull_request_daily_cap"})
MARKER = "quiesced_v1"


def _original_code(receipt) -> str:
    candidates = [receipt.get("code")] + [row.get("code") for row in reversed(receipt.get("history") or [])
                                          if isinstance(row, dict)]
    for value in candidates:
        if isinstance(value, str) and SAFE_CODE.fullmatch(value) and value not in NOT_ORIGINAL:
            return value
    return ""


def plan_run(run) -> dict | None:
    """The change this run needs, or None when it needs none (pure, so a rerun plans nothing)."""
    result = mapping(run.result)
    receipt = mapping(result.get("self_heal_v1"))
    scheduler = is_scheduler_run(run)
    if not scheduler and receipt:
        return None  # Self-heal already owns this founder run; history only covers untouched rows.
    stamp = receipt.get("last_action") not in {"stop", "none"}
    recode = result.get("error_code") if scheduler and result.get("error_code") in RECODES else None
    original = _original_code(receipt) if recode else ""
    flagged = mapping(receipt.get(MARKER)).get("unrestorable") == recode
    if not stamp and not (recode and (original or not flagged)):
        return None
    return {"run_pk": run.pk, "run_id": run.run_id, "organization_id": run.organization_id,
            "workflow": run.workflow, "audience": "scheduler" if scheduler else "founder_history",
            "stamp": stamp, "restore": {"from": recode, "to": original} if original else None,
            "unrestorable": recode if recode and not original else None}


def _apply(run_pk, *, now) -> dict | None:
    with transaction.atomic():
        current = ContentFactoryRun.objects.select_for_update(of=("self",)).filter(pk=run_pk).first()
        if current is None:
            return None
        planned = plan_run(current)
        if planned is None:
            return None
        result = dict(mapping(current.result))
        receipt = dict(mapping(result.get("self_heal_v1")))
        marker = dict(mapping(receipt.get(MARKER)))
        if planned["stamp"]:
            if receipt.get("last_action") != "stop":
                receipt["last_action"] = "none"
            receipt.update(version=1, next_at=None)
            receipt.setdefault("mode", "quiesced")
            marker.update(at=now.isoformat(), audience=planned["audience"])
        if planned["restore"]:
            recode = planned["restore"]["from"]
            receipt["stop"] = {"code": recode, "cap": RECODES[recode],
                               "at": receipt.get("last_action_at"), "restored_at": now.isoformat()}
            result["error_code"] = planned["restore"]["to"]
            marker["restored_code"] = planned["restore"]["to"]
        if planned["unrestorable"]:
            marker["unrestorable"] = planned["unrestorable"]
        receipt[MARKER] = marker
        result["self_heal_v1"] = receipt
        # QuerySet.update: the worker's updated_at stall clock stays where it was.
        ContentFactoryRun.objects.filter(pk=current.pk).update(result=result)
        return planned


def _pending_scheduler_receipts(scheduler_pks) -> int:
    from .models import OrganizationContentConfig
    if not scheduler_pks:
        return 0
    count = 0
    for strategy in (OrganizationContentConfig.objects.filter(pillar_strategy__has_key=RECEIPTS_KEY)
                     .values_list("pillar_strategy", flat=True).iterator(chunk_size=200)):
        for receipt in mapping(mapping(strategy).get(RECEIPTS_KEY)).values():
            if isinstance(receipt, dict) and receipt.get("state") == "pending" and receipt.get("run_pk") in scheduler_pks:
                count += 1
    return count


def quiesce_self_heal_history(*, apply=False, before: datetime | None = None, organization_id=None,
                              limit=None, now=None) -> dict:
    """Report (default) or apply the quiesce plan; see the module docstring."""
    now = now or timezone.now()
    before = before or alerts_since()
    rows = ContentFactoryRun.objects.filter(organization__isnull=False, status__in=CANDIDATE_STATUSES)
    if organization_id is not None:
        rows = rows.filter(organization_id=organization_id)
    scheduler_rows = rows.exclude(founder_run_q())
    history_rows = rows.filter(founder_run_q(), created_at__lt=before).filter(
        Q(result__self_heal_v1__isnull=True))
    ids = list(scheduler_rows.order_by("pk").values_list("pk", flat=True))
    scheduler_pks = set(ids)
    ids += [pk for pk in history_rows.order_by("pk").values_list("pk", flat=True) if pk not in scheduler_pks]
    report = {"mode": "apply" if apply else "dry_run", "before": before.isoformat(), "checked": len(ids),
              "stamped": 0, "restored": 0, "unrestorable": 0, "rows": []}
    for pk in ids:
        if limit is not None and len(report["rows"]) >= limit:
            break
        if apply:
            planned = _apply(pk, now=now)
        else:
            run = ContentFactoryRun.objects.filter(pk=pk).only(
                "pk", "run_id", "organization_id", "workflow", "run_request", "result").first()
            planned = plan_run(run) if run is not None else None
        if planned is None:
            continue
        report["rows"].append(planned)
        report["stamped"] += bool(planned["stamp"])
        report["restored"] += bool(planned["restore"])
        report["unrestorable"] += bool(planned["unrestorable"])
    report["pending_scheduler_receipts"] = _pending_scheduler_receipts(scheduler_pks)
    return report
