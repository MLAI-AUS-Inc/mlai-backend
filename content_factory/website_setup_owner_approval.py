"""The owner click before any setup merge (P1.14, contract IF-P1-17).

A new-journey startup's articles setup reaches its website only after the
owner presses "Add to my website" on the preview (decision 12). That press is
the owner approval receipt, stored in the founder-journey marker
(``pillar_strategy["founder_journey"]["setupApproval"]``). MLAI then merges
the setup change server-side ("auto"), or leaves the pull request open for the
founder to review on GitHub ("manual").

The requirement is sticky (guarantee G10): once ``ownerClickRequired`` is on
the raw marker, turning V2 off or adding the organisation to the per-org kill
switch never brings back an automatic setup merge. Legacy organisations never
reach this module's policy: every hook returns None and today's path decides.

be-start and be-journey import this module lazily by its path; keep the module
path and the three public names stable.
"""
from __future__ import annotations

import logging

from django.utils import timezone

logger = logging.getLogger(__name__)

MERGE_AUTO = "auto"
MERGE_MANUAL = "manual"


def requires_owner_click(organization) -> bool:
    """True when this organisation's setup changes need the owner's click before a merge.

    That is every new-journey organisation, and (sticky) any organisation whose
    raw marker already carries ``ownerClickRequired``, whatever V2 or the kill
    switch now say.
    """
    from organizations.models import Organization
    if not isinstance(organization, Organization) or not organization.pk:
        return False
    from . import founder_journey_cohort as cohort
    if cohort.is_new_journey(organization):
        return True
    marker = cohort.read_marker(organization)
    return bool(isinstance(marker, dict) and marker.get("ownerClickRequired") is True)


def _approval(organization):
    from . import founder_journey_cohort as cohort
    marker = cohort.read_marker(organization)
    approval = marker.get("setupApproval") if isinstance(marker, dict) else None
    return approval if isinstance(approval, dict) else None


def on_owner_setup_approve(*, organization, user, run, data):
    """Record the owner's approval of a setup run; None keeps today's path.

    Returns None when :func:`requires_owner_click` is false. Otherwise (the
    caller has already checked that ``user`` owns the startup) it records
    ``setupApproval`` (``runId``, ``merge``, ``at``, ``by``),
    ``publishAutomatically`` (origin ``owner_setup_approval``, kept as it is
    when already on) and ``ownerClickRequired``, and returns
    ``{"merge": "manual"}`` when ``data["merge"] == "manual"``, else
    ``{"merge": "auto"}``. Both choices turn "Publish automatically" on; every
    article still needs its own approval. "manual" only means MLAI never merges
    this setup change server-side. When the receipt cannot be recorded the
    answer is "manual", so nothing merges without one.
    """
    if not requires_owner_click(organization):
        return None
    from . import founder_journey_cohort as cohort
    choice = MERGE_MANUAL if isinstance(data, dict) and data.get("merge") == MERGE_MANUAL else MERGE_AUTO
    now = timezone.now().isoformat()
    user_id = getattr(user, "pk", None)
    run_id = str(getattr(run, "run_id", "") or "")
    updates = {"setup_approval": {"runId": run_id, "merge": choice, "at": now, "by": user_id},
        "owner_click_required": True}
    marker = cohort.read_marker(organization) or {}
    if not isinstance(marker.get("publishAutomatically"), dict):
        updates["publish_automatically"] = {"enabledAt": now, "enabledBy": user_id, "origin": "owner_setup_approval"}
    saved = cohort.update_marker(organization, **updates)
    if saved is None:
        logger.warning("founder_setup_owner_click_unrecorded org=%s run=%s", organization.pk, run_id)
        choice = MERGE_MANUAL
    logger.info("founder_setup_owner_click org=%s run=%s merge=%s", organization.pk, run_id, choice)
    return {"merge": choice}


def may_merge_setup(*, organization, run) -> bool | None:
    """Whether MLAI may merge this setup run's change server-side; None keeps today's policy.

    None when :func:`requires_owner_click` is false. Otherwise True only when
    the marker's ``setupApproval`` names this exact run with ``merge == "auto"``.
    """
    if not requires_owner_click(organization):
        return None
    approval = _approval(organization)
    run_id = str(getattr(run, "run_id", "") or "")
    return bool(approval and run_id and approval.get("runId") == run_id and approval.get("merge") == MERGE_AUTO)


def approved_run_choice(*, organization, run):
    """The owner's recorded choice ("auto" or "manual") for this run, or None when not approved."""
    approval = _approval(organization)
    run_id = str(getattr(run, "run_id", "") or "")
    if approval and run_id and approval.get("runId") == run_id and approval.get("merge") in {MERGE_AUTO, MERGE_MANUAL}:
        return approval["merge"]
    return None


def require_owner_click(organization) -> None:
    """Write the sticky ``ownerClickRequired`` key for a new-journey organisation once."""
    from organizations.models import Organization
    from . import founder_journey_cohort as cohort
    if not isinstance(organization, Organization) or not cohort.is_new_journey(organization):
        return
    marker = cohort.read_marker(organization)
    if isinstance(marker, dict) and marker.get("ownerClickRequired") is True:
        return
    cohort.update_marker(organization, owner_click_required=True)
