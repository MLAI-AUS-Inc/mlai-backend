"""Ops Slack alerts for an article held for staff (plan workstream 5).

When Content Factory's final check stops and names staff as the owner of the
hold (``hold_owner == "staff"``), ops are told once, with the run, the startup
and the codes of the top three findings, and again if the run is still held 24
hours later. Delivery reuses the founder-journey alert receipts and their
sender, so the message carries ids and codes only: the claim text stays in the
staff console and ``manage.py article_quality``.

Off unless ``ARTICLES_STAFF_HOLD_ALERTS_ENABLED`` is true. With it off nothing
is queued or sent.
"""
from __future__ import annotations

from datetime import timedelta
import hashlib
import logging
import re

from django.conf import settings
from django.utils import timezone

from workflow_runs.models import ContentFactoryRun

logger = logging.getLogger(__name__)

HELD_REASON = "article_staff_hold"
STILL_HELD_REASON = "article_staff_hold_24h"
STILL_HELD_AFTER = timedelta(hours=24)
TOP_FINDINGS = 3
_CODE = re.compile(r"[A-Za-z][A-Za-z0-9_.\-]{0,79}")


def staff_hold_alerts_enabled() -> bool:
    """The rollout switch (default off)."""
    return bool(getattr(settings, "ARTICLES_STAFF_HOLD_ALERTS_ENABLED", False))


def _mapping(value):
    return value if isinstance(value, dict) else {}


def _quality(run) -> dict:
    return _mapping(_mapping(getattr(run, "result", None)).get("article_preview_quality"))


def staff_held(run) -> bool:
    """An article run whose exhausted final check is waiting on staff."""
    from .founder_journey_alerts import ARTICLE_WORKFLOWS, is_scheduler_run
    from .hosted_quality_state import hold_owner
    return bool(getattr(run, "workflow", None) in ARTICLE_WORKFLOWS and getattr(run, "organization_id", None)
                and hold_owner(_quality(run)) == "staff" and not is_scheduler_run(run))


def hold_slot(quality) -> str:
    """One stable slot per hold: the same findings on the same inputs alert once."""
    basis = str(quality.get("self_heal_fingerprint") or quality.get("inputs_sha256") or quality.get("finished_at") or "")
    return "hold-" + hashlib.sha256(basis.encode()).hexdigest()[:16]


def top_finding_codes(quality, limit=TOP_FINDINGS) -> list[str]:
    """The codes of the most important findings, blocking first; codes only, never claim text."""
    codes = []
    detail = quality.get("findings_detail") if isinstance(quality.get("findings_detail"), list) else []
    ranked = sorted((item for item in detail if isinstance(item, dict)),
                    key=lambda item: {"blocking": 0, "advisory": 1}.get(item.get("severity"), 2))
    for item in ranked:
        codes.append(str(item.get("code") or ""))
    for finding in quality.get("findings") or []:
        codes.append(str(finding).split(":", 1)[0])
    if not codes:
        visible = _mapping(quality.get("visible_content_acceptance"))
        codes += [str(visible.get("code") or ""), str(quality.get("blocker_code") or "")]
    return [code for code in dict.fromkeys(codes) if _CODE.fullmatch(code)][:limit]


def _details(quality) -> dict:
    details = {f"finding_{index}": code for index, code in enumerate(top_finding_codes(quality), start=1)}
    stop = quality.get("self_heal_stop_reason")
    if isinstance(stop, str) and _CODE.fullmatch(stop):
        details["stop_reason"] = stop
    return details


def queue_staff_hold_alert(run, *, now=None) -> str | None:
    """Queue the hold alert, and the 24-hour reminder once it is due; idempotent per hold."""
    if not staff_hold_alerts_enabled() or not staff_held(run):
        return None
    from .founder_journey_alerts import _receipts_in, queue_founder_journey_alert
    now = now or timezone.now()
    quality = _quality(run)
    slot, details = hold_slot(quality), _details(quality)
    organization = getattr(run, "organization", None)
    alert_id = queue_founder_journey_alert(organization=organization, run=run, alert_reason=HELD_REASON,
                                           details=details, slot=slot)
    first = _receipts_in(getattr(run, "result", None)).get(HELD_REASON)
    queued_at = _queued_at(first) if isinstance(first, dict) and first.get("alert_id") == alert_id else None
    if queued_at is not None and now - queued_at >= STILL_HELD_AFTER:
        return queue_founder_journey_alert(organization=organization, run=run, alert_reason=STILL_HELD_REASON,
                                           details=details, slot=slot)
    return alert_id


def _queued_at(receipt):
    from django.utils.dateparse import parse_datetime
    value = receipt.get("queued_at")
    try:
        stamp = parse_datetime(value) if isinstance(value, str) else None
    except ValueError:
        return None
    return stamp if stamp and stamp.tzinfo else None


def run_staff_hold_alert_sweep(now=None, limit=None) -> dict:
    """One scheduler tick: queue current staff holds and their 24-hour reminders, then send them."""
    if not staff_hold_alerts_enabled():
        return {"status": "disabled"}
    from .founder_journey_alerts import ARTICLE_WORKFLOWS, _deliver
    now = now or timezone.now()
    limit = max(1, int(limit if limit is not None else getattr(settings, "ARTICLES_FOUNDER_JOURNEY_ALERTS_MAX_PER_TICK", 20)))
    queued = 0
    runs = (ContentFactoryRun.objects.select_related("organization")
            .filter(workflow__in=ARTICLE_WORKFLOWS, organization__isnull=False,
                    result__article_preview_quality__hold_owner="staff",
                    result__article_preview_quality__self_heal_exhausted=True)
            .order_by("-updated_at")[:limit * 5])
    for run in runs:
        try:
            if queue_staff_hold_alert(run, now=now):
                queued += 1
        except Exception as exc:  # noqa: BLE001 - one run must never stop the sweep
            logger.warning("staff_hold_alert_queue_failed run_pk=%s error_type=%s", run.pk, type(exc).__name__)
    delivered = _deliver(now=now, limit=limit, reasons={HELD_REASON, STILL_HELD_REASON})
    return {"status": "completed", "held": queued, **delivered}
