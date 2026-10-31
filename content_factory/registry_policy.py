"""P2 registry policy reads for the backend (IF-P2-04, IF-P2-05, IF-P2-06).

Pure functions over ``website_failure_contract.load_registry()``. The registry's
``by_code`` is the pre-P2 view that every existing reader uses; this module is
the one place backend code reads the P2 view (``by_code_v2``), and only for an
organisation in the P2 cohort while ``ARTICLES_FAILURE_POLICY_V2_ENABLED`` is on.
The only database access is ``p2_foundation.p2_applies(organization)`` when an
organisation is passed.

Action ids: the registry's ``actions`` hold the 8 canonical asks. Old desktop
builds know only the legacy ids, so a payload builder that takes an id from the
P2 view sends it through :func:`wire_action_id`, which keeps the legacy id
until ``MY_STARTUP_CANONICAL_ACTION_IDS_ENABLED`` is on.
"""
from __future__ import annotations

from copy import deepcopy

from django.conf import settings

from . import p2_foundation
from .website_failure_contract import canonical_action, load_registry

# IF-P2-05: the legacy id each canonical ask travels as while canonical ids are off.
LEGACY_WIRE_ACTION_IDS: dict[str, str | None] = {
    "approve_setup": "review_setup",
    "describe_customer": "review_setup",
    "agree_terms": "review_setup",
    "approve_on_github": "merge_pr",
    "authorize_github": "authorize_github",
    "review_draft": "review_draft",
    "buy_points": "buy_points",
    "approve_join_request": None,
}
WORKING = "working"
STOPPED = "stopped"


def _policy_v2_for(organization) -> bool:
    return organization is not None and p2_foundation.policy_v2_applies(organization)


def effective_entry(code: str | None, organization=None) -> dict | None:
    """The registry entry P2 code reads for ``code`` (IF-P2-03); None when unregistered or None.

    The raw P2 entry only with ``ARTICLES_FAILURE_POLICY_V2_ENABLED`` on and an
    organisation in the P2 cohort; otherwise the pre-P2 view, exactly what
    ``by_code`` gives every existing reader.
    """
    if not isinstance(code, str):
        return None
    registry = load_registry()
    view = registry["by_code_v2"] if _policy_v2_for(organization) else registry["by_code"]
    entry = view.get(code)
    return deepcopy(entry) if entry is not None else None


def is_registered(code: str | None) -> bool:
    """Whether ``code`` (or an alias of it) is a registered causal code."""
    return isinstance(code, str) and code in load_registry()["by_code_v2"]


def failure_class(code: str | None, organization=None) -> str | None:
    """The effective class of ``code`` for ``organization``, or None when unregistered."""
    entry = effective_entry(code, organization)
    return entry["failure_class"] if entry else None


def policy(code: str | None, organization=None) -> dict | None:
    """``{"name": <policy id>, **registry policy}`` for the effective entry, or None."""
    entry = effective_entry(code, organization)
    if entry is None:
        return None
    return {"name": entry["retry_policy"], **deepcopy(load_registry()["policies"][entry["retry_policy"]])}


def founder_copy_id(code: str | None) -> str:
    """The code's founder copy id; ``"working"`` for None or an unregistered code.

    The same in both views (IF-P2-03).
    """
    entry = load_registry()["by_code_v2"].get(code) if isinstance(code, str) else None
    return entry["founder_copy_id"] if entry and entry.get("founder_copy_id") else WORKING


def refund_on_no_delivery(code: str | None) -> bool:
    """The registry refund flag; True for None or an unregistered code."""
    entry = load_registry()["by_code_v2"].get(code) if isinstance(code, str) else None
    return entry["refund"] if entry and type(entry.get("refund")) is bool else True


def canonical_action_id(action_id: str | None) -> str | None:
    """An alias resolves to its canonical ask; a canonical ask is itself; retired or unknown is None."""
    return canonical_action(load_registry(), action_id)


def canonical_ids_enabled() -> bool:
    """``MY_STARTUP_CANONICAL_ACTION_IDS_ENABLED``; off by default."""
    return bool(getattr(settings, "MY_STARTUP_CANONICAL_ACTION_IDS_ENABLED", False))


def wire_action_id(action_id: str | None) -> str | None:
    """The id to send for ``action_id`` on a wire shape old desktop builds read (IF-P2-05).

    With ``MY_STARTUP_CANONICAL_ACTION_IDS_ENABLED`` on, the canonical id.
    Otherwise the legacy id of its canonical ask (``approve_join_request`` has
    none and gives None). A retired or unknown id passes through unchanged.
    """
    if not isinstance(action_id, str) or not action_id:
        return None
    canonical = canonical_action_id(action_id)
    if canonical is None:
        return action_id
    if canonical_ids_enabled():
        return canonical
    return LEGACY_WIRE_ACTION_IDS.get(canonical, canonical)


def founder_ask_for_code(code: str | None, organization=None) -> str | None:
    """The canonical ask id for a needs_user code in ``organization``'s view, else None."""
    entry = effective_entry(code, organization)
    if not entry or entry["failure_class"] != "needs_user":
        return None
    return canonical_action_id(entry.get("action_id"))


def _engine_will_act(self_heal) -> bool:
    """The ``self_heal_v1`` receipt says the engine will still act on this item."""
    return (isinstance(self_heal, dict) and self_heal.get("mode") == "policy" and not self_heal.get("capped")
            and bool(self_heal.get("next_at")))


def copy_id_for(code: str | None, *, organization, terminal: bool, self_heal: dict | None) -> str | None:
    """The founder copy id for an item, or None to keep today's line (IF-P2-06).

    None unless ``ARTICLES_FAILURE_POLICY_V2_ENABLED`` is on and the org is in
    the P2 cohort. Otherwise a needs_user code gives its ask id; a terminal item
    gives ``stopped`` unless its ``self_heal_v1`` receipt says the engine will
    still act; anything else, an unregistered or missing code included, gives
    the code's ``founder_copy_id`` (``working`` when unregistered). Callers
    apply the journey's Done and Your turn rules first.
    """
    if not _policy_v2_for(organization):
        return None
    entry = effective_entry(code, organization)
    if entry is not None and entry["failure_class"] == "needs_user":
        copy_id = entry.get("founder_copy_id")
        if isinstance(copy_id, str) and copy_id.startswith("ask_"):
            return copy_id
        ask = canonical_action_id(entry.get("action_id"))
        return f"ask_{ask}" if ask else founder_copy_id(code)
    if terminal and not _engine_will_act(self_heal):
        return STOPPED
    return founder_copy_id(code)


def limits() -> dict:
    """Registry ``limits`` plus the retry_same_step backoff and its ceiling (IF-P2-01 item 5)."""
    registry = load_registry()
    retry = registry["policies"]["retry_same_step"]
    return {**deepcopy(registry.get("limits", {})), "backoff_minutes": list(retry["backoff_minutes"]),
            "max_elapsed_hours": retry["max_elapsed_hours"]}
