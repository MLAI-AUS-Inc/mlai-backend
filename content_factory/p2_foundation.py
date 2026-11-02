"""The P2 cohort, the per-run stamp and the backend-owned keys (IF-P2-26, IF-P2-28).

Every P2 package decides "does this apply to this startup?" with
:func:`p2_applies` and nothing else. The P2 cohort is the P1 new-journey cohort
(``founder_journey_cohort.is_new_journey``) plus the legacy organisations an
operator opted in one at a time through ``ARTICLES_SELF_HEAL_LEGACY_ORGANIZATIONS``.
The backend always re-reads the cohort live, so removing an org from the opt-in
list takes effect at the next request or tick.

Content Factory cannot read the cohort, so dispatch stamps it on the run request
(:func:`stamp_run_request`) under ``self_heal_policy_v1``, next to the optional
``merge_policy``. CF ignores keys its request models do not declare, so the
backend can deploy first; a run without a stamp is legacy in CF.

This module imports nothing from the rest of ``content_factory`` at module
level, so the cohort and editorial modules can import its constants.
"""
from __future__ import annotations

from functools import lru_cache
import importlib
import logging

from django.conf import settings

logger = logging.getLogger(__name__)

# IF-P2-28: result keys the backend writes; a CF snapshot never drops or changes them.
P2_BACKEND_RESULT_KEYS = frozenset({
    "self_heal_v1", "points_v1", "channel_v1", "re_pin_v1", "article_approval_v1", "owner_merge_approval_v1",
    "merge_v1", "deploy_v1", "staff_actions_v1", "publish_offer_v1", "publish_reviewed_v1", "publish_dispatch_v1",
})
# IF-P2-26: run-request keys the backend owns; a snapshot never introduces or changes them.
P2_BACKEND_REQUEST_KEYS = frozenset({"self_heal_policy_v1", "merge_policy"})
# IF-P2-28: pillar_strategy keys that are backend bookkeeping, never strategy.
P2_PRIVATE_STRATEGY_KEYS = ("self_heal", "startup_team", "founder_notices", "founder_funnel", "first_article_commands")

STAMP_KEY = "self_heal_policy_v1"
MERGE_POLICY_KEY = "merge_policy"
STAMP_VERSION = 1
BACKEND_WORKER = "backend_worker"
TEAM_MODULE = "founder_tools.startup_team"
_PARENT_KEYS = ("parent_run_id", "source_run_id")


@lru_cache(maxsize=None)
def _optional(module: str, name: str):
    """The provider function ``module.name``, or None while that package is missing (IF-P2-24)."""
    try:
        return getattr(importlib.import_module(module), name)
    except (ImportError, AttributeError):
        return None


def legacy_opt_in_ids() -> frozenset[int]:
    """The organisation ids in ``ARTICLES_SELF_HEAL_LEGACY_ORGANIZATIONS``.

    The setting is a comma list of positive integer ids. Blank, non-numeric,
    zero and negative entries are ignored, so a malformed list can only opt
    in fewer organisations, never more.
    """
    raw = getattr(settings, "ARTICLES_SELF_HEAL_LEGACY_ORGANIZATIONS", "") or ""
    items = raw if isinstance(raw, (list, tuple, set, frozenset)) else str(raw).split(",")
    ids = set()
    for item in items:
        text = str(item).strip()
        if text.isascii() and text.isdigit() and int(text) > 0:
            ids.add(int(text))
    return frozenset(ids)


def p2_applies(organization) -> bool:
    """True when new P2 behaviour applies to ``organization`` (the P2 cohort).

    That is a new-journey organisation (``is_new_journey``, which needs the P1
    V2 switch) or an organisation opted in by id. ``None`` and unsaved
    organisations are never in the cohort.
    """
    if organization is None or getattr(organization, "pk", None) is None:
        return False
    if organization.pk in legacy_opt_in_ids():
        return True
    from .founder_journey_cohort import is_new_journey
    return is_new_journey(organization)


def policy_v2_applies(organization) -> bool:
    """True when ``ARTICLES_FAILURE_POLICY_V2_ENABLED`` is on and the org is in the P2 cohort."""
    return bool(getattr(settings, "ARTICLES_FAILURE_POLICY_V2_ENABLED", False)) and p2_applies(organization)


def policy_stamp(organization) -> dict:
    """The run-request stamp for ``organization`` as it stands now (IF-P2-26)."""
    return {"version": STAMP_VERSION, "cohort": "p2" if p2_applies(organization) else "legacy",
            "failure_policy": "v2" if policy_v2_applies(organization) else "pre_p2"}


def _requester(requester_user_id):
    if requester_user_id in (None, ""):
        return None
    try:
        identifier = int(requester_user_id)
    except (TypeError, ValueError):
        return None
    from core.models import User
    return User.objects.filter(pk=identifier).first()


def _member_without_publish_rights(organization, requester_user_id) -> bool:
    """The IF-P2-24 member clause: a startup-team member who may not publish.

    It needs be-team's ``viewer_role`` and ``member_may_publish``. While that
    package is missing no member exists, so the clause is false. A founder with
    their own company row is never a team member, so legacy runs are never
    stamped by this clause.
    """
    viewer_role = _optional(TEAM_MODULE, "viewer_role")
    may_publish = _optional(TEAM_MODULE, "member_may_publish")
    if viewer_role is None or may_publish is None:
        return False
    user = _requester(requester_user_id)
    if user is None:
        return False
    role = None
    try:
        role = viewer_role(user, organization)
        return role == "member" and not may_publish(user, organization)
    except Exception:  # noqa: BLE001 - dispatch may continue, but uncertain consent cannot auto-merge
        logger.warning("p2_merge_policy_member_check_failed org=%s", getattr(organization, "pk", None), exc_info=True)
        if role == "member":
            return True
        from founder_tools.services import organization_owner_user_id
        try:
            return organization_owner_user_id(organization) != user.pk
        except Exception:  # noqa: BLE001 - inability to verify ownership also retains the merge guard
            return True


def merge_policy_for(organization, *, requester_user_id) -> str | None:
    """``"backend_worker"`` when only the backend merge worker may merge this run, else None (IF-P2-15).

    Either the merge worker is on and the org is in the P2 cohort, or the
    requester is a startup-team member who may not publish. The second clause
    holds whatever the policy mode and worker flag say (M12).
    """
    if organization is None or getattr(organization, "pk", None) is None:
        return None
    if getattr(settings, "ARTICLES_MERGE_WORKER_ENABLED", False) and p2_applies(organization):
        return BACKEND_WORKER
    if _member_without_publish_rights(organization, requester_user_id):
        return BACKEND_WORKER
    return None


def stamp_run_request(payload: dict, *, organization, requester_user_id=None) -> dict:
    """Stamp ``payload`` in place with the P2 cohort and merge policy, and return it.

    Existing cohort and merge-policy values are immutable. A later caller
    that knows the requester may add a missing merge policy, so an early
    outbox stamp cannot bypass the member permission guard.
    """
    if not isinstance(payload, dict):
        return payload
    if STAMP_KEY not in payload:
        payload[STAMP_KEY] = policy_stamp(organization)
    if MERGE_POLICY_KEY not in payload:
        policy = merge_policy_for(organization, requester_user_id=requester_user_id)
        if policy is not None:
            payload[MERGE_POLICY_KEY] = policy
    return payload


def _request_merge_policy(request) -> str | None:
    value = request.get(MERGE_POLICY_KEY) if isinstance(request, dict) else None
    return value if value == BACKEND_WORKER else None


def run_merge_policy(run) -> str | None:
    """The run's merge policy: its own request, else its parent's, else its source run's."""
    request = getattr(run, "run_request", None)
    request = request if isinstance(request, dict) else {}
    own = _request_merge_policy(request)
    if own is not None:
        return own
    from workflow_runs.models import ContentFactoryRun
    for key in _PARENT_KEYS:
        parent_id = request.get(key)
        if not isinstance(parent_id, str) or not parent_id or parent_id == getattr(run, "run_id", None):
            continue
        parent = ContentFactoryRun.objects.filter(run_id=parent_id).values_list("run_request", flat=True).first()
        policy = _request_merge_policy(parent)
        if policy is not None:
            return policy
    return None
