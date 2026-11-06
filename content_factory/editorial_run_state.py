"""Preserve a known article brief in worker observations, not issue approval.

Current catalogue eligibility belongs at effect boundaries. A revoked offer's
failure/status snapshot must remain recordable without changing its old brief.
"""
from copy import deepcopy

from .editorial_contract import ArticleEditorialAdmission, ArticleEditorialBrief
from .run_state import ARTICLE_WORKFLOWS

BRIEF_KEYS = ("editorial_brief", "editorialBrief")
_UNREADABLE = object()


class EditorialRunConflict(ValueError):
    """A snapshot would discard or change a known reader decision/tenant."""


def _request(value):
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise EditorialRunConflict("Run request must be an object")
    return value


def _brief(request, *, stored=False):
    keys = [key for key in BRIEF_KEYS if key in request]
    if not keys:
        return None
    if len(keys) == 2 and request[keys[0]] != request[keys[1]]:
        raise EditorialRunConflict("Run request has conflicting editorial brief fields")
    value = request[keys[0]]
    if value is None:
        return None  # Old catalogue-free worker snapshots may serialize null.
    try:
        return ArticleEditorialBrief.model_validate(value).model_dump(mode="json")
    except ValueError:
        raise EditorialRunConflict(
            "Stored editorial brief requires repair" if stored else "Incoming editorial brief is invalid"
        ) from None  # Do not expose Pydantic input values through callback logs.


def _admission(request, brief, *, stored=False):
    value = request.get("editorial_admission")
    if value is None:
        return None
    try:
        admitted = ArticleEditorialAdmission.model_validate(value)
        admitted.assert_request(brief, admitted.domain, request.get("github_repo"))
        return admitted
    except (TypeError, ValueError):
        raise EditorialRunConflict(
            "Stored editorial admission requires repair" if stored else "Incoming editorial admission is invalid"
        ) from None


def merge_editorial_run_snapshot(existing, incoming, *, drift=None):
    """Return a new snapshot, preserving a known brief when the worker omits it.

    A first-seen worker brief is structurally checked, not certified as a human
    selection or approved against current policy. No generation is authorised.
    A ``drift`` list compares by meaning: the stored brief is the backend's own
    record, so a worker copy that differs is noted there instead of refused.
    The admission, organisation and dispatch key stay rails.
    """
    existing = existing or {}
    result = deepcopy(incoming)
    old_request = _request(existing.get("run_request"))
    new_request = _request(incoming.get("run_request"))
    old_brief = _brief(old_request, stored=True)
    if drift is not None and old_brief is not None:
        from .snapshot_meaning import same_meaning
        if any(key in new_request for key in BRIEF_KEYS):
            try:
                reported = _brief(new_request)
            except EditorialRunConflict:
                reported = _UNREADABLE
            if reported is _UNREADABLE or not same_meaning(old_brief, reported, "editorial_brief"):
                drift.append("run_request.editorial_brief")
        new_brief = old_brief
    else:
        new_brief = _brief(new_request)
    old_admission = _admission(old_request, old_brief, stored=True)
    new_admission = _admission(new_request, new_brief if new_brief is not None else old_brief)
    if old_admission is not None and "editorial_admission" in new_request and new_admission != old_admission:
        raise EditorialRunConflict("A worker snapshot cannot change or clear the original editorial admission")
    if old_brief is None and new_brief is None:
        return result  # Preserve the non-editorial/legacy snapshot contract.
    if old_brief is not None and any(key in new_request for key in BRIEF_KEYS) and new_brief != old_brief:
        raise EditorialRunConflict("A worker snapshot cannot change or clear the stored editorial brief")

    old_domain = str(existing.get("domain") or "").strip()
    new_domain = str(incoming.get("domain") or "").strip()
    if old_domain and new_domain and old_domain.lower() != new_domain.lower():
        raise EditorialRunConflict("An editorial run cannot move to another organisation")
    domain = old_domain or new_domain
    if not domain:
        raise EditorialRunConflict("An editorial run requires its organisation domain")
    for admitted in (old_admission, new_admission):
        if admitted is not None:
            try:
                if admitted.normalized_domain(domain) != admitted.normalized_domain(admitted.domain):
                    raise ValueError("Admission belongs to another organisation")
            except (TypeError, ValueError):
                raise EditorialRunConflict("Editorial admission differs from the run organisation") from None
    for request in (old_request, new_request):
        request_domain = str(request.get("domain") or "").strip()
        if request_domain and request_domain.lower() != domain.lower():
            raise EditorialRunConflict("Run request domain differs from its organisation")
    if incoming.get("workflow") not in ARTICLE_WORKFLOWS or (
        existing.get("workflow") and existing["workflow"] not in ARTICLE_WORKFLOWS
    ):
        raise EditorialRunConflict("An editorial brief must stay on its article workflow")

    merged_request = deepcopy(new_request)
    for key in BRIEF_KEYS:
        merged_request.pop(key, None)
    merged_request["editorial_brief"] = deepcopy(old_brief if old_brief is not None else new_brief)
    admission = old_admission if old_admission is not None else new_admission
    if admission is not None:
        merged_request["editorial_admission"] = admission.model_dump(mode="json")
    old_key = old_request.get("client_request_id")
    new_key = new_request.get("client_request_id")
    if old_key:
        if drift is not None:
            from .snapshot_meaning import same_meaning
            changed_key = bool(new_key) and not same_meaning(old_key, new_key, "client_request_id")
        else:
            changed_key = bool(new_key) and new_key != old_key
        if changed_key:
            raise EditorialRunConflict("An editorial run cannot change its dispatch key")
        merged_request["client_request_id"] = old_key
    result["run_request"] = merged_request
    result["domain"] = domain
    return result
